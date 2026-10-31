import unittest
from manager.preferences import normalize_preferences


class PreferencesTests(unittest.TestCase):
    def test_defaults_and_invalid_inputs(self):
        default=normalize_preferences(None)
        self.assertEqual(default['mode'],'auto')
        self.assertEqual(normalize_preferences({'palette':'evil','customAccent':'url(x)','fontSize':True,'mode':'bad'}),default)

    def test_supported_values(self):
        p=normalize_preferences(dict(palette='custom',customAccent='#c5f35b',mode='dark',density='compact',fontSize=16,motion=False))
        self.assertEqual((p['customAccent'],p['fontSize'],p['motion']),('#C5F35B',16,False))
