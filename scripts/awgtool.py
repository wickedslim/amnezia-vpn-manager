#!/usr/bin/env python3
"""Stdlib-only helpers for awg-vpn.sh and awg-move.sh.

They run on a fresh VPS before the manager's environment exists, so only the
standard library is used and the syntax stays compatible with Python 3.8.
"""
import argparse
import configparser
import ipaddress
import json
import random
import re
import sqlite3
import sys
import urllib.parse
import urllib.request

CONTAINER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def obfuscation(rng):
    """AmneziaWG 1.0 parameters in the ranges the AmneziaVPN app uses."""
    s1 = rng.randint(15, 150)
    s2 = rng.randint(15, 150)
    while s1 + 56 == s2:
        s2 = rng.randint(15, 150)
    headers = set()
    while len(headers) < 4:
        headers.add(rng.randint(5, 2147483647))
    h1, h2, h3, h4 = sorted(headers, key=lambda _: rng.random())
    return {"Jc": rng.randint(3, 10), "Jmin": 10, "Jmax": 50, "S1": s1, "S2": s2,
            "H1": h1, "H2": h2, "H3": h3, "H4": h4}


def interface(private, port, subnet, rng):
    network = ipaddress.ip_network(subnet, strict=True)
    if network.version != 4 or network.prefixlen > 29:
        raise ValueError("Нужна IPv4-подсеть не меньше /29")
    if not 1024 <= port <= 65535:
        raise ValueError("Порт должен быть от 1024 до 65535")
    lines = ["[Interface]", "PrivateKey = " + private, "Address = " + str(network), "ListenPort = %d" % port]
    lines += ["%s = %s" % item for item in obfuscation(rng).items()]
    return "\n".join(lines) + "\n\n"


def start_script(subnet):
    """The container start script, as the AmneziaVPN app writes it."""
    network = str(ipaddress.ip_network(subnet, strict=True))
    return """#!/bin/bash

# This scripts copied from Amnezia client to Docker container to /opt/amnezia and launched every time container starts

echo "Container startup"

# kill daemons in case of restart
wg-quick down /opt/amnezia/awg/wg0.conf

# start daemons if configured
if [ -f /opt/amnezia/awg/wg0.conf ]; then (wg-quick up /opt/amnezia/awg/wg0.conf); fi

# Allow traffic on the TUN interface.
iptables -A INPUT -i wg0 -j ACCEPT
iptables -A FORWARD -i wg0 -j ACCEPT
iptables -A OUTPUT -o wg0 -j ACCEPT

# Allow forwarding traffic only from the VPN.
iptables -A FORWARD -i wg0 -o eth0 -s {net} -j ACCEPT
iptables -A FORWARD -i wg0 -o eth1 -s {net} -j ACCEPT

iptables -A FORWARD -m state --state ESTABLISHED,RELATED -j ACCEPT

iptables -t nat -A POSTROUTING -s {net} -o eth0 -j MASQUERADE
iptables -t nat -A POSTROUTING -s {net} -o eth1 -j MASQUERADE

tail -f /dev/null
""".replace("{net}", network)


def config_info(text):
    """Port, subnet and obfuscation values of a server wg0.conf."""
    section, values, peers = None, {}, 0
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            section = line.lower()
            peers += section == "[peer]"
        elif section == "[interface]" and "=" in line and not line.startswith("#"):
            key, value = (part.strip() for part in line.split("=", 1))
            values[key] = value
    port = int(values.get("ListenPort", "0"))
    address = values.get("Address", "").split(",")[0].strip()
    if not address or not port:
        raise ValueError("В wg0.conf нет Address или ListenPort")
    subnet = str(ipaddress.ip_interface(address).network)
    params = {k: v for k, v in values.items() if re.fullmatch(r"Jc|Jmin|Jmax|S\d|H\d", k)}
    return {"port": port, "subnet": subnet, "peers": peers, "params": params}


def create_args(inspect):
    """docker create arguments that reproduce an AmneziaWG container on another host."""
    name = inspect["Name"].lstrip("/")
    if not CONTAINER.match(name):
        raise ValueError("Недопустимое имя контейнера")
    host = inspect["HostConfig"]
    args = ["--name", name, "--restart", (host.get("RestartPolicy") or {}).get("Name") or "always"]
    if host.get("Privileged"):
        args.append("--privileged")
    for cap in host.get("CapAdd") or []:
        args += ["--cap-add", cap]
    for key, value in sorted((host.get("Sysctls") or {}).items()):
        args += ["--sysctl", "%s=%s" % (key, value)]
    for bind in host.get("Binds") or []:
        args += ["-v", bind]
    for device in host.get("Devices") or []:
        args += ["--device", "%s:%s:%s" % (device["PathOnHost"], device["PathInContainer"], device.get("CgroupPermissions") or "rwm")]
    for container_port, bindings in sorted((host.get("PortBindings") or {}).items()):
        for binding in bindings or []:
            ip = binding.get("HostIp") or ""
            args += ["-p", "%s%s:%s" % (ip + ":" if ip else "", binding["HostPort"], container_port)]
    log = (host.get("LogConfig") or {}).get("Type")
    if log:
        args += ["--log-driver", log]
    mode = host.get("NetworkMode") or "bridge"
    if mode not in ("default", "bridge"):
        raise ValueError("Сеть контейнера %s не поддерживается переносом" % mode)
    args.append(inspect["Config"]["Image"])
    return args


