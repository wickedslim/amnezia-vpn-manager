#!/usr/bin/env python3
"""Optional per-tunnel traffic accounting. Only aggregates leave the collector."""
import argparse
import hashlib
import ipaddress
import json
import os
import queue
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from collections import defaultdict
from contextlib import closing
from pathlib import Path

POLL_INTERVAL = 15
BUCKET = 300
WINDOWS = {"day": 86400, "week": 7 * 86400}
SERVICE_DOMAINS = {
    "youtube": ("youtube.com", "googlevideo.com", "ytimg.com", "youtu.be", "youtube-nocookie.com", "youtubei.googleapis.com"),
    "telegram": ("telegram.org", "t.me", "telegram.me", "telegram-cdn.org"),
    "instagram": ("instagram.com", "cdninstagram.com"),
    "whatsapp": ("whatsapp.com", "whatsapp.net"),
    "tiktok": ("tiktok.com", "tiktokv.com", "tiktokcdn.com"),
    "vk": ("vk.com", "vk.ru", "userapi.com"),
    "discord": ("discord.com", "discord.gg", "discordapp.com", "discordapp.net"),
    "netflix": ("netflix.com", "nflxvideo.net", "nflximg.net"),
    "spotify": ("spotify.com", "spotifycdn.com", "scdn.co"),
    "twitch": ("twitch.tv", "ttvnw.net", "jtvnw.net"),
    "facebook": ("facebook.com", "facebook.net", "fb.com"),
    "x": ("x.com", "twitter.com", "twimg.com", "t.co"),
    "vk_video": ("vkvideo.ru",),
    "youtube_music": ("music.youtube.com",),
    "yandex_music": ("music.yandex.ru", "music.yandex.com"),
    "soundcloud": ("soundcloud.com", "sndcdn.com"),
    "steam": ("steampowered.com", "steamcontent.com", "steamcommunity.com"),
    "zoom": ("zoom.us", "zoom.com"),
}
DOMAIN_SERVICES = {suffix: name for name, suffixes in SERVICE_DOMAINS.items() for suffix in suffixes}
SERVICE_PROTOCOLS = {name: name for name in SERVICE_DOMAINS if name not in {"vk", "x"}}
SERVICE_PROTOCOLS.update(youtubeupload="youtube", whatsappfiles="whatsapp", whatsappcall="whatsapp",
                         vkontakte="vk", vk="vk", twitter="x")
DPI_GROUPS = {
    "video": "video",
    "music": "music",
    "media": "entertainment",
    "streaming": "entertainment",
    "entertainment": "entertainment",
    "chat": "messages",
    "voip": "messages",
    "socialnetwork": "social",
    "game": "games",
    "email": "work",
    "collaborative": "work",
    "productivity": "work",
    "cloud": "files",
    "filesharing": "files",
    "datatransfer": "files",
    "download": "files",
    "softwareupdate": "updates",
    "shopping": "shopping",
    "news": "news",
}
CATEGORIES = {*SERVICE_DOMAINS, *(f"group_{group}" for group in DPI_GROUPS.values()), "web", "unknown"}
BASE = Path("/var/lib/amnezia-service-traffic")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
PEER = re.compile(r"[A-Za-z0-9+/]{43}=")


def peer_id(key):
    return hashlib.sha256(key.encode()).hexdigest()


def category(application, hostname, confidence, dpi_category=""):
    names = (application or "").lower().split(".")
    protocols = set(names)
    if "dns" in protocols:
        return "unknown"  # A lookup is not a connection to the requested service.
    host = (hostname or "").lower().rstrip(".")
    suffix = host
    while suffix:
        if suffix in DOMAIN_SERVICES:
            return DOMAIN_SERVICES[suffix]
        suffix = suffix.partition(".")[2]
    if confidence >= 3:
        for protocol in reversed(names):
            if protocol in SERVICE_PROTOCOLS:
                return SERVICE_PROTOCOLS[protocol]
        group = DPI_GROUPS.get((dpi_category or "").lower())
        if group:
            return f"group_{group}"
        if protocols & {"http", "http2", "http3"} or (host and protocols & {"tls", "quic"}):
            return "web"
    return "unknown"


def peer_addresses(text):
    addresses = {}
    for line in text.splitlines():
        parts = line.replace(",", " ").split()
        if not parts or not PEER.fullmatch(parts[0]):
            continue
        identity = peer_id(parts[0])
        for item in parts[1:]:
            try:
                network = ipaddress.ip_network(item)
            except ValueError:
                continue
            if network.prefixlen == network.max_prefixlen:
                addresses[str(network.network_address)] = identity
    return addresses


def capture(interface):
    from nfstream import NFStreamer

    socket.if_nametoindex(interface)
    flows = NFStreamer(source=interface, decode_tunnels=False, promiscuous_mode=False,
                       bpf_filter="tcp or udp", n_meters=1, accounting_mode=1,
                       snapshot_length=1536, idle_timeout=15, active_timeout=120,
                       n_dissections=20)
    for flow in flows:
        # The pipe is private. Hostnames and service signatures stay in this process.
        name = category(flow.application_name, flow.requested_server_name,
                        flow.application_confidence or 0, flow.application_category_name)
        row = (flow.src_ip, flow.dst_ip, flow.src2dst_bytes, flow.dst2src_bytes,
               flow.bidirectional_first_seen_ms, flow.bidirectional_last_seen_ms, name)
        print(json.dumps(row), flush=True)


