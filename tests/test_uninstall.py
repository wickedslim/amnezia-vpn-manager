import os,subprocess,unittest
from pathlib import Path
from tempfile import TemporaryDirectory
ROOT=Path(__file__).resolve().parents[1]
class UninstallTests(unittest.TestCase):
 def setUp(self):
  self.tmp=TemporaryDirectory();self.base=Path(self.tmp.name).resolve();self.bin=self.base/'bin';self.bin.mkdir();self.target=self.base/'manager';(self.target/'awg/manager').mkdir(parents=True);(self.target/'awg/bot_manager.py').write_text('pass')
  scripts={'uname':'#!/bin/sh\necho Linux\n','systemctl':'''#!/bin/sh
case "$*" in
*LoadState*) echo loaded;;
*WorkingDirectory*) echo "$MOCK_WORKDIR";;
*ExecStart*) echo "$MOCK_EXEC";;
*FragmentPath*) echo /etc/systemd/system/awg_bot.service;;
'is-active --quiet awg-manager-update') exit "${MOCK_UPDATING:-1}";;
*) echo UNEXPECTED_MUTATION >&2; exit 99;;
esac
''','docker':'''#!/bin/sh
case "$*" in
'ps -aq') [ -z "$MOCK_MOUNT" ] || echo abc123;;
inspect*) echo "$MOCK_MOUNT";;
*) exit 99;;
esac
'''}
  for n,s in scripts.items():p=self.bin/n;p.write_text(s);p.chmod(0o755)
  self.env={**os.environ,'PATH':str(self.bin)+':'+os.environ['PATH'],'AWG_INSTALL_DIR':str(self.target),'AWG_SERVICE_NAME':'awg_bot','MOCK_WORKDIR':str(self.target/'awg'),'MOCK_EXEC':str(self.target/'awg/bot_manager.py'),'MOCK_MOUNT':''}
 def tearDown(self):self.tmp.cleanup()
 def run_script(self,*args,**env):return subprocess.run(['bash',str(ROOT/'uninstall.sh'),*args],env={**self.env,**env},text=True,capture_output=True)
 def test_check_is_readonly(self):
  before=set(self.base.rglob('*'));r=self.run_script('--check');self.assertEqual(r.returncode,0,r.stderr);self.assertIn('Проверка без изменений',r.stdout);self.assertNotIn('UNEXPECTED_MUTATION',r.stderr);self.assertEqual(set(self.base.rglob('*')),before)
 def test_wrong_service_directory_or_entrypoint_rejected(self):
  for env in [{'MOCK_WORKDIR':'/other/awg'},{'MOCK_EXEC':'/other/bot_manager.py'}]:self.assertNotEqual(self.run_script('--check',**env).returncode,0)
 def test_docker_mount_overlap_rejected(self):
  for m in [str(self.target),str(self.target/'vpn-data'),str(self.base),'/']:
   r=self.run_script('--check',MOCK_MOUNT=m);self.assertNotEqual(r.returncode,0);self.assertIn('Docker mount',r.stderr)
 def test_running_update_rejected(self):
  r=self.run_script('--check',MOCK_UPDATING='0');self.assertNotEqual(r.returncode,0);self.assertIn('обновления',r.stderr)
 def test_invalid_arguments_rejected(self):
  self.assertNotEqual(self.run_script('--yes').returncode,0);self.assertNotEqual(self.run_script('--check',AWG_SERVICE_NAME='awg_bot;id').returncode,0)
