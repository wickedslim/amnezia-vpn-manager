import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import patch
from manager.domain import Peer, ManagerError
import test_store

class DailyTests(unittest.TestCase):
    setUp=test_store.StoreTests.setUp
    tearDown=test_store.StoreTests.tearDown
    def stamp(self,date):return datetime.fromisoformat(date).replace(tzinfo=ZoneInfo('Europe/Samara')).timestamp()
    def sync(self,date,peers):
        with patch('manager.store.time.time',return_value=self.stamp(date)):
            self.store.sync(self.one.id,peers,'server')
    def test_baseline_midnight_reset_and_deleted(self):
        p=Peer('key','Phone','ip','',received=100,sent=200)
        self.sync('2026-11-01T23:59:00',[p])
        p.received=130;p.sent=290;self.sync('2026-11-01T23:59:30',[p])
        p.received=150;p.sent=330;self.sync('2026-11-02T00:00:05',[p])
        p.received=10;p.sent=15;self.sync('2026-11-02T00:01:00',[p])
        cid=self.store.clients(self.one.id)[0]['id']
        self.sync('2026-11-02T00:02:00',[])
        r=self.store.daily_report(self.one.id,'2026-11',cid)
        self.assertEqual(len(r['days']),30)
        self.assertEqual((r['days'][0]['rx'],r['days'][0]['tx']),(90,30))
        self.assertEqual((r['days'][1]['rx'],r['days'][1]['tx']),(55,30))
        self.assertTrue(r['deleted']);self.assertFalse(r['days'][2]['observed'])
        self.assertEqual(sum(d['rx'] for d in r['days']),145)
        self.assertEqual(self.store.monthly_report(self.one.id,'2026-11')[0]['tx'],145)
    def test_validation_isolation_and_leap_year(self):
        self.assertEqual(len(self.store.daily_report(self.one.id,'2028-02')['days']),29)
        for month in ['2026-13','2026-1','0000-01','2026-11%']:
            with self.assertRaises(ManagerError):self.store.daily_report(self.one.id,month)
        p=Peer('key','Phone','ip','',received=0,sent=0)
        self.sync('2026-11-01T12:00:00',[p]);cid=self.store.clients(self.one.id)[0]['id']
        with self.assertRaises(ManagerError):self.store.daily_report(self.two.id,'2026-11',cid)
        self.assertFalse(any(d['observed'] for d in self.store.daily_report(self.two.id,'2026-11')['days']))
    def test_server_aggregates_multiple_peers(self):
        peers=[Peer('a','A','ip','',received=0,sent=0),Peer('b','B','ip','',received=0,sent=0)]
        self.sync('2026-11-01T12:00:00',peers)
        peers[0].sent=20;peers[1].received=30
        self.sync('2026-11-01T12:01:00',peers)
        d=self.store.daily_report(self.one.id,'2026-11')['days'][0]
        self.assertEqual((d['rx'],d['tx']),(20,30))
    def test_missing_runtime_is_not_a_zero_measurement(self):
        p=Peer('key','Phone','ip','',runtime_present=False)
        self.sync('2026-11-01T12:00:00',[p])
        self.assertFalse(any(d['observed'] for d in self.store.daily_report(self.one.id,'2026-11')['days']))
