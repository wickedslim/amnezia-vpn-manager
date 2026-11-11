from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
import time
from pathlib import Path

import paramiko

from .domain import ManagerError, Server

q = shlex.quote
COMMAND_TIMEOUT = 25


class Runner:
    """Synchronous transport, exclusively used in a worker under a per-server lock."""

    def __init__(self, server: Server, data_dir: Path):
        self.server = server
        self.data_dir = data_dir
        self.client = None

    def close(self):
        if self.client:
            self.client.close()
            self.client = None

    def connect(self):
        if self.client and self.client.get_transport() and self.client.get_transport().is_active():
            return
        self.close()
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        directory = self.data_dir / "known_hosts"
        directory.mkdir(mode=0o700, exist_ok=True)
        identity = hashlib.sha256(f"{self.server.host}:{self.server.port}".encode()).hexdigest()
        known = directory / identity
        known.touch(mode=0o600, exist_ok=True)
        client.load_host_keys(str(known))
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=self.server.host, port=self.server.port, username=self.server.username,
                password=self.server.password if self.server.auth_type == "password" else None,
                key_filename=self.server.key_path if self.server.auth_type == "key" else None,
                timeout=8, banner_timeout=8, auth_timeout=8, channel_timeout=8,
                look_for_keys=False, allow_agent=False,
            )
            client.get_transport().set_keepalive(30)
            self.client = client
        except paramiko.AuthenticationException as exc:
            client.close()
            raise ManagerError("SSH отклонил вход. Проверьте пользователя и пароль или ключ.") from exc
        except paramiko.BadHostKeyException as exc:
            client.close()
            raise ManagerError("SSH-ключ сервера изменился. Проверьте сервер и запись known_hosts.") from exc
        except Exception as exc:
            client.close()
            raise ManagerError("Не удалось подключиться по SSH. Проверьте адрес, порт и доступность сервера.") from exc

    def run(self, command: str, data: bytes | None = None, *, timeout: int = COMMAND_TIMEOUT) -> str:
        if self.server.local:
            try:
                result = subprocess.run(["sh", "-c", command], input=data, capture_output=True, timeout=timeout)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ManagerError("Локальная команда не завершилась за отведённое время.") from exc
            if result.returncode:
                if b"MANAGER_STALE" in result.stderr:
                    raise ManagerError("Конфигурация изменилась другим приложением. Обновите список и повторите действие.")
                raise ManagerError("Команда VPN завершилась с ошибкой. Проверьте контейнер и права доступа к Docker.")
            return result.stdout.decode()
        self.connect()
        try:
            stdin, stdout, _ = self.client.exec_command(command, timeout=timeout)
            channel = stdout.channel
            if data:
                channel.sendall(data)
            channel.shutdown_write()
            output, errors = bytearray(), bytearray()
            deadline = time.monotonic() + timeout
            while True:
                while channel.recv_ready():
                    output.extend(channel.recv(65536))
                while channel.recv_stderr_ready():
                    errors.extend(channel.recv_stderr(65536))
                if len(output) + len(errors) > 16 * 1024 * 1024:
                    channel.close()
                    raise ManagerError("Ответ сервера слишком большой.")
                if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                    break
                if time.monotonic() >= deadline:
                    channel.close()
                    raise ManagerError("Сервер не ответил вовремя. Попробуйте обновить данные позже.")
                time.sleep(0.01)
            status = channel.recv_exit_status()
            channel.close()
            if status:
                if b"MANAGER_STALE" in errors:
                    raise ManagerError("Конфигурация изменилась другим приложением. Обновите список и повторите действие.")
                raise ManagerError("Команда VPN завершилась с ошибкой. Проверьте контейнер и доступ пользователя к Docker.")
            return output.decode()
        except ManagerError:
            raise
        except Exception as exc:
            self.close()
            raise ManagerError("SSH-соединение прервалось. Проверьте состояние сервера.") from exc

    def docker(self, script: str, data: bytes | None = None) -> str:
        return self.run(f"docker exec -i {q(self.server.container)} sh -c {q(script)}", data)


WG_SELECT = 'if command -v awg >/dev/null 2>&1; then WG=awg; else WG=wg; fi\n'


