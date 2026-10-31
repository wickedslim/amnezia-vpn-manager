import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from manager.domain import ManagerError, Peer, Server
from manager.migration import migrate
from manager.store import Store
from helpers import keypair


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "db")
        self.one = self.store.add_server(Server(0,"one",host="one"))
        self.two = self.store.add_server(Server(0,"two",host="two"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_same_name_across_servers_is_isolated(self):
        self.store.sync(self.one.id, [Peer("key", "name", "10.0.0.2/32", "", received=100)], "server")
        self.store.sync(self.two.id, [Peer("key", "name", "10.0.0.2/32", "", received=200)], "server")
        a, b = self.store.clients(self.one.id)[0], self.store.clients(self.two.id)[0]
        self.assertNotEqual(a["id"], b["id"])
        self.assertEqual((a["total_rx"],b["total_rx"]), (100,200))
        with self.assertRaises(ManagerError):
            self.store.client(self.two.id, a["id"])

    def test_counter_reset_counts_new_epoch(self):
        for count in (100,150,20,30):
            self.store.sync(self.one.id, [Peer("key","name","ip","",received=count)], "server")
        self.assertEqual(self.store.clients(self.one.id)[0]["total_rx"], 180)

    def test_same_sample_is_not_counted_twice(self):
        for _ in range(3):
            self.store.sync(self.one.id, [Peer("key","name","ip","",received=100)], "server")
        self.assertEqual(self.store.clients(self.one.id)[0]["total_rx"], 100)

    def test_missing_runtime_peer_does_not_reset_counter_baseline(self):
        self.store.sync(self.one.id,[Peer("key","name","ip","",received=100)],"server")
        self.store.sync(self.one.id,[Peer("key","name","ip","",runtime_present=False)],"server")
        self.store.sync(self.one.id,[Peer("key","name","ip","",received=110)],"server")
        self.assertEqual(self.store.clients(self.one.id)[0]["total_rx"],110)

    def test_old_save_cannot_clear_new_dialog(self):
        self.store.set_wizard(42,{"nonce":"new","step":"name"})
        self.store.clear_wizard(42,nonce="old")
        self.assertEqual(self.store.wizard(42)["nonce"],"new")

    def test_update_server_keeps_clients_and_identity(self):
        self.store.sync(self.one.id,[Peer("key","name","ip","")],"server")
        self.store.update_server(Server(self.one.id,"renamed",host="newhost"))
        self.assertEqual(self.store.server(self.one.id).name,"renamed")
        self.assertEqual(len(self.store.clients(self.one.id)),1)

    def test_connection_history_is_scoped_to_client(self):
        self.store.sync(self.one.id,[Peer("key","name","ip","",handshake=100,endpoint="203.0.113.1:1234")],"server")
        client = self.store.clients(self.one.id)[0]
        self.assertEqual(self.store.connections(self.one.id,client["id"])[0]["endpoint"],"203.0.113.1:1234")
        with self.assertRaises(ManagerError):
            self.store.connections(self.two.id,client["id"])

    def test_wizard_survives_reopen(self):
        self.store.set_wizard(42, {"step":"auth","nonce":"123","name":"server"})
        self.store.close()
        self.store = Store(self.root / "db")
        self.assertEqual(self.store.wizard(42)["step"], "auth")

    def test_remove_only_selected_server(self):
        for server in (self.one,self.two):
            self.store.sync(server.id, [Peer("key","name","ip", "")], "server")
        self.store.remove_server(self.one.id)
        self.assertEqual(len(self.store.clients(self.two.id)), 1)

    def test_server_ids_are_not_reused(self):
        previous = self.two.id
        self.store.remove_server(previous)
        new = self.store.add_server(Server(0,"three",host="three"))
        self.assertGreater(new.id, previous)

    def test_duplicate_server_does_not_overwrite(self):
        with self.assertRaises(ManagerError):
            self.store.add_server(Server(0,"one",host="different"))
        self.assertEqual(self.store.server(self.one.id).host, "one")

    def test_nested_transactions_rollback_atomically(self):
        with self.assertRaises(ValueError):
            with self.store.transaction():
                self.store.add_server(Server(0,"three",host="three"))
                raise ValueError("abort")
        self.assertEqual(len(self.store.servers()), 2)

    def test_backup_is_valid_database(self):
        path = self.root / "backup.sqlite3"
        self.store.snapshot_database(path)
        with sqlite3.connect(path) as conn:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_failed_migration_is_atomic_and_retryable(self):
        legacy = self.root / "legacy"
        legacy.mkdir()
        (legacy / "servers.json").write_text(json.dumps({"imported":{"host":"imported","is_remote":"true"},"bad":{"port":"invalid"}}))
        with self.assertRaises(ValueError):
            migrate(self.store, legacy)
        self.assertEqual(len(self.store.servers()),2)
        self.assertFalse(self.store.get_pref("legacy_imported",False))

    def test_import_does_not_modify_originals_and_is_idempotent(self):
        legacy = self.root / "legacy"
        legacy.mkdir()
        original = json.dumps({"serv.host":{"host":"host","port":22,"is_remote":"true","_original_password":"secret"}})
        (legacy / "servers.json").write_text(original)
        (legacy / "expirations.json").write_text(json.dumps({"client":{"serv.host":{"expiration_time":"2030-01-01T00:00:00+00:00","traffic_limit":"5 GB"}}}))
        migrate(self.store, legacy)
        migrate(self.store, legacy)
        self.assertEqual(len(self.store.servers()),3)
        self.assertEqual((legacy / "servers.json").read_text(), original)
        self.assertNotIn("secret",repr(self.store.servers()[-1]))

    def test_imported_config_requires_both_client_and_server_key_match(self):
        private, public = keypair()
        _, server_public = keypair()
        legacy = self.root / "files"
        legacy.mkdir()
        (legacy / "servers.json").write_text(json.dumps({"imported":{"host":"imported","is_remote":"true"}}))
        user = self.root / "users" / "alice"
        user.mkdir(parents=True)
        config = f"[Interface]\nPrivateKey = {private}\n[Peer]\nPublicKey = {server_public}\n"
        (user / "alice.conf").write_text(config)
        migrate(self.store,legacy)
        server = next(s for s in self.store.servers() if s.name == "imported")
        self.store.sync(server.id,[Peer(public,"alice","ip","")],"wrong-server-key")
        self.assertIsNone(self.store.clients(server.id)[0]["config"])
        self.store.sync(server.id,[Peer(public,"alice","ip","")],server_public)
        self.assertEqual(self.store.clients(server.id)[0]["config"],config)

    def test_migrated_traffic_adds_only_delta_since_old_sample(self):
        policy = {"expires":None,"traffic_limit":None,"total_rx":1000,"total_tx":2000,"last_rx":100,"last_tx":200}
        with self.store.transaction() as conn:
            conn.execute("INSERT INTO legacy_limits VALUES (?,?,?)", (self.one.id,"name",json.dumps(policy)))
        self.store.sync(self.one.id,[Peer("key","name","ip","",received=150,sent=220)],"server")
        client = self.store.clients(self.one.id)[0]
        self.assertEqual((client["total_rx"],client["total_tx"]),(1050,2020))

    def test_ambiguous_legacy_policy_is_not_assigned_to_wrong_peer(self):
        with self.store.transaction() as conn:
            conn.execute("INSERT INTO legacy_limits VALUES (?,?,?)", (self.one.id,"name",json.dumps({"expires":1,"traffic_limit":10})))
        self.store.sync(self.one.id,[Peer("a","name","ip",""),Peer("b","name","ip","")],"server")
        self.assertTrue(all(c["expires"] is None for c in self.store.clients(self.one.id)))
        self.assertEqual(self.store.pending_imports(self.one.id),1)
