"""Per-client speed limit for the VPN interface, applied with ``tc`` inside the VPN container.

One value limits both directions of a client: traffic to the device (HTB class on the egress
queue of the interface) and traffic from the device (policing on the ingress queue).
Without limits the interface is not touched at all.
"""
from __future__ import annotations

import ipaddress
import re

from .domain import ManagerError

MAX_MBIT = 10000
INTERFACE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,14}")


def desired(clients: list[dict]) -> dict[str, int]:
    """Map the VPN address of each limited client to its limit in Mbit/s."""
    result = {}
    for client in clients:
        speed = client.get("speed_limit")
        if not speed:
            continue
        for item in str(client.get("allowed_ips") or "").replace(",", " ").split():
            try:
                network = ipaddress.ip_network(item, strict=False)
            except ValueError:
                continue
            if network.version == 4 and network.prefixlen == 32:
                result[str(network.network_address)] = int(speed)
                break
    return result


def minor(address: str) -> int:
    return 2 + int(ipaddress.ip_address(address)) % 65000


def check_script(interface: str) -> str:
    return f"""tc qdisc show dev {interface} 2>/dev/null
printf '\\036\\n'
tc class show dev {interface} 2>/dev/null
printf '\\036\\n'
tc filter show dev {interface} parent ffff: 2>/dev/null
"""


def in_sync(output: str, limits: dict[str, int], interface: str) -> bool:
    """True when the interface already carries exactly the wanted classes and filters."""
    try:
        qdiscs, classes, filters = output.split("\x1e\n", 2)
    except ValueError:
        return False
    if not limits:
        return "htb 1:" not in qdiscs and "ingress" not in qdiscs
    if "htb 1:" not in qdiscs or "ingress" not in qdiscs:
        return False
    found = {}
    for match in re.finditer(r"class htb 1:([0-9a-f]+) .*?rate (\S+)", classes):
        found[int(match.group(1), 16)] = match.group(2)
    wanted = {minor(address): speed for address, speed in limits.items()}
    if {key for key in found if key != 1} != set(wanted):
        return False
    for key, speed in wanted.items():
        if _mbit(found[key]) != speed:
            return False
    return len(re.findall(r"police", filters)) >= len(wanted) or filters.count("match") >= len(wanted)


def _mbit(text: str) -> float:
    match = re.fullmatch(r"([0-9.]+)([KMG]?)bit", text)
    if not match:
        return -1
    factor = {"": 1e-6, "K": 1e-3, "M": 1, "G": 1e3}[match.group(2)]
    return round(float(match.group(1)) * factor, 3)


def apply_script(interface: str, limits: dict[str, int]) -> str:
    if not INTERFACE.fullmatch(interface):
        raise ManagerError("Недопустимое имя интерфейса VPN.")
    lines = ["set -eu",
             f"tc qdisc del dev {interface} root 2>/dev/null || true",
             f"tc qdisc del dev {interface} ingress 2>/dev/null || true"]
    if limits:
        lines += [
            f"tc qdisc add dev {interface} root handle 1: htb default 1",
            f"tc class add dev {interface} parent 1: classid 1:1 htb rate 10gbit",
            f"tc qdisc add dev {interface} parent 1:1 fq_codel",
            f"tc qdisc add dev {interface} handle ffff: ingress",
        ]
        for address, speed in sorted(limits.items()):
            if not 1 <= speed <= MAX_MBIT:
                raise ManagerError("Скорость должна быть от 1 до 10000 Мбит/с.")
            ipaddress.IPv4Address(address)
            number = f"{minor(address):x}"
            # Policing drops packets over the burst; TCP only keeps the full rate with a generous burst.
            burst = min(8192, max(256, speed * 50))
            lines += [
                f"tc class add dev {interface} parent 1:1 classid 1:{number} htb rate {speed}mbit ceil {speed}mbit",
                f"tc qdisc add dev {interface} parent 1:{number} fq_codel",
                f"tc filter add dev {interface} parent 1: protocol ip prio 1 u32 match ip dst {address}/32 flowid 1:{number}",
                f"tc filter add dev {interface} parent ffff: protocol ip prio 1 u32 match ip src {address}/32 police rate {speed}mbit burst {burst}k drop flowid :1",
            ]
    return "\n".join(lines) + "\n"
