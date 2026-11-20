import json
import sqlite3
import unittest
from tempfile import TemporaryDirectory
from pathlib import Path
from manager.domain import Server, ManagerError, Peer
from manager.endpoints import configure, delivery, configure_endpoints, endpoint_choices
from manager.store import Store

class EndpointTests(unittest.TestCase):
    def test_old_database_derives_choices_and_backfills_saved_config(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            store = Store(path)
            server = store.add_server(Server(0, 'vpn', endpoint='203.0.113.10'))
            store.sync(server.id, [Peer('public', 'client', '10.0.0.2/32', '', endpoint='198.51.100.99:1234')], 'server')
            store.save_client_config(server.id, 'public', '[Peer]\r\nEndpoint = [2001:db8::2]:42080\r\n')
            store.close()
            with sqlite3.connect(path/'manager.sqlite3') as conn:
                conn.execute('ALTER TABLE clients DROP COLUMN config_endpoint')
                conn.execute('UPDATE servers SET config=? WHERE id=?',
                             (json.dumps({'name':'vpn','endpoint':'203.0.113.10','future_field':True}), server.id))
            store = Store(path)
            try:
                self.assertEqual(endpoint_choices(store.server(server.id)), [{'label':'','address':'203.0.113.10'}])
                client = store.clients(server.id)[0]
                self.assertEqual(client['config_endpoint'], '[2001:db8::2]:42080')
                self.assertEqual(client['endpoint'], '198.51.100.99:1234')
            finally:
                store.close()

    def test_address_list_rejects_missing_default_duplicates_and_empty_list(self):
        server = Server(0, 'vpn')
        for rows, default in [
            ([], '203.0.113.10'),
            ([{'address':'203.0.113.10'}], '203.0.113.11'),
            ([{'address':'VPN.example.com'},{'address':'vpn.example.com.'}], 'VPN.example.com'),
        ]:
            with self.subTest(rows=rows),self.assertRaises(ManagerError):
                configure_endpoints(server, rows, default)

    def test_legacy_addresses_and_two_saved_choices(self):
        server = Server(0, 'vpn', host='198.51.100.1', endpoint='198.51.100.2')
        self.assertEqual(delivery(server), {'mode':'ip','ip':'198.51.100.2','dns':''})
        server = configure(server, {'mode':'dns','ip':'198.51.100.2','dns':'VPN.Example.com.'})
        self.assertEqual(server.endpoint, 'vpn.example.com')
        server = configure(server, {**delivery(server), 'mode':'ip'})
        self.assertEqual(server.endpoint, '198.51.100.2')
        self.assertEqual(server.endpoint_dns, 'vpn.example.com')
        with TemporaryDirectory() as d:
            store=Store(Path(d));server=store.add_server(server)
            self.assertEqual(delivery(store.server(server.id))['dns'],'vpn.example.com')
            store.close()

    def test_rejects_urls_ports_ip_as_domain_and_malformed_names(self):
        server=Server(0,'vpn')
        for domain in ('https://vpn.example.com','vpn.example.com:443','example.com/path','1.1.1.1','bad_.example','-bad.example','a..example','localhost'):
            with self.subTest(domain=domain),self.assertRaises(ManagerError):
                configure(server, {'mode':'dns','ip':'','dns':domain})
        for body in ({'mode':'other'}, {'mode':'dns','dns':''},{'mode':'ip','ip':'vpn.example.com'}, {'mode':'dns','dns':[]}, []):
            with self.subTest(body=body),self.assertRaises(ManagerError):configure(server,body)

    def test_ipv6_remains_an_ip(self):
        server=configure(Server(0,'vpn'),{'mode':'ip','ip':'2001:db8::1','dns':''})
        self.assertEqual(delivery(server)['mode'],'ip')
