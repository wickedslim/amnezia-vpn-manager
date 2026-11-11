"""Connection key for the AmneziaVPN app ("vpn://..."), built from a client's .conf.

The app keeps a server as JSON: containers -> amnezia-awg -> awg -> last_config, where
last_config is itself JSON text that carries the original .conf plus its parsed fields. The
key is that JSON, compressed like Qt's qCompress (4-byte big-endian length + zlib) and
base64url-encoded without padding. Every app version imports this form; a compressed bare
.conf is only understood by recent versions. Key names follow the app's configKeys.h.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import re
import struct
import zlib

from .domain import ManagerError

# configKey::awgProtocolKeys() of the app; any of them makes the config AmneziaWG.
AWG_KEYS = ("Jc", "Jmin", "Jmax", "S1", "S2", "S3", "S4", "H1", "H2", "H3", "H4",
            "I1", "I2", "I3", "I4", "I5", "HeaderProtectionKey", "ContentPaddingAddition",
            "RekeyAfterTime", "RekeyTimeout", "RejectAfterTime", "KeepaliveTimeout",
            "MaxHandshakeAttempts", "RandomTrailers", "DisableCookies")


def _fields(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";", "[")) or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def connection(config: str, description: str, public_key: str = "") -> dict:
    """The server entry the app stores after importing this client config."""
    values = _fields(config)
    match = re.fullmatch(r"\[?([^\]]+?)\]?:(\d{1,5})", values.get("Endpoint", ""))
    if not match or not all(values.get(k) for k in ("PrivateKey", "Address", "PublicKey")):
        raise ManagerError("В конфиге клиента нет адреса сервера или ключей.")
    host, port = match.group(1), match.group(2)
    # Like the app: a non-empty parameter makes it AmneziaWG; parameters present but empty
    # (I1 = ) are carried over as empty strings.
    awg = {key: values[key] for key in AWG_KEYS if key in values}
    if not any(awg.values()):
        awg = {}
    protocol, container = ("awg", "amnezia-awg") if awg else ("wireguard", "amnezia-wireguard")
    # The app keeps the bare address here and 1280 as the MTU when the config has none.
    client_ip = values["Address"].split(",")[0].strip().split("/")[0]
    last = {
        "config": config,
        "hostName": host,
        "port": int(port),
        "client_ip": client_ip,
        "client_priv_key": values["PrivateKey"],
        "server_pub_key": values["PublicKey"],
        "allowed_ips": [item for item in re.split(r"\s*,\s*", values.get("AllowedIPs", "")) if item],
        "mtu": values.get("MTU", "1280"),
        **awg,
    }
    psk = values.get("PresharedKey") or values.get("PreSharedKey")
    if psk:
        last["psk_key"] = psk
    if values.get("PersistentKeepalive"):
        last["persistent_keep_alive"] = values["PersistentKeepalive"]
    if public_key:
        last["client_pub_key"] = public_key
        last["clientId"] = public_key
    settings = {"last_config": json.dumps(last, ensure_ascii=False, indent=4),
                "port": port, "transport_proto": "udp", **awg}
    try:
        settings["subnet_address"] = str(ipaddress.ip_interface(f"{client_ip}/24").network.network_address)
    except ValueError:
        pass
    entry = {
        "containers": [{"container": container, protocol: settings}],
        "defaultContainer": container,
        "description": description,
        # Keeps the server name from this key instead of the app's "Server N".
        "nameOverriddenByUser": True,
        "hostName": host,
    }
    dns = [item for item in re.split(r"\s*,\s*", values.get("DNS", "")) if item]
    if dns:
        entry["dns1"] = dns[0]
    if len(dns) > 1:
        entry["dns2"] = dns[1]
    return entry


def vpn_key(config: str, description: str, public_key: str = "") -> str:
    data = json.dumps(connection(config, description, public_key), ensure_ascii=False, indent=4).encode()
    packed = struct.pack(">I", len(data)) + zlib.compress(data, 8)
    return "vpn://" + base64.urlsafe_b64encode(packed).decode().rstrip("=")


def decode(key: str) -> dict:
    """Reverse of vpn_key, the way the app reads it; used by tests and diagnostics."""
    body = key.removeprefix("vpn://")
    packed = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    size = struct.unpack(">I", packed[:4])[0]
    data = zlib.decompress(packed[4:])
    if len(data) != size:
        raise ValueError("length prefix does not match")
    return json.loads(data)
