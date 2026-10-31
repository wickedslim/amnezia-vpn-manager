from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .domain import ManagerError, Server, WGConfig, allocate_address, client_config, parse_config, parse_dump, validate_key, validate_name
from .store import Store
from .transport import Runner, WG_SELECT, read_snapshot, write_peer

log = logging.getLogger(__name__)


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


class VPNService:
    def __init__(self, store: Store, runner_factory=Runner):
        self.store = store
        self.runner_factory = runner_factory
        self.runners: dict[int, Runner] = {}
        self.locks: dict[int, asyncio.Lock] = {}
        self.cache: dict[int, Snapshot] = {}
        self.capacity = asyncio.Semaphore(4)

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

    def _read(self, sid: int) -> Snapshot:
        server = self.store.server(sid)
        text, table_text, runtime = read_snapshot(self.runner(sid))
        table = parse_table(table_text)
        names = {c["clientId"]: c["userData"].get("clientName", "") for c in table}
        config = parse_config(text, names)
        interface = Path(server.config_path).stem
        public, peers = parse_dump(runtime, interface)
        for peer in config.peers:
            peer.runtime_present = peer.public_key in peers
            for key, value in peers.get(peer.public_key, {}).items():
                setattr(peer, key, value)
        snapshot = Snapshot(config, text, table_text, table, public, time.time())
        self.store.reconcile_rotations(sid, {p.public_key for p in config.peers})
        self.store.sync(sid, config.peers, public)
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
                address = allocate_address(snapshot.config)
                generated = client_config(snapshot.config, address, private, snapshot.server_public, psk, server.endpoint)
                # Persist the private key before touching the remote server.
                with self.store.transaction() as conn:
                    cursor = conn.execute("INSERT INTO clients(server_id,public_key,name,allowed_ips,config,expires,traffic_limit,present) VALUES (?,?,?,?,?,?,?,0)", (sid, public, name, address, generated, int(time.time()) + duration if duration else None, limit))
                    cid = cursor.lastrowid
                from .domain import Peer
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

    async def delete_client(self, sid: int, cid: int):
        async with self.lock(sid):
            def operation():
                snapshot = self._read(sid)
                client = self.store.client(sid, cid)
                key = client["public_key"]
                peer = next((p for p in snapshot.config.peers if p.public_key == key), None)
                if peer is None:
                    raise ManagerError("Клиент уже удалён с сервера.")
                server = self.store.server(sid)
                remaining = [p for p in snapshot.config.peers if p.public_key != key]
                table = [entry for entry in snapshot.table if entry["clientId"] != key]
                try:
                    write_peer(self.runner(sid), before=snapshot.text, after=snapshot.config.render(remaining), table_before=snapshot.table_text, table_after=json.dumps(table, ensure_ascii=False), interface=Path(server.config_path).stem, key=key, allowed_ips="", psk="", remove=True, old_peer=peer.block)
                finally:
                    self.cache.pop(sid, None)
                self._read(sid)
            await self.worker(operation)

    async def disconnect_server(self, sid: int):
        async with self.lock(sid):
            runner = self.runners.pop(sid, None)
            if runner:
                await self.worker(runner.close)
            self.store.remove_server(sid)
            self.cache.pop(sid, None)

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
                    raise ManagerError('Клиент больше не существует на сервере.')
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
                if expired or exhausted:
                    try:
                        await self.delete_client(sid, client["id"])
                        notifications.append((sid, f"Клиент {client['name']} удалён: {'истёк срок' if expired else 'исчерпан трафик'}."))
                    except ManagerError:
                        log.warning("Не удалось применить ограничение: server=%d client=%d", sid, client["id"])
        return notifications

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
        for sid in list(self.runners):
            async with self.lock(sid):
                await self.worker(self.runners[sid].close)