def read_snapshot(runner: Runner) -> tuple[str, str, str]:
    path = q(runner.server.config_path)
    script = f"""set -eu
{WG_SELECT}
cat {path}
printf '\\036MANAGER_TABLE\\n'
if test -f /opt/amnezia/awg/clientsTable; then cat /opt/amnezia/awg/clientsTable; else printf '[]'; fi
printf '\\036MANAGER_RUNTIME\\n'
"$WG" show all dump
"""
    result = runner.docker(script)
    try:
        config, rest = result.split("\x1eMANAGER_TABLE\n", 1)
        table, runtime = rest.split("\x1eMANAGER_RUNTIME\n", 1)
        return config, table, runtime
    except ValueError as exc:
        raise ManagerError("Не удалось прочитать конфигурацию и статистику VPN.") from exc


def write_peer(runner: Runner, *, before: str, after: str, table_before: str, table_after: str,
               interface: str, key: str, allowed_ips: str, psk: str, remove=False, old_peer="", replace_key="", restore_peer=False, add_peers=(), table_only=False):
    """Compare-and-swap persistent config, targeted runtime update, rollback on failure.

    Payload goes through stdin; private configuration never appears in process arguments.
    The VPN interface is never stopped. Every successful mutation leaves a server-side backup.
    """
    import base64
    import re
    values = [before, after, table_before, table_after, psk, old_peer]
    payload = b"".join(base64.b64encode(value.encode()) + b"\n" for value in values)
    # AmneziaWG resets its obfuscation parameters (Jc, S*, H*, ...) to defaults on every runtime
    # change of a peer ("wg set" and "wg addconf"). The server then stops understanding all clients
    # until the container restarts. Every runtime command therefore carries the parameters from
    # the interface section of the saved configuration.
    interface_text = after.split("[Peer]", 1)[0]
    parameters = [(name.lower(), value.strip()) for name, value in re.findall(
        r"(?m)^\s*(Jc|Jmin|Jmax|S[1-4]|H[1-4]|I[1-5]|J[1-3]|Itime)\s*=\s*(\S[^\n]*)$", interface_text, re.I)]
    keep = " ".join(f"{name} {q(value)}" for name, value in parameters)
    def run_set(tail):
        return f'"$WG" set {q(interface)} {keep + " " if keep else ""}{tail}'
    reapply = f'"$WG" set {q(interface)} {keep}' if keep else ':'
    action = run_set(f'peer {q(key)} remove') if remove else run_set(f'peer {q(key)} preshared-key "$tmp/psk" allowed-ips {q(allowed_ips)}')
    rollback = f'"$WG" addconf {q(interface)} "$tmp/peer"' if remove else run_set(f'peer {q(key)} remove')
    if restore_peer:
        action = f'"$WG" addconf {q(interface)} "$tmp/peer"'
    if add_peers:
        from .domain import validate_key
        action = f'"$WG" addconf {q(interface)} "$tmp/peer"'
        rollback = "\n        ".join(
            run_set(f'peer {q(validate_key(peer))} remove') + ' || true' for peer in add_peers
        )
    if replace_key:
        from .domain import validate_key
        validate_key(replace_key)
        action = run_set(f'peer {q(replace_key)} remove') + '\n' + action
        rollback = run_set(f'peer {q(key)} remove') + ' || true\n        "$WG" addconf ' + q(interface) + ' "$tmp/peer"'
    if table_only:
        action = rollback = reapply = ":"
    script = f"""set -eu
umask 077
{WG_SELECT}
cfg={q(runner.server.config_path)}
table=/opt/amnezia/awg/clientsTable
tmp=$(mktemp -d "$(dirname "$cfg")/.manager.XXXXXX")
changed=0
table_existed=0
test ! -f "$table" || table_existed=1
cleanup() {{
    status=$?
    trap - EXIT HUP INT TERM
    if test "$status" -ne 0 && test "$changed" -eq 1; then
        cp "$tmp/old" "$cfg" || true
        if test "$table_existed" -eq 1; then cp "$tmp/table.old" "$table" || true; else rm -f "$table"; fi
        {rollback} || true
        {reapply} || true
    fi
    rm -rf "$tmp"
    exit "$status"
}}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
for name in old new table.old table.new psk peer; do
    IFS= read -r encoded
    printf '%s' "$encoded" | base64 -d > "$tmp/$name"
done
cmp -s "$cfg" "$tmp/old" || {{ echo MANAGER_STALE >&2; exit 1; }}
if test "$table_existed" -eq 1; then
    cmp -s "$table" "$tmp/table.old" || {{ echo MANAGER_STALE >&2; exit 1; }}
fi
cp "$cfg" "$cfg.manager-backup"
if test "$table_existed" -eq 1; then cp "$table" "$table.manager-backup"; fi
changed=1
{action}
{reapply}
mv "$tmp/new" "$cfg"
mv "$tmp/table.new" "$table"
changed=0
"""
    runner.docker(script, payload)