def published_udp(inspect):
    ports = []
    for container_port, bindings in ((inspect.get("HostConfig") or {}).get("PortBindings") or {}).items():
        if container_port.endswith("/udp"):
            ports += [int(b["HostPort"]) for b in bindings or [] if b.get("HostPort")]
    return sorted(set(ports))


def servers(path):
    db = sqlite3.connect("file:%s?mode=ro" % urllib.parse.quote(path), uri=True)
    try:
        rows = db.execute("SELECT id, name, config FROM servers ORDER BY id").fetchall()
    finally:
        db.close()
    result = []
    for sid, name, raw in rows:
        config = json.loads(raw)
        result.append({"id": sid, "name": name, "local": bool(config.get("local")),
                       "container": config.get("container") or "amnezia-awg",
                       "endpoint": config.get("endpoint") or "",
                       "endpoint_ip": config.get("endpoint_ip") or "",
                       "endpoints": [row["address"] for row in config.get("endpoints", [])]})
    return result


def set_endpoint(path, old, new):
    """Point local servers that use the old IP at the new one; returns their names."""
    db = sqlite3.connect(path)
    changed = []
    try:
        with db:
            for sid, name, raw in db.execute("SELECT id, name, config FROM servers").fetchall():
                config = json.loads(raw)
                if config.get("local") and old and (config.get("endpoint") == old or config.get("endpoint_ip") == old):
                    if config.get("endpoint") == old:
                        config["endpoint"] = new
                    if config.get("endpoint_ip") == old:
                        config["endpoint_ip"] = new
                    rows = config.get("endpoints", [])
                    if rows:
                        remapped = {}
                        for row in rows:
                            address = new if row["address"] == old else row["address"]
                            if address not in remapped or row["address"] == old:
                                remapped[address] = {**row, "address": address}
                        config["endpoints"] = list(remapped.values())
                    db.execute("UPDATE servers SET config=? WHERE id=?", (json.dumps(config, ensure_ascii=False), sid))
                    changed.append(name)
    finally:
        db.close()
    return changed


def is_ip(value):
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def notify(setting, text):
    ini = configparser.ConfigParser(interpolation=None)
    ini.read(setting)
    token, admin = ini["setting"]["bot_token"], ini["setting"]["admin_id"]
    data = urllib.parse.urlencode({"chat_id": admin, "text": text}).encode()
    with urllib.request.urlopen("https://api.telegram.org/bot%s/sendMessage" % token, data, timeout=15) as response:
        return json.load(response).get("ok") is True


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("interface"); p.add_argument("--private", required=True); p.add_argument("--port", type=int, required=True); p.add_argument("--subnet", default="10.8.1.0/24"); p.add_argument("--seed", type=int)
    p = sub.add_parser("start-script"); p.add_argument("--subnet", default="10.8.1.0/24")
    sub.add_parser("config-info").add_argument("path")
    sub.add_parser("create-args").add_argument("path")
    sub.add_parser("udp-ports").add_argument("path")
    sub.add_parser("servers").add_argument("db")
    p = sub.add_parser("set-endpoint"); p.add_argument("db"); p.add_argument("old"); p.add_argument("new")
    sub.add_parser("is-ip").add_argument("value")
    p = sub.add_parser("notify"); p.add_argument("setting"); p.add_argument("text")
    args = parser.parse_args(argv)
    try:
        if args.command == "interface":
            rng = random.Random(args.seed) if args.seed is not None else random.SystemRandom()
            sys.stdout.write(interface(args.private, args.port, args.subnet, rng))
        elif args.command == "start-script":
            sys.stdout.write(start_script(args.subnet))
        elif args.command == "config-info":
            print(json.dumps(config_info(open(args.path).read())))
        elif args.command in ("create-args", "udp-ports"):
            data = json.load(sys.stdin if args.path == "-" else open(args.path))
            data = data[0] if isinstance(data, list) else data
            values = create_args(data) if args.command == "create-args" else published_udp(data)
            print("\n".join(str(v) for v in values))
        elif args.command == "servers":
            print(json.dumps(servers(args.db), ensure_ascii=False))
        elif args.command == "set-endpoint":
            print("\n".join(set_endpoint(args.db, args.old, args.new)))
        elif args.command == "is-ip":
            return 0 if is_ip(args.value) else 1
        elif args.command == "notify":
            return 0 if notify(args.setting, args.text) else 1
    except (ValueError, KeyError, OSError, sqlite3.Error) as error:
        print("awgtool: %s" % error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
