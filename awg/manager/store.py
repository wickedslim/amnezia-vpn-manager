from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from .domain import ManagerError, Peer, Server


class Store:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        self.directory = directory
        self.lock = threading.RLock()
        self.depth = 0
        self.connection = sqlite3.connect(directory / "manager.sqlite3", check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        os.chmod(directory / "manager.sqlite3", 0o600)
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, actor INTEGER,
                source TEXT NOT NULL, request_id TEXT NOT NULL, action TEXT NOT NULL,
                result TEXT NOT NULL, sid INTEGER, cid INTEGER, error TEXT, elapsed INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS audit_ts ON audit_events(ts);
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS servers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL COLLATE NOCASE UNIQUE, config TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS clients (
                id INTEGER PRIMARY KEY AUTOINCREMENT, server_id INTEGER NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
                public_key TEXT NOT NULL, name TEXT NOT NULL, allowed_ips TEXT NOT NULL DEFAULT '',
                config TEXT, expires INTEGER, traffic_limit INTEGER, total_rx INTEGER NOT NULL DEFAULT 0,
                total_tx INTEGER NOT NULL DEFAULT 0, last_rx INTEGER, last_tx INTEGER,
                handshake INTEGER NOT NULL DEFAULT 0, endpoint TEXT NOT NULL DEFAULT '', present INTEGER NOT NULL DEFAULT 1,
                UNIQUE(server_id, public_key));
            CREATE TABLE IF NOT EXISTS rotations (
                client_id INTEGER PRIMARY KEY REFERENCES clients(id) ON DELETE CASCADE,
                old_key TEXT NOT NULL, new_key TEXT NOT NULL, config TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS preferences (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS wizards (user_id INTEGER PRIMARY KEY, value TEXT NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS legacy_configs (public_key TEXT PRIMARY KEY, config TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS legacy_limits (server_id INTEGER NOT NULL REFERENCES servers(id) ON DELETE CASCADE, name TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY(server_id,name));
            CREATE TABLE IF NOT EXISTS samples (server_id INTEGER NOT NULL REFERENCES servers(id) ON DELETE CASCADE, ts INTEGER NOT NULL, rx INTEGER NOT NULL, tx INTEGER NOT NULL, online INTEGER NOT NULL, PRIMARY KEY(server_id,ts));
            CREATE TABLE IF NOT EXISTS daily (client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE, day TEXT NOT NULL, rx INTEGER NOT NULL DEFAULT 0, tx INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(client_id,day));
            CREATE TABLE IF NOT EXISTS monthly (client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE, month TEXT NOT NULL, rx INTEGER NOT NULL DEFAULT 0, tx INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(client_id,month));
            CREATE INDEX IF NOT EXISTS samples_ts ON samples(ts);
            CREATE TABLE IF NOT EXISTS connections (client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE, endpoint TEXT NOT NULL, ts INTEGER NOT NULL, PRIMARY KEY(client_id,endpoint));
        """)
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(clients)")}
        if "speed_limit" not in columns:
            self.connection.execute("ALTER TABLE clients ADD COLUMN speed_limit INTEGER")
        if "last_handshake" not in columns:
            self.connection.execute("ALTER TABLE clients ADD COLUMN last_handshake INTEGER NOT NULL DEFAULT 0")
            self.connection.execute("UPDATE clients SET last_handshake=handshake")
        self.connection.execute("INSERT OR IGNORE INTO preferences VALUES (?,?)", ("monthly_since", json.dumps(int(time.time()))))
        self.connection.execute("INSERT OR IGNORE INTO preferences VALUES (?,?)", ("daily_since", json.dumps(int(time.time()))))
        self.connection.commit()

    @contextmanager
    def transaction(self):
        with self.lock:
            name = f"transaction_{self.depth}"
            self.depth += 1
            self.connection.execute(f"SAVEPOINT {name}")
            try:
                yield self.connection
            except BaseException:
                self.connection.execute(f"ROLLBACK TO {name}")
                self.connection.execute(f"RELEASE {name}")
                raise
            else:
                self.connection.execute(f"RELEASE {name}")
            finally:
                self.depth -= 1

    def audit_record(self,actor,source,request_id,action,result,sid=None,cid=None,error=None,elapsed=0):
        with self.transaction() as conn:
            conn.execute("INSERT INTO audit_events(ts,actor,source,request_id,action,result,sid,cid,error,elapsed) VALUES (?,?,?,?,?,?,?,?,?,?)",(int(time.time()),actor,source,request_id,action,result,sid,cid,error,elapsed))
            conn.execute("DELETE FROM audit_events WHERE ts<?",(int(time.time())-90*86400,))
            conn.execute("DELETE FROM audit_events WHERE id IN (SELECT id FROM audit_events ORDER BY id DESC LIMIT -1 OFFSET 10000)")

    def audit_events(self,before=0,limit=50):
        with self.lock:
            rows=self.connection.execute("SELECT * FROM audit_events WHERE (?=0 OR id<?) ORDER BY id DESC LIMIT ?",(before,before,limit)).fetchall()
        return [dict(row) for row in rows]

    def close(self):
        self.connection.close()

    def get_pref(self, key: str, default=None):
        with self.lock:
            row = self.connection.execute("SELECT value FROM preferences WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_pref(self, key: str, value):
        with self.transaction() as conn:
            conn.execute("INSERT OR REPLACE INTO preferences VALUES (?,?)", (key, json.dumps(value)))

    def servers(self) -> list[Server]:
        with self.lock:
            rows = self.connection.execute("SELECT id,config FROM servers ORDER BY name COLLATE NOCASE").fetchall()
        return [Server(**{**json.loads(r["config"]), "id": r["id"]}) for r in rows]

    def server(self, sid: int) -> Server:
        return next((s for s in self.servers() if s.id == sid), None) or self._missing("Сервер больше не подключён. Откройте главную.")

    @staticmethod
    def _missing(message):
        raise ManagerError(message)

    def add_server(self, server: Server) -> Server:
        values = asdict(server)
        values.pop("id")
        try:
            with self.transaction() as conn:
                for existing in self.servers():
                    if (existing.local, existing.host.casefold(), existing.port, existing.container, existing.config_path) == (server.local, server.host.casefold(), server.port, server.container, server.config_path):
                        raise ManagerError("Этот VPN-сервер уже подключён.")
                cursor = conn.execute("INSERT INTO servers(name,config) VALUES (?,?)", (server.name, json.dumps(values)))
                sid = cursor.lastrowid
        except sqlite3.IntegrityError as exc:
            raise ManagerError("Сервер с таким названием уже есть. Выберите другое название.") from exc
        from .audit import record
        record(self,'server.add','ok',sid)
        return self.server(sid)

    def remove_server(self, sid: int):
        with self.transaction() as conn:
            conn.execute("DELETE FROM servers WHERE id=?", (sid,))

    def update_server(self, server: Server):
        self.server(server.id)
        values = asdict(server)
        values.pop("id")
        try:
            with self.transaction() as conn:
                for other in self.servers():
                    if other.id != server.id and (other.local, other.host.casefold(), other.port, other.container, other.config_path) == (server.local, server.host.casefold(), server.port, server.container, server.config_path):
                        raise ManagerError("Этот VPN-сервер уже подключён.")
                conn.execute("UPDATE servers SET name=?,config=? WHERE id=?", (server.name, json.dumps(values), server.id))
        except sqlite3.IntegrityError as exc:
            raise ManagerError("Сервер с таким названием уже есть.") from exc

    def wizard(self, uid: int):
        with self.lock:
            row = self.connection.execute("SELECT value,updated FROM wizards WHERE user_id=?", (uid,)).fetchone()
        if not row:
            return None
        if time.time() - row["updated"] > 3600:
            self.clear_wizard(uid)
            return None
        return json.loads(row["value"])

    def set_wizard(self, uid: int, value: dict):
        with self.transaction() as conn:
            conn.execute("INSERT OR REPLACE INTO wizards VALUES (?,?,?)", (uid, json.dumps(value), time.time()))

    def clear_wizard(self, uid: int, nonce: str | None = None):
        with self.transaction() as conn:
            if nonce is not None:
                row = conn.execute("SELECT value FROM wizards WHERE user_id=?", (uid,)).fetchone()
                if not row or json.loads(row[0]).get("nonce") != nonce:
                    return
            conn.execute("DELETE FROM wizards WHERE user_id=?", (uid,))

    def clients(self, sid: int, present=True) -> list[dict]:
        with self.lock:
            rows = self.connection.execute("SELECT * FROM clients WHERE server_id=? AND present=? ORDER BY name COLLATE NOCASE,id", (sid, int(present))).fetchall()
        return [dict(r) for r in rows]

    def client(self, sid: int, cid: int) -> dict:
        with self.lock:
            row = self.connection.execute("SELECT * FROM clients WHERE id=? AND server_id=? AND present=1", (cid, sid)).fetchone()
        return dict(row) if row else self._missing("Клиент не найден. Обновите список клиентов.")

    def set_policy(self, sid: int, cid: int, expires: int | None, limit: int | None, speed: int | None = None):
        self.client(sid, cid)
        with self.transaction() as conn:
            conn.execute("UPDATE clients SET expires=?,traffic_limit=?,speed_limit=? WHERE server_id=? AND id=?", (expires, limit, speed, sid, cid))

        from .audit import record
        record(self,"client.policy","ok",sid,cid)

    def prepare_rotation(self, sid: int, cid: int, old_key: str, new_key: str, config: str):
        client = self.client(sid, cid)
        if client['public_key'] != old_key:
            raise ManagerError('Конфиг уже изменился. Откройте карточку заново.')
        with self.transaction() as conn:
            conn.execute('INSERT INTO rotations VALUES (?,?,?,?)', (cid, old_key, new_key, config))

    def reconcile_rotations(self, sid: int, keys: set[str]):
        # Durable journal: a process crash after remote replacement must not create a
        # second client or lose its saved private key / accounting identity.
        with self.transaction() as conn:
            pending = conn.execute('SELECT r.* FROM rotations r JOIN clients c ON c.id=r.client_id WHERE c.server_id=?', (sid,)).fetchall()
            for item in pending:
                old, new = item['old_key'] in keys, item['new_key'] in keys
                if new and not old:
                    conn.execute('UPDATE clients SET public_key=?,config=?,last_rx=0,last_tx=0,handshake=0,endpoint=? WHERE id=?', (item['new_key'], item['config'], '', item['client_id']))
                elif not old or new:
                    raise ManagerError('Не удалось однозначно проверить перевыпуск. Обновите связь с сервером; повторный перевыпуск пока остановлен.')
                conn.execute('DELETE FROM rotations WHERE client_id=?', (item['client_id'],))

    def save_client_config(self, sid: int, key: str, config: str):
        with self.transaction() as conn:
            conn.execute("UPDATE clients SET config=? WHERE server_id=? AND public_key=?", (config, sid, key))

    def sync(self, sid: int, peers: list[Peer], server_public: str):
        from .domain import fields
        now = int(time.time())
        name_counts = Counter(p.name for p in peers)
        with self.transaction() as conn:
            conn.execute("UPDATE clients SET present=0 WHERE server_id=?", (sid,))
            for p in peers:
                previous = conn.execute("SELECT * FROM clients WHERE server_id=? AND public_key=?", (sid, p.public_key)).fetchone()
                if previous:
                    # A reset starts a new counter epoch; count its bytes instead of discarding them.
                    rx = (p.received if previous["last_rx"] is None or p.received < previous["last_rx"] else p.received - previous["last_rx"]) if p.runtime_present else 0
                    tx = (p.sent if previous["last_tx"] is None or p.sent < previous["last_tx"] else p.sent - previous["last_tx"]) if p.runtime_present else 0
                    last_rx = p.received if p.runtime_present else previous["last_rx"]
                    last_tx = p.sent if p.runtime_present else previous["last_tx"]
                    conn.execute("UPDATE clients SET name=?,allowed_ips=?,total_rx=total_rx+?,total_tx=total_tx+?,last_rx=?,last_tx=?,handshake=?,last_handshake=MAX(last_handshake,?),endpoint=?,present=1 WHERE id=?", (p.name, p.allowed_ips, rx, tx, last_rx, last_tx, p.handshake, p.handshake, p.endpoint, previous["id"]))
                else:
                    conn.execute("INSERT INTO clients(server_id,public_key,name,allowed_ips,total_rx,total_tx,last_rx,last_tx,handshake,last_handshake,endpoint) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (sid, p.public_key, p.name, p.allowed_ips, p.received, p.sent, p.received if p.runtime_present else None, p.sent if p.runtime_present else None, p.handshake, p.handshake, p.endpoint))
                month = datetime.fromtimestamp(now, ZoneInfo("Europe/Samara")).strftime("%Y-%m")
                cid = conn.execute("SELECT id FROM clients WHERE server_id=? AND public_key=?", (sid, p.public_key)).fetchone()[0]
                # First observation is a baseline, not historical monthly traffic.
                monthly_rx = rx if previous and previous["last_rx"] is not None else 0
                monthly_tx = tx if previous and previous["last_tx"] is not None else 0
                conn.execute("INSERT INTO monthly VALUES (?,?,?,?) ON CONFLICT(client_id,month) DO UPDATE SET rx=rx+excluded.rx,tx=tx+excluded.tx", (cid, month, monthly_rx, monthly_tx))
                day = datetime.fromtimestamp(now, ZoneInfo("Europe/Samara")).strftime("%Y-%m-%d")
                if p.runtime_present:
                    conn.execute("INSERT INTO daily VALUES (?,?,?,?) ON CONFLICT(client_id,day) DO UPDATE SET rx=rx+excluded.rx,tx=tx+excluded.tx", (cid, day, monthly_rx, monthly_tx))
                imported = conn.execute("SELECT config FROM legacy_configs WHERE public_key=?", (p.public_key,)).fetchone()
                if imported and fields(imported[0].split("[Peer]", 1)[-1]).get("PublicKey") == server_public:
                    conn.execute("UPDATE clients SET config=COALESCE(config,?) WHERE server_id=? AND public_key=?", (imported[0], sid, p.public_key))
                if p.endpoint:
                    cid = conn.execute("SELECT id FROM clients WHERE server_id=? AND public_key=?", (sid, p.public_key)).fetchone()[0]
                    conn.execute("INSERT OR REPLACE INTO connections VALUES (?,?,?)", (cid, p.endpoint, p.handshake or now))
                    conn.execute("DELETE FROM connections WHERE client_id=? AND endpoint NOT IN (SELECT endpoint FROM connections WHERE client_id=? ORDER BY ts DESC LIMIT 50)", (cid, cid))
                legacy = conn.execute("SELECT value FROM legacy_limits WHERE server_id=? AND name=?", (sid, p.name)).fetchone()
                if legacy and name_counts[p.name] == 1:
                    policy = json.loads(legacy[0])
                    delta_rx = (p.received - policy.get("last_rx", 0)) if p.received >= policy.get("last_rx", 0) else p.received
                    delta_tx = (p.sent - policy.get("last_tx", 0)) if p.sent >= policy.get("last_tx", 0) else p.sent
                    migrated_rx = policy.get("total_rx", 0) + (delta_rx if p.runtime_present else 0)
                    migrated_tx = policy.get("total_tx", 0) + (delta_tx if p.runtime_present else 0)
                    conn.execute("UPDATE clients SET expires=?,traffic_limit=?,total_rx=MAX(total_rx,?),total_tx=MAX(total_tx,?) WHERE server_id=? AND public_key=?", (policy.get("expires"), policy.get("traffic_limit"), migrated_rx, migrated_tx, sid, p.public_key))
                    conn.execute("DELETE FROM legacy_limits WHERE server_id=? AND name=?", (sid, p.name))
            totals = conn.execute("SELECT COALESCE(SUM(total_rx),0),COALESCE(SUM(total_tx),0) FROM clients WHERE server_id=?", (sid,)).fetchone()
            conn.execute("INSERT OR REPLACE INTO samples VALUES (?,?,?,?,?)", (sid, now, *totals, sum(p.online for p in peers)))
            conn.execute("DELETE FROM samples WHERE ts<?", (now - 7 * 86400,))

    def server_totals(self, sid: int, day: str, month: str) -> dict:
        """Traffic of all clients of a server for one day and one month (Europe/Samara keys).
        In these tables tx is what devices receive."""
        with self.lock:
            today = self.connection.execute("SELECT COALESCE(SUM(d.rx),0),COALESCE(SUM(d.tx),0) FROM daily d JOIN clients c ON c.id=d.client_id WHERE c.server_id=? AND d.day=?", (sid, day)).fetchone()
            period = self.connection.execute("SELECT COALESCE(SUM(m.rx),0),COALESCE(SUM(m.tx),0) FROM monthly m JOIN clients c ON c.id=m.client_id WHERE c.server_id=? AND m.month=?", (sid, month)).fetchone()
        return {"day_down": today[1], "day_up": today[0], "month_down": period[1], "month_up": period[0]}

    def samples(self, sid: int, limit=20) -> list[dict]:
        with self.lock:
            rows = self.connection.execute("SELECT * FROM samples WHERE server_id=? ORDER BY ts DESC LIMIT ?", (sid, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def snapshot_database(self, destination: Path):
        with sqlite3.connect(self.directory / "manager.sqlite3") as source, sqlite3.connect(destination) as backup:
            source.backup(backup)
        os.chmod(destination, 0o600)

    def connections(self, sid: int, cid: int) -> list[dict]:
        self.client(sid, cid)
        with self.lock:
            rows = self.connection.execute("SELECT endpoint,ts FROM connections WHERE client_id=? ORDER BY ts DESC LIMIT 10", (cid,)).fetchall()
        return [dict(r) for r in rows]

    def pending_imports(self, sid: int) -> int:
        with self.lock:
            return self.connection.execute("SELECT COUNT(*) FROM legacy_limits WHERE server_id=?", (sid,)).fetchone()[0]

    def monthly_report(self, sid: int, month: str, sort="total") -> list[dict]:
        import re
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise ManagerError("Некорректный месяц.")
        order = {"total": "m.rx+m.tx", "rx": "m.tx", "tx": "m.rx"}.get(sort)
        if order is None:
            raise ManagerError("Неизвестная сортировка.")
        with self.lock:
            rows = self.connection.execute(f"SELECT c.id,c.name,c.allowed_ips,c.present,m.rx,m.tx FROM monthly m JOIN clients c ON c.id=m.client_id WHERE c.server_id=? AND m.month=? ORDER BY {order} DESC,c.id", (sid, month)).fetchall()
        return [dict(row) for row in rows]

    def daily_report(self, sid: int, month: str, cid=None) -> dict:
        import calendar
        import re
        if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", month) or not 1 <= int(month[:4]) <= 9999:
            raise ManagerError("Некорректный месяц.")
        server = self.server(sid)
        client = None
        if cid is not None:
            with self.lock:
                client = self.connection.execute("SELECT name,present FROM clients WHERE server_id=? AND id=?", (sid,cid)).fetchone()
            if client is None:
                raise ManagerError("Конфиг не найден на этом сервере.")
        with self.lock:
            rows = self.connection.execute("SELECT d.day,SUM(d.rx) AS rx,SUM(d.tx) AS tx FROM daily d JOIN clients c ON c.id=d.client_id WHERE c.server_id=? AND d.day LIKE ? AND (? IS NULL OR c.id=?) GROUP BY d.day ORDER BY d.day", (sid, month+'-%', cid, cid)).fetchall()
        indexed = {r['day']: r for r in rows}
        days = []
        for number in range(1, calendar.monthrange(int(month[:4]), int(month[5:]))[1]+1):
            day = f"{month}-{number:02d}"
            row = indexed.get(day)
            days.append(dict(day=day, rx=row['tx'] if row else 0, tx=row['rx'] if row else 0, observed=row is not None))
        return dict(month=month, server_id=sid, server_name=server.name, client_id=cid,
                    name=client['name'] if client else server.name, deleted=bool(client and not client['present']),
                    since=self.get_pref('daily_since'), days=days)
