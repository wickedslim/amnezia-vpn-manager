from collections import defaultdict
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from scripts.service_traffic import Continuations, aggregate, category, flush, peer_addresses, peer_id, prepare, report


class ServiceTrafficTests(unittest.TestCase):
    def test_service_signals_and_generic_web(self):
        for application, host, confidence, expected in [
            ("TLS", "rr2.googlevideo.com", 6, "youtube"),
            ("QUIC.YouTube", "", 6, "youtube"),
            ("Telegram", "", 6, "telegram"),
            ("QUIC", "scontent.cdninstagram.com", 6, "instagram"),
            ("TLS", "instagram.com.example.org", 6, "web"),
            ("QUIC", "", 6, "unknown"),
            ("TLS", "example.org", 1, "unknown"),
            ("DNS.YouTube", "youtube.com", 6, "unknown"),
            ("TLS", "mmg.whatsapp.net", 6, "whatsapp"),
            ("QUIC", "video.tiktokcdn.com", 6, "tiktok"),
            ("TLS.VKontakte", "", 6, "vk"),
            ("TLS", "video.vkvideo.ru", 6, "vk_video"),
            ("QUIC.Discord", "", 6, "discord"),
            ("TLS", "stream.nflxvideo.net", 6, "netflix"),
            ("TLS.Spotify", "", 6, "spotify"),
            ("TLS", "video-weaver.ttvnw.net", 6, "twitch"),
            ("TLS.Facebook", "", 6, "facebook"),
            ("TLS.Twitter", "", 6, "x"),
        ]:
            with self.subTest(application=application, host=host):
                self.assertEqual(category(application, host, confidence), expected)

    def test_named_service_precedes_the_broad_category_and_weak_signals_stay_unknown(self):
        self.assertEqual(category("TLS.YouTube", "music.youtube.com", 6, "Video"), "youtube_music")
        self.assertEqual(category("TLS", "soundcloud.com", 6, "Streaming"), "soundcloud")
        self.assertEqual(category("TLS.OtherService", "", 6, "Music"), "group_music")
        self.assertEqual(category("TLS.OtherService", "", 6, "Streaming"), "group_entertainment")
        self.assertEqual(category("TLS.OtherService", "", 6, "SoftwareUpdate"), "group_updates")
        self.assertEqual(category("TLS", "", 1, "Music"), "unknown")

    def test_pieces_of_a_long_connection_keep_the_service(self):
        # NFStream cuts a connection every two minutes; later pieces have no server name and
        # may start with a server packet, so the ends come swapped.
        pieces = Continuations()
        client, server = ("10.8.1.7", 51000), ("142.250.74.110", 443)
        self.assertEqual(pieces.label(6, *client, *server, 0, 119_000, True, "youtube"), "youtube")
        self.assertEqual(pieces.label(6, *server, *client, 120_000, 239_000, True, "unknown"), "youtube")
        self.assertEqual(pieces.label(6, *server, *client, 240_000, 300_000, False, "unknown"), "youtube")
        # The connection has ended: the same ports later are a new connection.
        self.assertEqual(pieces.label(6, *client, *server, 301_000, 302_000, False, "unknown"), "unknown")
        self.assertEqual(pieces.known, {})

    def test_unrelated_or_late_pieces_do_not_inherit(self):
        pieces = Continuations()
        client, server = ("10.8.1.7", 51000), ("142.250.74.110", 443)
        pieces.label(6, *client, *server, 0, 119_000, True, "youtube")
        self.assertEqual(pieces.label(17, *client, *server, 120_000, 130_000, False, "unknown"), "unknown")
        self.assertEqual(pieces.label(6, "10.8.1.8", 51000, *server, 120_000, 130_000, False, "unknown"), "unknown")
        self.assertEqual(pieces.label(6, *client, *server, 119_000 + Continuations.GAP_MS + 1, 200_000, False, "unknown"), "unknown")
        # An unidentified first piece does not mask what a later piece recognises.
        pieces.label(6, *client, *server, 0, 119_000, True, "unknown")
        self.assertEqual(pieces.label(6, *client, *server, 120_000, 130_000, False, "web"), "web")

    def test_continuation_memory_is_bounded(self):
        pieces = Continuations(limit=4)
        for port in range(10):
            pieces.label(6, "10.8.1.7", port, "1.1.1.1", 443, 0, port * 60_000, True, "web")
        self.assertLessEqual(len(pieces.known), 4)

    def test_peer_scope_direction_period_and_staleness(self):
        first, second = "A" * 43 + "=", "B" * 43 + "="
        raw = peer_addresses(f"{first}\t10.8.1.2/32, fd00::2/128\n{second}\t10.8.1.3/32, 10.20.0.0/24")
        self.assertEqual(raw, {"10.8.1.2": peer_id(first), "fd00::2": peer_id(first),
                               "10.8.1.3": peer_id(second)})
        now = 1_800_000_000
        addresses = {ip: (key, 0) for ip, key in raw.items()}
        addresses["10.8.1.3"] = (peer_id(second), now * 1000 - 1000)
        batch = defaultdict(lambda: [0, 0])
        remote = "198.51.100.2"
        for src, dst, sent, returned, stamp, name in [
            ("10.8.1.2", remote, 10, 500, now, "youtube"),
            (remote, "fd00::2", 300, 20, now, "youtube"),
            ("10.8.1.3", remote, 10, 100, now, "unknown"),
            ("10.8.1.3", remote, 1000, 1000, now - 2, "unknown"),
            ("10.8.1.2", "10.8.1.3", 1000, 1000, now, "unknown"),
            ("10.8.1.2", remote, 1, 42, now - 2 * 86400, "web"),
            ("10.8.1.2", remote, 1, 42, now - 9 * 86400, "web"),
        ]:
            aggregate(batch, (src, dst, sent, returned, stamp * 1000, stamp * 1000, name), addresses)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "traffic.sqlite3"
            self.assertEqual(report(path, "day", now=now)["state"], "disabled")
            with closing(sqlite3.connect(path)) as conn:
                prepare(conn)
                flush(conn, batch, now)
                self.assertFalse(batch)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM counters").fetchone()[0], 3)
                flush(conn, batch, now + 15)
            day = report(path, "day", now=now + 15)
            self.assertEqual(day["state"], "active")
            self.assertEqual(day["started"], now)
            self.assertEqual(day["rows"], [
                {"category": "youtube", "rx": 800, "tx": 30},
                {"category": "unknown", "rx": 100, "tx": 10},
            ])
            week = report(path, "week", peer=peer_id(first), now=now + 100)
            self.assertEqual(week["state"], "stale")
            self.assertEqual([row["category"] for row in week["rows"]], ["youtube", "web"])
