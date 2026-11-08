from __future__ import annotations

import asyncio
import base64
import io
import hashlib
import json
import logging
import os
import re
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .domain import ManagerError, Server, WGConfig, allocate_address, client_config, fields, Peer, parse_config, parse_dump, validate_key, validate_name
from .audit import audited
from .store import Store
from .transport import Runner, WG_SELECT, q, read_snapshot, write_peer

log = logging.getLogger(__name__)

TRAFFIC_COLLECTOR = "/opt/amnezia-service-traffic/collector.py"
TRAFFIC_PYTHON = "/opt/amnezia-service-traffic/venv/bin/python"
TRAFFIC_UNIT = "amnezia-service-traffic.service"
TRAFFIC_RUNNING = {"active", "activating", "deactivating", "reloading"}
TRAFFIC_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
TRAFFIC_INSTALL_COMMAND = """set -eu
umask 077
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
for name in install-service-traffic.sh service_traffic.py; do
    IFS= read -r payload
    printf '%s' "$payload" | base64 -d > "$work/$name"
done
timeout --kill-after=5s 180s bash "$work/install-service-traffic.sh" "$@"
"""


def parse_table(text: str) -> list[dict]:
    try:
        table = json.loads(text)
        if not isinstance(table, list) or any(not isinstance(c, dict) or not isinstance(c.get("userData"), dict) or not isinstance(c.get("clientId"), str) or not isinstance(c["userData"].get("clientName", ""), str) for c in table):
            raise ValueError
        return table
    except (ValueError, TypeError) as exc:
        raise ManagerError("Не удалось прочитать clientsTable. Изменения не выполнялись.") from exc


@dataclass
class Snapshot:
    config: WGConfig
    text: str
    table_text: str
    table: list[dict]
    server_public: str
    timestamp: float
    runtime_peers: dict


