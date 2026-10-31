from __future__ import annotations

import base64
import ipaddress
import re
import time
from dataclasses import dataclass, field


class ManagerError(Exception):
    """A safe, user-facing error; never include commands, credentials or raw config."""


@dataclass(frozen=True)
class Server:
    id: int
    name: str
    host: str = ""
    port: int = 22
    username: str = "root"
    auth_type: str = "password"
    password: str | None = field(default=None, repr=False)
    key_path: str | None = None
    container: str = "amnezia-awg"
    config_path: str = "/opt/amnezia/awg/wg0.conf"
    endpoint: str = ""
    local: bool = False


@dataclass
class Peer:
    public_key: str
    name: str
    allowed_ips: str
    block: str
    handshake: int = 0
    received: int = 0
    sent: int = 0
    endpoint: str = ""
    runtime_present: bool = True

    @property
    def online(self) -> bool:
        return self.handshake > 0 and 0 <= time.time() - self.handshake <= 180


@dataclass
class WGConfig:
    header: str
    interface: dict[str, str]
    peers: list[Peer]

    def render(self, peers: list[Peer] | None = None) -> str:
        selected = self.peers if peers is None else peers
        return self.header.rstrip() + "\n\n" + "\n\n".join(p.block.rstrip() for p in selected) + "\n"


def fields(text: str) -> dict[str, str]:
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith(("#", ";")) and "=" in line:
            key, value = line.split("=", 1)
            result[key.strip()] = value.split(" #", 1)[0].strip()
    return result


def parse_config(text: str, names: dict[str, str] | None = None) -> WGConfig:
    parts = re.split(r"(?m)^\s*\[Peer\]\s*(?:\r?\n|$)", text)
    interface = fields(parts[0])
    if "PrivateKey" not in interface or "Address" not in interface:
        raise ManagerError("Конфигурация VPN повреждена: отсутствуют PrivateKey или Address.")
    peers = []
    for part in parts[1:]:
        values = fields(part)
        key = values.get("PublicKey", "")
        if not key or any(p.public_key == key for p in peers):
            raise ManagerError("Конфигурация содержит peer без ключа или повторяющийся ключ.")
        comments = [line.strip()[1:].strip() for line in part.splitlines() if line.strip().startswith("#")]
        name = (names or {}).get(key) or (comments[0] if comments else "Без имени")
        peers.append(Peer(key, name, values.get("AllowedIPs", ""), "[Peer]\n" + part.rstrip(), runtime_present=False))
    return WGConfig(parts[0], interface, peers)


def parse_dump(text: str, interface: str) -> tuple[str, dict[str, dict]]:
    public_key = ""
    peers = {}
    header_seen = False
    for line in text.splitlines():
        values = line.split("\t")
        if not values or values[0] != interface:
            continue
        # AmneziaWG appends obfuscation fields to the interface header. The first
        # row for each interface is its header; peer rows keep the standard prefix.
        if not header_seen and len(values) >= 5:
            public_key = values[2]
            header_seen = True
        elif len(values) >= 9:
            try:
                peers[values[1]] = {
                    "endpoint": "" if values[3] == "(none)" else values[3],
                    "handshake": int(values[5]), "received": int(values[6]), "sent": int(values[7]),
                }
            except ValueError as exc:
                raise ManagerError("Сервер вернул некорректные счётчики VPN.") from exc
    if not public_key:
        raise ManagerError("VPN-интерфейс не запущен или формат статистики не поддерживается.")
    return public_key, peers


def validate_name(value: str) -> str:
    value = value.strip()
    if not 1 <= len(value) <= 48 or any(ord(c) < 32 for c in value):
        raise ManagerError("Введите название длиной от 1 до 48 символов без переносов строк.")
    return value


def validate_key(value: str) -> str:
    try:
        if len(base64.b64decode(value, validate=True)) == 32:
            return value
    except (ValueError, TypeError):
        pass
    raise ManagerError("Сервер вернул некорректный ключ VPN.")


def allocate_address(config: WGConfig) -> str:
    addresses = [ipaddress.ip_interface(a.strip()) for a in config.interface["Address"].split(",")]
    subnet = next((a.network for a in addresses if a.version == 4), None)
    if subnet is None or subnet.num_addresses > 65536:
        raise ManagerError("Нужна IPv4-подсеть сервера с маской /16 или больше.")
    occupied = [a.ip for a in addresses if a.version == 4]
    networks = []
    for peer in config.peers:
        for value in peer.allowed_ips.split(","):
            if value.strip():
                networks.append(ipaddress.ip_network(value.strip(), strict=False))
    for candidate in subnet.hosts():
        if candidate not in occupied and not any(candidate in n for n in networks if n.version == 4):
            return f"{candidate}/32"
    raise ManagerError("В подсети сервера нет свободных адресов.")


def client_config(config: WGConfig, address: str, private: str, public: str, psk: str, endpoint: str) -> str:
    port = config.interface.get("ListenPort")
    if not endpoint or not port:
        raise ManagerError("Не задан внешний адрес или порт VPN-сервера.")
    # Keep AmneziaWG parameters; never copy hooks, routes or server private keys.
    extended = {"HeaderProtectionKey", "ContentPaddingAddition", "RekeyAfterTime", "RekeyTimeout", "RejectAfterTime", "KeepaliveTimeout", "MaxHandshakeAttempts", "RandomTrailers", "DisableCookies"}
    parameters = {k: v for k, v in config.interface.items() if re.fullmatch(r"(?:Jc|Jmin|Jmax|S\d+|H\d+|I\d+)", k) or k in extended}
    lines = ["[Interface]", f"Address = {address}", "DNS = 1.1.1.1, 1.0.0.1", f"PrivateKey = {private}"]
    lines.extend(f"{k} = {v}" for k, v in parameters.items())
    host = f"[{endpoint}]" if ":" in endpoint and not endpoint.startswith("[") else endpoint
    lines += ["", "[Peer]", f"PublicKey = {public}", f"PresharedKey = {psk}", "AllowedIPs = 0.0.0.0/0", f"Endpoint = {host}:{port}", "PersistentKeepalive = 25", ""]
    return "\n".join(lines)