def aggregate(batch, row, addresses):
    src, dst, outbound, inbound, first, last, name = row
    src_peer, dst_peer = addresses.get(src), addresses.get(dst)
    if bool(src_peer) == bool(dst_peer):
        return
    identity, since = src_peer or dst_peer
    if first < since:
        return  # The flow may span a reassignment of the VPN address.
    if dst_peer:
        outbound, inbound = inbound, outbound
    key = (last // (BUCKET * 1000) * BUCKET, identity, name)
    batch[key][0] += inbound
    batch[key][1] += outbound


def prepare(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS counters (
            bucket INTEGER, peer TEXT, category TEXT, rx INTEGER, tx INTEGER,
            PRIMARY KEY (bucket, peer, category));
        CREATE TABLE IF NOT EXISTS status (
            id INTEGER PRIMARY KEY CHECK (id=1), started INTEGER, updated INTEGER);
    """)


def flush(conn, batch, now):
    with conn:
        conn.executemany("""
            INSERT INTO counters VALUES (?,?,?,?,?)
            ON CONFLICT(bucket,peer,category) DO UPDATE SET rx=rx+excluded.rx,tx=tx+excluded.tx
        """, ((*key, *counts) for key, counts in batch.items()))
        conn.execute("DELETE FROM counters WHERE bucket<?", (now - 8 * 86400,))
        conn.execute("""
            INSERT INTO status VALUES (1,?,?)
            ON CONFLICT(id) DO UPDATE SET updated=excluded.updated
        """, (now, now))
    batch.clear()


def report(path, window, peer="", now=None):
    now = int(time.time()) if now is None else now
    start = (now - WINDOWS[window]) // BUCKET * BUCKET
    result = {"state": "disabled", "from": start, "to": now, "rows": []}
    if not path.is_file():
        return result
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as conn, conn:
        conn.execute("BEGIN")
        status = conn.execute("SELECT started,updated FROM status WHERE id=1").fetchone()
        if not status:
            return result
        started, updated = status
        statement = "SELECT category,SUM(rx),SUM(tx) FROM counters WHERE bucket>=? AND bucket<=?"
        args = [start, now]
        if peer:
            statement += " AND peer=?"
            args.append(peer)
        rows = conn.execute(statement + " GROUP BY category ORDER BY SUM(rx)+SUM(tx) DESC,category", args)
        result.update(state="active" if 0 <= now - updated <= 75 else "stale",
                      started=started, updated=updated,
                      rows=[{"category": name, "rx": rx, "tx": tx}
                            for name, rx, tx in rows if name in CATEGORIES and rx + tx])
    return result


def command(*args):
    return subprocess.check_output(args, text=True, timeout=6, stderr=subprocess.DEVNULL).strip()


def container_pid(container):
    pid = int(command("docker", "inspect", "--format", "{{.State.Pid}}", container))
    if pid <= 0:
        raise RuntimeError("Контейнер VPN не запущен")
    return str(pid)


def runtime_peers(container, interface):
    return peer_addresses(command("docker", "exec", container, "sh", "-c",
                                  'if command -v awg >/dev/null; then exec awg show "$1" allowed-ips; fi; exec wg show "$1" allowed-ips',
                                  "sh", interface))


def read_flows(pipe, pending):
    for line in pipe:
        if line.startswith("["):
            try:
                row = json.loads(line)
            except ValueError:
                continue  # NFStream may write informational messages to stdout.
            if isinstance(row, list) and len(row) == 7:
                pending.put(row)


def collect(path, container, interface):
    os.umask(0o077)
    path.parent.mkdir(parents=True, exist_ok=True)
    pid = container_pid(container)
    initial = runtime_peers(container, interface)
    addresses = {address: (identity, 0) for address, identity in initial.items()}
    namespace = ("nsenter", "--target", pid, "--net")
    process = subprocess.Popen(
        [*namespace, sys.executable, "-u", __file__, "capture", interface],
        text=True, stdout=subprocess.PIPE,
    )
    pending = queue.Queue(maxsize=4096)
    threading.Thread(target=read_flows, args=(process.stdout, pending), daemon=True).start()
    batch = defaultdict(lambda: [0, 0])
    try:
        with closing(sqlite3.connect(path)) as conn:
            prepare(conn)
            while process.poll() is None:
                deadline = time.monotonic() + POLL_INTERVAL
                while process.poll() is None and time.monotonic() < deadline:
                    time.sleep(min(1, max(0, deadline - time.monotonic())))
                if process.poll() is not None:
                    break
                if container_pid(container) != pid:
                    raise RuntimeError("Контейнер VPN перезапущен")
                current = runtime_peers(container, interface)
                if process.poll() is not None:
                    break
                changed = int(time.time() * 1000)
                addresses = {address: addresses[address]
                             if address in addresses and addresses[address][0] == identity
                             else (identity, changed) for address, identity in current.items()}
                for _ in range(pending.qsize()):
                    try:
                        row = pending.get_nowait()
                    except queue.Empty:
                        break
                    aggregate(batch, row, addresses)
                if process.poll() is None:
                    flush(conn, batch, int(time.time()))
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    raise RuntimeError("Захват на интерфейсе VPN завершился")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("collect", "capture", "report"))
    parser.add_argument("target")
    parser.add_argument("interface", nargs="?")
    parser.add_argument("--window", choices=WINDOWS, default="day")
    parser.add_argument("--peer", default="")
    args = parser.parse_args()
    if not IDENTIFIER.fullmatch(args.target) or (args.peer and not re.fullmatch(r"[0-9a-f]{64}", args.peer)):
        parser.error("Некорректный идентификатор")
    if args.action == "capture":
        capture(args.target)
        return
    if not args.interface or not IDENTIFIER.fullmatch(args.interface):
        parser.error("Укажите интерфейс VPN")
    path = BASE / args.target / (args.interface + ".sqlite3")
    if args.action == "collect":
        collect(path, args.target, args.interface)
    else:
        print(json.dumps(report(path, args.window, args.peer)))


if __name__ == "__main__":
    main()