class VPNService:
    def __init__(self, store: Store, runner_factory=Runner):
        self.store = store
        self.runner_factory = runner_factory
        self.runners: dict[int, Runner] = {}
        self.locks: dict[int, asyncio.Lock] = {}
        self.cache: dict[int, Snapshot] = {}
        self.capacity = asyncio.Semaphore(4)
        self.traffic_installs: dict[int, asyncio.Task] = {}
        self.traffic_install_errors: dict[int, str] = {}

    def lock(self, sid: int):
        return self.locks.setdefault(sid, asyncio.Lock())

    def runner(self, sid: int):
        if sid not in self.runners:
            self.runners[sid] = self.runner_factory(self.store.server(sid), self.store.directory)
        return self.runners[sid]

    async def worker(self, operation):
        async with self.capacity:
            task = asyncio.create_task(asyncio.to_thread(operation))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # The worker must finish before the enclosing server lock is released.
                try:
                    await task
                finally:
                    raise

    async def service_traffic(self, sid: int, window: str, public_key: str = ""):
        server = self.store.server(sid)
        args = ["report", server.container, Path(server.config_path).stem, "--window", window]
        if public_key:
            args.extend(["--peer", hashlib.sha256(public_key.encode()).hexdigest()])
        command = (f"if [ -f {TRAFFIC_COLLECTOR} ]; then exec {TRAFFIC_PYTHON} {TRAFFIC_COLLECTOR} "
                   + " ".join(map(q, args)) + "; fi")
        async with self.lock(sid):
            output = await self.worker(lambda: self.runner(sid).run(command, timeout=5))
        return json.loads(output) if output else {"state": "disabled", "rows": []}

    def _service_traffic_status(self, server: Server, runner: Runner) -> dict:
        output = runner.run(
            f"state=$(systemctl show -p LoadState {TRAFFIC_UNIT}) || "
            f'[ "$state" = LoadState=not-found ] || exit 1; '
            f'if [ "$state" = LoadState=loaded ] && [ -f {TRAFFIC_COLLECTOR} ]; then '
            f"systemctl show -p LoadState -p ExecStart -p ActiveState -p UnitFileState {TRAFFIC_UNIT}; "
            f'else printf "%s\\n" "$state"; fi',
            timeout=5,
        )
        props = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        load = props.get("LoadState")
        if not output or load == "not-found":
            return {"state": "missing"} if self._can_install_traffic(server) else {"state": "requires_root"}
        expected = f"{TRAFFIC_PYTHON} {TRAFFIC_COLLECTOR} collect {server.container} {Path(server.config_path).stem}"
        commands = re.findall(r"argv\[\]=(.*?) ;", props.get("ExecStart", ""))
        if load != "loaded" or commands != [expected]:
            return {"state": "unmanaged"}
        activity = props.get("ActiveState")
        mode = props.get("UnitFileState")
        if not self._can_install_traffic(server) or mode not in {"enabled", "disabled", "enabled-runtime"}:
            return {"state": "unmanaged"}
        return {"state": "ready", "enabled": mode.startswith("enabled") or activity in TRAFFIC_RUNNING,
                "running": activity in {"active", "reloading"}}

    @staticmethod
    def _can_install_traffic(server: Server) -> bool:
        return os.geteuid() == 0 if server.local else server.username == "root"

    async def start_service_traffic_install(self, sid: int, *, enable: bool = False) -> dict:
        server = self.store.server(sid)
        if not self._can_install_traffic(server):
            raise ManagerError("Для установки сборщика нужен root на выбранном VPS.")
        async with self.lock(sid):
            if sid in self.traffic_installs:
                return {"state": "installing"}
            def prepare():
                runner = self.runner(sid)
                status = self._service_traffic_status(server, runner)
                if enable and status["state"] == "ready" and not status["running"]:
                    return self._set_service_traffic(server, runner, True)
                return status

            self.traffic_install_errors.pop(sid, None)
            status = await self.worker(prepare)
            if status["state"] == "ready":
                return status
            if status["state"] != "missing":
                raise ManagerError("На VPS уже есть служба сборщика. Проверьте, к какому интерфейсу она привязана.")
            try:
                payload = b"".join(
                    base64.b64encode((TRAFFIC_SCRIPTS / name).read_bytes()) + b"\n"
                    for name in ("install-service-traffic.sh", "service_traffic.py")
                )
            except OSError as exc:
                raise ManagerError("В установке менеджера нет файлов сборщика. Обновите менеджер и повторите.") from exc
            self.traffic_installs[sid] = asyncio.create_task(self._install_service_traffic(server, payload, enable))
            return {"state": "installing"}

    async def _install_service_traffic(self, server: Server, payload: bytes, enable: bool) -> None:
        sid = server.id

        def install():
            # Package download uses its own SSH connection so VPN polling can continue.
            runner = self.runner_factory(server, self.store.directory)
            try:
                args = [TRAFFIC_INSTALL_COMMAND, "install-traffic", server.container, Path(server.config_path).stem]
                runner.run("sh -c " + " ".join(map(q, args)), payload, timeout=195)
                status = self._service_traffic_status(server, runner)
                if status["state"] != "ready":
                    raise ManagerError("Служба сборщика не появилась после установки.")
                if enable:
                    try:
                        self._set_service_traffic(server, runner, True)
                    except ManagerError:
                        return "nDPI установлен, но сбор не запустился. Проверьте службу сборщика."
            finally:
                runner.close()

        try:
            error = await self.worker(install)
            if error:
                self.traffic_install_errors[sid] = error
        except Exception as exc:
            log.warning("Traffic collector install failed for server %s: %s", sid, type(exc).__name__)
            self.traffic_install_errors[sid] = (
                "Не удалось установить сборщик. Проверьте контейнер и VPN-интерфейс, "
                "Python с venv и доступ VPS к PyPI."
            )
        finally:
            self.traffic_installs.pop(sid, None)

    def _set_service_traffic(self, server: Server, runner: Runner, enabled: bool) -> dict:
        if enabled:
            runner.run("systemctl is-active --quiet docker.service", timeout=5)
        action = "enable" if enabled else "disable"
        runner.run(f"systemctl --no-ask-password {action} --now {TRAFFIC_UNIT}", timeout=15)
        result = self._service_traffic_status(server, runner)
        if result.get("enabled") != enabled or (enabled and not result.get("running")):
            raise ManagerError("Сборщик не перешёл в нужное состояние. Проверьте статус службы.")
        return result

    async def service_traffic_control(self, sid: int, enabled: bool | None = None) -> dict:
        server = self.store.server(sid)

        def control():
            runner = self.runner(sid)
            status = self._service_traffic_status(server, runner)
            if enabled is None:
                if sid in self.traffic_install_errors:
                    status["error"] = self.traffic_install_errors[sid]
                return status
            if status["state"] != "ready":
                raise ManagerError("Управление этим сборщиком недоступно.")
            result = self._set_service_traffic(server, runner, enabled)
            self.traffic_install_errors.pop(sid, None)
            return result

        async with self.lock(sid):
            if sid in self.traffic_installs:
                if enabled is not None:
                    raise ManagerError("Дождитесь завершения установки сборщика.")
                return {"state": "installing"}
            return await self.worker(control)

    def _read(self, sid: int) -> Snapshot:
        server = self.store.server(sid)
        text, table_text, runtime = read_snapshot(self.runner(sid))
        table = parse_table(table_text)
        names = {c["clientId"]: c["userData"].get("clientName", "") for c in table}
        config = parse_config(text, names)
        interface = Path(server.config_path).stem
        public, peers = parse_dump(runtime, interface, include_addresses=True)
        for peer in config.peers:
            peer.runtime_present = peer.public_key in peers
            for key, value in peers.get(peer.public_key, {}).items():
                if key != "runtime_ips":
                    setattr(peer, key, value)
        snapshot = Snapshot(config, text, table_text, table, public, time.time(), peers)
        self.store.reconcile_rotations(sid, {p.public_key for p in config.peers})
        active = {peer.public_key for peer in config.peers}
        saved = self.paused(sid)
        restored = active & peers.keys() & saved.keys()
        if restored:
            saved = {key: item for key, item in saved.items() if key not in restored}
            self.store.set_pref(f'paused_clients:{sid}', saved)
        paused = [Peer(key, item['name'], item['ips'], item['block'], runtime_present=False)
                  for key, item in saved.items() if key not in active]
        self.store.sync(sid, config.peers + paused, public)
        self.store.set_pref(f"health:{sid}", {"ok": True, "updated": snapshot.timestamp})
        self.cache[sid] = snapshot
        return snapshot

    async def snapshot(self, sid: int, force=False) -> Snapshot:
        self.store.server(sid)
        async with self.lock(sid):
            cached = self.cache.get(sid)
            if cached and not force and time.time() - cached.timestamp < 10:
                return cached
            try:
                return await self.worker(lambda: self._read(sid))
            except ManagerError as exc:
                self.store.set_pref(f"health:{sid}", {"ok": False, "error": str(exc), "updated": time.time()})
                raise

    async def probe(self, server: Server):
        def operation():
            runner = self.runner_factory(server, self.store.directory)
            try:
                text, table, runtime = read_snapshot(runner)
                parse_config(text)
                parse_table(table)
                parse_dump(runtime, Path(server.config_path).stem)
            finally:
                runner.close()
        await self.worker(operation)

    async def overview(self, force=False) -> dict[int, Snapshot | ManagerError]:
        async def one(server):
            try:
                return server.id, await self.snapshot(server.id, force)
            except ManagerError as exc:
                return server.id, exc
        return dict(await asyncio.gather(*(one(s) for s in self.store.servers())))

    @audited('client.create')
    async def create_client(self, sid: int, name: str, duration: int | None, limit: int | None) -> dict:
        name = validate_name(name)
        async with self.lock(sid):
            def operation():
                snapshot = self._read(sid)
                if any(p.name.casefold() == name.casefold() for p in snapshot.config.peers):
                    raise ManagerError("Клиент с таким именем на этом сервере уже есть.")
                server = self.store.server(sid)
                if not server.endpoint:
                    raise ManagerError("У сервера не задан внешний адрес для клиентской конфигурации.")
                runner = self.runner(sid)
                script = f"""set -eu
{WG_SELECT}
private=$("$WG" genkey)
printf '%s\\n' "$private"
printf '%s\\n' "$private" | "$WG" pubkey
"$WG" genpsk
"""
                keys = runner.docker(script).splitlines()
                if len(keys) != 3:
                    raise ManagerError("Не удалось сгенерировать ключи клиента.")
                private, public, psk = map(validate_key, keys)
                reserved = [Peer(key, item['name'], item['ips'], item['block'])
                            for key, item in self.paused(sid).items()]
                address = allocate_address(WGConfig(snapshot.config.header, snapshot.config.interface,
                                                     snapshot.config.peers + reserved))
                generated = client_config(snapshot.config, address, private, snapshot.server_public, psk, server.endpoint)
                # Persist the private key before touching the remote server.
                with self.store.transaction() as conn:
                    cursor = conn.execute("INSERT INTO clients(server_id,public_key,name,allowed_ips,config,expires,traffic_limit,present) VALUES (?,?,?,?,?,?,?,0)", (sid, public, name, address, generated, int(time.time()) + duration if duration else None, limit))
                    cid = cursor.lastrowid
                block = f"[Peer]\n# {name}\nPublicKey = {public}\nPresharedKey = {psk}\nAllowedIPs = {address}\n"
                peer = Peer(public, name, address, block)
                table = snapshot.table + [{"clientId": public, "userData": {"clientName": name, "creationDate": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())}}]
                try:
                    write_peer(runner, before=snapshot.text, after=snapshot.config.render(snapshot.config.peers + [peer]), table_before=snapshot.table_text, table_after=json.dumps(table, ensure_ascii=False), interface=Path(server.config_path).stem, key=public, allowed_ips=address, psk=psk)
                finally:
                    self.cache.pop(sid, None)
                self._read(sid)
                return self.store.client(sid, cid)
            return await self.worker(operation)

    def paused(self, sid):
        return self.store.get_pref(f'paused_clients:{sid}', {})

    @audited('client.pause')
    async def set_paused(self, sid: int, cid: int, pause: bool):
        async with self.lock(sid):
            def operation():
                snapshot = self._read(sid)
                client = self.store.client(sid, cid)
                key = client['public_key']
                saved = self.paused(sid)
                peer = next((item for item in snapshot.config.peers if item.public_key == key), None)
                if pause:
                    if peer is None and key in saved:
                        if key not in snapshot.runtime_peers:
                            return
                        item = saved[key]
                        peer = Peer(key, item['name'], item['ips'], item['block'])
                    if peer is None:
                        raise ManagerError('Клиент больше не существует на сервере.')
                    saved[key] = {'name': peer.name, 'ips': peer.allowed_ips, 'block': peer.block}
                    # Save the recovery material before removing the peer.
                    self.store.set_pref(f'paused_clients:{sid}', saved)
                    peers = [item for item in snapshot.config.peers if item.public_key != key]
                else:
                    if key not in saved:
                        return
                    if (client['expires'] is not None and client['expires'] <= time.time()
                            or client['traffic_limit'] is not None and client['total_rx'] + client['total_tx'] >= client['traffic_limit']):
                        raise ManagerError('Сначала продлите срок или измените лимит клиента.')
                    item = saved[key]
                    peer = Peer(key, item['name'], item['ips'], item['block'])
                    active = next((other for other in snapshot.config.peers if other.public_key == key), None)
                    if active is not None:
                        if key in snapshot.runtime_peers and fields(active.block) == fields(peer.block):
                            saved.pop(key)
                            self.store.set_pref(f'paused_clients:{sid}', saved)
                            return
                        raise ManagerError('Конфиг клиента уже изменён на сервере.')
                    if key in snapshot.runtime_peers:
                        raise ManagerError('Клиент уже существует в активном интерфейсе.')
                    from ipaddress import ip_network
                    networks = [ip_network(ip.strip(), strict=False) for ip in peer.allowed_ips.split(',') if ip.strip()]
                    occupied = [other.allowed_ips for other in snapshot.config.peers]
                    occupied += [other.get('runtime_ips', '') for other in snapshot.runtime_peers.values()]
                    for ips in occupied:
                        for ip in ips.split(','):
                            if ip.strip() and any(network.overlaps(ip_network(ip.strip(), strict=False)) for network in networks):
                                raise ManagerError('Адрес клиента занят. Освободите его перед возобновлением.')
                    peers = snapshot.config.peers + [peer]
                try:
                    write_peer(self.runner(sid), before=snapshot.text, after=snapshot.config.render(peers),
                               table_before=snapshot.table_text, table_after=snapshot.table_text,
                               interface=Path(self.store.server(sid).config_path).stem, key=key,
                               allowed_ips=peer.allowed_ips, psk=fields(peer.block).get('PresharedKey', ''),
                               remove=pause, old_peer=peer.block, restore_peer=not pause)
                finally:
                    self.cache.pop(sid, None)
                if not pause:
                    saved.pop(key)
                    self.store.set_pref(f'paused_clients:{sid}', saved)
                self._read(sid)
            await self.worker(operation)

    @audited('client.delete')
    async def delete_client(self, sid: int, cid: int):
        async with self.lock(sid):
            def operation():
                snapshot = self._read(sid)
                client = self.store.client(sid, cid)
                key = client["public_key"]
                peer = next((p for p in snapshot.config.peers if p.public_key == key), None)
                if peer is None:
                    saved = self.paused(sid)
                    if key not in saved:
                        raise ManagerError("Клиент уже удалён с сервера.")
                    if key in snapshot.runtime_peers:
                        raise ManagerError('Клиент присутствует в активном интерфейсе. Проверьте сервер.')
                    table = [entry for entry in snapshot.table if entry['clientId'] != key]
                    write_peer(self.runner(sid), before=snapshot.text, after=snapshot.text,
                               table_before=snapshot.table_text, table_after=json.dumps(table, ensure_ascii=False),
                               interface=Path(self.store.server(sid).config_path).stem, key=key,
                               allowed_ips='', psk='', remove=True, old_peer=saved[key]['block'],
                               table_only=True)
                    saved.pop(key)
                    self.store.set_pref(f'paused_clients:{sid}', saved)
                    self._read(sid)
                    return
                server = self.store.server(sid)
                remaining = [p for p in snapshot.config.peers if p.public_key != key]
                table = [entry for entry in snapshot.table if entry["clientId"] != key]
                try:
                    write_peer(self.runner(sid), before=snapshot.text, after=snapshot.config.render(remaining), table_before=snapshot.table_text, table_after=json.dumps(table, ensure_ascii=False), interface=Path(server.config_path).stem, key=key, allowed_ips="", psk="", remove=True, old_peer=peer.block)
                finally:
                    self.cache.pop(sid, None)
                saved = self.paused(sid)
                if key in saved:
                    saved.pop(key)
                    self.store.set_pref(f'paused_clients:{sid}', saved)
                self._read(sid)
            await self.worker(operation)

    @audited('server.disconnect')
    async def disconnect_server(self, sid: int):
        async with self.lock(sid):
            runner = self.runners.pop(sid, None)
            if runner:
                await self.worker(runner.close)
            self.store.remove_server(sid)
            self.cache.pop(sid, None)

    @audited('client.reissue')
    async def reissue_client(self, sid: int, cid: int, expected_key: str) -> dict:
        async with self.lock(sid):
            def operation():
                snapshot = self._read(sid)
                client = self.store.client(sid, cid)
                if client['public_key'] != expected_key:
                    raise ManagerError('Конфиг уже перевыпущен или изменён. Откройте карточку заново.')
                if (client['expires'] is not None and client['expires'] <= time.time()
                        or client['traffic_limit'] is not None and client['total_rx'] + client['total_tx'] >= client['traffic_limit']):
                    raise ManagerError('Срок или лимит клиента исчерпан. Сначала измените ограничения.')
                peer = next((p for p in snapshot.config.peers if p.public_key == expected_key), None)
                if not peer:
                    raise ManagerError('Возобновите доступ перед перевыпуском конфига.')
                server = self.store.server(sid)
                if not server.endpoint:
                    raise ManagerError('У сервера не задан внешний адрес для клиентской конфигурации.')
                runner = self.runner(sid)
                keys = runner.docker(f"""set -eu
{WG_SELECT}
private=$("$WG" genkey)
printf '%s\\n' "$private"
printf '%s\\n' "$private" | "$WG" pubkey
"$WG" genpsk
""").splitlines()
                if len(keys) != 3:
                    raise ManagerError('Не удалось сгенерировать ключи клиента.')
                private, public, psk = map(validate_key, keys)
                generated = client_config(snapshot.config, peer.allowed_ips, private, snapshot.server_public, psk, server.endpoint)
                # Keep all peer options, replacing only its cryptographic identity.
                block = re.sub(r'(?m)^(\s*PublicKey\s*=)[^\n]*', lambda m: m[1] + ' ' + public, peer.block)
                block = re.sub(r'(?m)^\s*PresharedKey\s*=[^\n]*\n?', '', block)
                block = block.rstrip() + '\nPresharedKey = ' + psk + '\n'
                from dataclasses import replace
                replacement = replace(peer, public_key=public, block=block)
                peers = [replacement if p.public_key == expected_key else p for p in snapshot.config.peers]
                table = [{**entry, 'clientId': public} if entry['clientId'] == expected_key else entry for entry in snapshot.table]
                self.store.prepare_rotation(sid, cid, expected_key, public, generated)
                try:
                    write_peer(runner, before=snapshot.text, after=snapshot.config.render(peers), table_before=snapshot.table_text,
                               table_after=json.dumps(table, ensure_ascii=False), interface=Path(server.config_path).stem,
                               key=public, allowed_ips=peer.allowed_ips, psk=psk, replace_key=expected_key, old_peer=peer.block)
                finally:
                    self.cache.pop(sid, None)
                self._read(sid)
                return self.store.client(sid, cid)
            return await self.worker(operation)

    @audited('server.update')
    async def update_server(self, server: Server):
        async with self.lock(server.id):
            await self.probe(server)
            self.store.update_server(server)
            runner = self.runners.pop(server.id, None)
            if runner:
                await self.worker(runner.close)
            self.cache.pop(server.id, None)

    async def monitor_once(self) -> list[tuple[int, str]]:
        results = await self.overview(force=True)
        notifications = []
        for sid, result in results.items():
            if isinstance(result, ManagerError):
                continue
            for client in self.store.clients(sid):
                expired = client["expires"] is not None and client["expires"] <= time.time()
                used = client["total_rx"] + client["total_tx"]
                exhausted = client["traffic_limit"] is not None and used >= client["traffic_limit"]
                if (expired or exhausted) and client['public_key'] not in self.paused(sid):
                    try:
                        await self.delete_client(sid, client["id"])
                        notifications.append((sid, f"Клиент {client['name']} удалён: {'истёк срок' if expired else 'исчерпан трафик'}."))
                    except ManagerError:
                        log.warning("Не удалось применить ограничение: server=%d client=%d", sid, client["id"])
        return notifications

    @audited('backup.restore')
    async def restore_backup(self, sid: int, archive: bytes, expected=None):
        """Restore missing peers without overwriting existing peers or manager data."""
        async with self.lock(sid):
            def operation():
                from ipaddress import ip_interface, ip_network
                try:
                    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
                        info = zipped.getinfo('server/wg0.conf')
                        if info.file_size > 2 * 1024 * 1024:
                            raise ValueError()
                        table_info = zipped.getinfo('server/clientsTable')
                        if table_info.file_size > 2 * 1024 * 1024:
                            raise ValueError()
                        archived_table = parse_table(zipped.read(table_info).decode())
                        recovered = parse_config(zipped.read(info).decode(), {
                            entry['clientId']: entry['userData'].get('clientName', '')
                            for entry in archived_table
                        })
                except (zipfile.BadZipFile, KeyError, RuntimeError, ValueError, OSError):
                    raise ManagerError('Архив не содержит читаемого конфига VPN.') from None
                snapshot = self._read(sid)
                if any(recovered.interface.get(key) != snapshot.config.interface.get(key)
                       for key in ('PrivateKey', 'Address')):
                    raise ManagerError('Архив создан для другого интерфейса или ключа сервера.')
                existing = {peer.public_key for peer in snapshot.config.peers} | self.paused(sid).keys() | snapshot.runtime_peers.keys()
                reserved = [peer.allowed_ips for peer in snapshot.config.peers]
                reserved += [item['ips'] for item in self.paused(sid).values()]
                reserved += [item['runtime_ips'] for item in snapshot.runtime_peers.values() if item.get('runtime_ips')]
                reserved.extend(str(ip_interface(ip.strip()).ip) for ip in snapshot.config.interface['Address'].split(','))
                occupied = [ip_network(ip.strip(), strict=False) for ips in reserved
                            for ip in ips.split(',') if ip.strip()]
                added = []
                conflicts = []
                for peer in recovered.peers:
                    validate_key(peer.public_key)
                    if peer.public_key in existing:
                        continue
                    settings = fields(peer.block)
                    if set(settings) - {'PublicKey', 'PresharedKey', 'AllowedIPs', 'Endpoint', 'PersistentKeepalive'}:
                        raise ManagerError('В архиве есть неподдерживаемые параметры клиента.')
                    if settings.get('PresharedKey'):
                        validate_key(settings['PresharedKey'])
                    try:
                        addresses = [ip_network(ip.strip(), strict=False) for ip in peer.allowed_ips.split(',')]
                    except ValueError:
                        raise ManagerError('В архиве указан неверный адрес клиента.') from None
                    if any(address.overlaps(used) for address in addresses for used in occupied):
                        conflicts.append(peer.name)
                        continue
                    occupied.extend(addresses)
                    name = ' '.join(peer.name.splitlines())[:64] or 'Без имени'
                    clean_block = f'[Peer]\n# {name}\n' + '\n'.join(f'{key} = {value}' for key, value in settings.items())
                    added.append(Peer(peer.public_key, name, peer.allowed_ips, clean_block))
                revision = hashlib.sha256((snapshot.text + '\0' + snapshot.table_text +
                                           json.dumps(self.paused(sid), sort_keys=True) +
                                           json.dumps({key: item.get('runtime_ips') for key, item in snapshot.runtime_peers.items()}, sort_keys=True)).encode()).hexdigest()
                result = {'add': [peer.name for peer in added], 'conflicts': conflicts, 'revision': revision}
                if expected is None:
                    return result
                if revision != expected:
                    raise ManagerError('Конфигурация изменилась. Откройте предпросмотр заново.')
                if not added:
                    raise ManagerError('В архиве нет клиентов, которых можно восстановить.')
                restored_keys = {peer.public_key for peer in added}
                table = [entry for entry in snapshot.table if entry['clientId'] not in restored_keys]
                table.extend({'clientId': peer.public_key, 'userData': {'clientName': peer.name}} for peer in added)
                try:
                    write_peer(self.runner(sid), before=snapshot.text,
                               after=snapshot.config.render(snapshot.config.peers + added),
                               table_before=snapshot.table_text, table_after=json.dumps(table, ensure_ascii=False),
                               interface=Path(self.store.server(sid).config_path).stem,
                               key=added[0].public_key, allowed_ips='', psk='',
                               old_peer='\n\n'.join(peer.block for peer in added),
                               add_peers=[peer.public_key for peer in added])
                finally:
                    self.cache.pop(sid, None)
                self._read(sid)
                return result
            return await self.worker(operation)

    @audited('backup.create')
    async def backup(self, sid: int) -> bytes:
        async with self.lock(sid):
            def operation():
                snapshot = self._read(sid)
                output = io.BytesIO()
                with tempfile.TemporaryDirectory(prefix="awg-backup-") as directory:
                    database = Path(directory) / "manager.sqlite3"
                    self.store.snapshot_database(database)
                    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
                        archive.write(database, "manager.sqlite3")
                        archive.writestr("server/wg0.conf", snapshot.text)
                        archive.writestr("server/clientsTable", snapshot.table_text)
                        archive.writestr("README.txt", "Резервная копия содержит ключи и SSH-реквизиты. Храните приватно.\nSQLite содержит настройки всех подключённых серверов.\n")
                return output.getvalue()
            return await self.worker(operation)

    async def close(self):
        await asyncio.gather(*self.traffic_installs.values())
        for sid in list(self.runners):
            async with self.lock(sid):
                await self.worker(self.runners[sid].close)
