import unittest
from manager.hoststats import accumulate

class HostStatsTests(unittest.TestCase):
    def row(self,rx,tx,boot='a'):
        return dict(rx=rx,tx=tx,boot=boot,interface='eth0',since=100,updated=200)
    def test_initial_kernel_totals_retained(self):
        r=accumulate(None,self.row(100,200));self.assertEqual((r['rx'],r['tx']),(100,200))
    def test_restart_and_reset_preserve_total(self):
        p=accumulate(None,self.row(100,200))
        p=accumulate(p,self.row(130,250));self.assertEqual((p['rx'],p['tx']),(130,250))
        p=accumulate(p,self.row(4,8,'b'));self.assertEqual((p['rx'],p['tx']),(134,258))
        p=accumulate(p,self.row(4,8,'b'));self.assertEqual((p['rx'],p['tx']),(134,258))
