import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from manager.domain import Peer
import test_store

class MonthlyTests(unittest.TestCase):
    setUp = test_store.StoreTests.setUp
    tearDown = test_store.StoreTests.tearDown
    def month(self):
        return datetime.now(ZoneInfo('Europe/Samara')).strftime('%Y-%m')

    def test_baseline_is_not_added_to_month(self):
        self.store.sync(self.one.id,[Peer('a','A','ip','',received=1000,sent=2000)],'server')
        row=self.store.monthly_report(self.one.id,self.month())[0]
        self.assertEqual((row['rx'],row['tx']),(0,0))

    def test_month_counts_deltas_and_keeps_deleted(self):
        self.store.sync(self.one.id,[Peer('a','A','ip','',received=100,sent=200)],'server')
        self.store.sync(self.one.id,[Peer('a','A','ip','',received=150,sent=220)],'server')
        self.store.sync(self.one.id,[],'server')
        row=self.store.monthly_report(self.one.id,self.month())[0]
        self.assertEqual((row['rx'],row['tx'],row['present']),(50,20,0))

    def test_month_sorting_and_server_isolation(self):
        peers=[Peer('a','A','ip','',received=0,sent=0),Peer('b','B','ip','',received=0,sent=0)]
        self.store.sync(self.one.id,peers,'server')
        peers[0].received=100; peers[1].sent=200
        self.store.sync(self.one.id,peers,'server')
        self.assertEqual([r['name'] for r in self.store.monthly_report(self.one.id,self.month())],['B','A'])
        self.assertEqual([r['name'] for r in self.store.monthly_report(self.one.id,self.month(),'tx')],['A','B'])
        self.assertEqual(self.store.monthly_report(self.two.id,self.month()),[])
