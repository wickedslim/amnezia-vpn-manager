import unittest
from tempfile import TemporaryDirectory
from pathlib import Path
from manager.access import Access
from manager.domain import ManagerError
from manager.store import Store


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.store=Store(Path(self.tmp.name));self.access=Access(self.store,123)
    def tearDown(self):self.store.close();self.tmp.cleanup()
    def test_grants_persist_owner_cannot_be_removed_and_revoke_clears_wizard(self):
        self.access.grant(123,456,'Друг')
        self.assertTrue(Access(self.store,123).allowed(456))
        self.store.set_wizard(456,{'kind':'search','step':'query','nonce':'test','history':[]})
        self.access.revoke(123,456)
        self.assertFalse(self.access.allowed(456));self.assertIsNone(self.store.wizard(456))
        with self.assertRaises(ManagerError):self.access.revoke(123,123)
        self.assertTrue(self.access.allowed(123))
    def test_only_owner_can_grant_valid_unique_numeric_ids(self):
        for actor,uid,name in [(456,789,'Other'),(123,True,'Bool'),(123,'456','Text'),(123,-1,'Negative'),(123,123,'Owner'),(123,456,'')]:
            with self.subTest(uid=uid),self.assertRaises(ManagerError):self.access.grant(actor,uid,name)
        self.access.grant(123,456,'Друг')
        with self.assertRaises(ManagerError):self.access.grant(123,456,'Again')
        with self.assertRaises(ManagerError):self.access.revoke(456,456)
