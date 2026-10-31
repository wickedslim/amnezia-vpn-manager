"""Exercise read-only installer planning with fake systemd, never root mutations."""
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT=Path(__file__).resolve().parents[1]

class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.base=Path(self.tmp.name);self.bin=self.base/'bin';self.bin.mkdir()
        for name,text in {
            'uname':'#!/bin/sh\necho Linux\n',
            'systemctl':'''#!/bin/sh
case "$*" in
*LoadState*) echo "${MOCK_LOAD:-not-found}";;
*WorkingDirectory*) echo "${MOCK_WORKDIR:-}";;
*) exit 99;;
esac
''',
            'apt-get':'#!/bin/sh\necho UNEXPECTED_MUTATION >&2\nexit 99\n',
        }.items():
            p=self.bin/name;p.write_text(text);p.chmod(0o755)
        self.env={**os.environ,'PATH':str(self.bin)+':'+os.environ['PATH'],'TASK_PYTHON':sys.executable,'AWG_INSTALL_DIR':str(self.base/'target')}
    def tearDown(self):self.tmp.cleanup()
    def run_installer(self,*args,env=None):
        return subprocess.run(['bash',str(ROOT/'install.sh'),*args],env=env or self.env,text=True,capture_output=True)
    def test_fresh_bot_only_preflight_does_not_create_target(self):
        r=self.run_installer('--bot-only','--check')
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertIn('интерфейс=bot',r.stdout)
        self.assertFalse((self.base/'target').exists())
    def test_upgrade_keeps_mode_and_existing_data(self):
        target=self.base/'target';target.mkdir();f=target/'marker';f.write_text('keep')
        r=self.run_installer('--upgrade','--check',env={**self.env,'MOCK_LOAD':'loaded','MOCK_WORKDIR':str(target/'awg')})
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertIn('интерфейс=preserve',r.stdout)
        self.assertEqual(f.read_text(),'keep')
    def test_rejects_conflicts_and_wrong_install_mode(self):
        r=self.run_installer('--bot-only','--domain','vpn.example.com','--check')
        self.assertNotEqual(r.returncode,0)
        r=self.run_installer('--upgrade','--check')
        self.assertNotEqual(r.returncode,0)
        r=self.run_installer('--bot-only','--check',env={**self.env,'MOCK_LOAD':'loaded'})
        self.assertNotEqual(r.returncode,0)
