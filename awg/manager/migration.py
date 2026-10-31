from __future__ import annotations

import base64
import json
import logging
import re
from datetime import datetime
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from .domain import ManagerError, Server, fields
from .store import Store

log = logging.getLogger(__name__)


def read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except (ValueError, OSError) as exc:
        raise ManagerError(f"Не удалось прочитать старые настройки: {path.name}. Исходный файл сохранён.") from exc


def traffic_limit(value) -> int | None:
    if not value or value == "Неограниченно":
        return None
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB)", str(value), re.I)
    if not match:
        raise ManagerError("Неизвестный формат лимита трафика в старых настройках.")
    return int(float(match[1]) * 1000 ** ["B", "KB", "MB", "GB", "TB"].index(match[2].upper()))


def migrate(store: Store, legacy: Path):
    if store.get_pref("legacy_imported", False):
        return
    servers = read_json(legacy / "servers.json", {})
    expirations = read_json(legacy / "expirations.json", {})
    # One transaction: a failed import can be retried without partially imported servers.
    with store.transaction() as conn:
        for name, values in servers.items():
            server = store.add_server(Server(
                id=0, name=name, host=values.get("host") or "", port=int(values.get("port") or 22),
                username=values.get("username") or "root", auth_type=values.get("auth_type") or "password",
                password=values.get("_original_password"), key_path=values.get("key_path"),
                container=values.get("docker_container") or "amnezia-awg",
                config_path=values.get("wg_config_file") or "/opt/amnezia/awg/wg0.conf",
                endpoint=values.get("endpoint") or values.get("host") or "", local=values.get("is_remote") != "true",
            ))
            for username, per_server in expirations.items():
                policy = per_server.get(name)
                if not isinstance(policy, dict):
                    continue
                end = policy.get("expiration_time")
                parsed = datetime.fromisoformat(end) if end else None
                if parsed and parsed.tzinfo is None:
                    from datetime import timezone
                    parsed = parsed.replace(tzinfo=timezone.utc)
                stats = read_json(legacy.parent / "users" / username / f"traffic_{name}.json", {}) if Path(username).name == username else {}
                data = {"expires": int(parsed.timestamp()) if parsed else None, "traffic_limit": traffic_limit(policy.get("traffic_limit")), "total_rx": int(stats.get("total_incoming", 0)), "total_tx": int(stats.get("total_outgoing", 0)), "last_rx": int(stats.get("last_incoming", 0)), "last_tx": int(stats.get("last_outgoing", 0))}
                conn.execute("INSERT OR REPLACE INTO legacy_limits VALUES (?,?,?)", (server.id, username, json.dumps(data)))
        for path in (legacy.parent / "users").glob("*/*.conf"):
            try:
                text = path.read_text()
                private = fields(text.split("[Peer]", 1)[0]).get("PrivateKey", "")
                key = X25519PrivateKey.from_private_bytes(base64.b64decode(private, validate=True))
                public = base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
                conn.execute("INSERT OR REPLACE INTO legacy_configs VALUES (?,?)", (public, text))
            except (ValueError, OSError):
                log.warning("Пропущен старый клиентский файл с неверным форматом ключа")
        conn.execute("INSERT OR REPLACE INTO preferences VALUES ('legacy_imported','true')")
    log.info("Импортированы настройки %d серверов; старые файлы сохранены", len(servers))
