import io,json,hashlib,unittest,zipfile,time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock,patch
from manager.updates import candidate,version,unpack,Updates

class UpdateTests(unittest.TestCase):
    def asset(self,tag='v2.0.0-beta.6',**extra):
        return {'id':1,'name':'release.zip','state':'uploaded','size':100,'digest':'sha256:'+'a'*64,'browser_download_url':'https://github.com/wickedslim/amnezia-vpn-manager/releases/download/'+tag+'/release.zip',**extra}
    def test_version_beta_numeric_and_stable(self):
        self.assertGreater(version('v2.0.0-beta.10'),version('2.0.0-beta.9'))
        self.assertGreater(version('2.0.0'),version('2.0.0-beta.99'))
        for bad in ['dev','v2.0.0-beta.6;id','2.0','']:self.assertIsNone(version(bad))
    def test_only_new_published_verified_fixed_repo_assets(self):
        release={'tag_name':'v2.0.0-beta.6','assets':[self.asset()]}
        self.assertEqual(candidate([release],'2.0.0-beta.5')['version'],'2.0.0-beta.6')
        self.assertIsNone(candidate([release],'2.0.0-beta.6'))
        self.assertIsNone(candidate([{**release,'draft':True}],'2.0.0-beta.5'))
        for extra in [{'digest':None},{'digest':'sha256:bad'},{'browser_download_url':'https://evil.example/release.zip'},{'size':30*1024**2}]:
            self.assertIsNone(candidate([{**release,'assets':[self.asset(**extra)]}],'2.0.0-beta.5'))
    def archive(self,files,manifest=None):
        data=io.BytesIO()
        with zipfile.ZipFile(data,'w') as z:
            for n,b in files.items():z.writestr(n,b)
            z.writestr('SHA256SUMS',manifest if manifest is not None else ''.join(hashlib.sha256(b).hexdigest()+'  '+n+'\n' for n,b in files.items()))
        data.seek(0);return data
    def test_unpack_checks_all_files_before_extract(self):
        files={n:b'x' for n in ['install.sh','update.sh','VERSION','awg/bot_manager.py']}
        with TemporaryDirectory() as tmp:
            dest=Path(tmp)/'ok';dest.mkdir();unpack(self.archive(files),dest);self.assertTrue((dest/'update.sh').exists())
            for bad,manifest in [({**files,'../outside':b'x'},None),(files,'0'*64+'  install.sh\n'),({**files,'unsigned':b'x'},''.join(hashlib.sha256(b).hexdigest()+'  '+n+'\n' for n,b in files.items()))]:
                target=Path(tmp)/'bad';target.mkdir(exist_ok=True)
                with self.assertRaises(ValueError):unpack(self.archive(bad,manifest),target)
                self.assertEqual(list(target.iterdir()),[])

class AsyncUpdateTests(unittest.IsolatedAsyncioTestCase):
    async def test_check_cache_and_rate_limit(self):
        updates=Updates('/tmp','2.0.0-beta.5')
        with patch('manager.updates.fetch_releases',return_value=[]) as fetch:
            self.assertFalse((await updates.check())['error']);await updates.check(True);self.assertEqual(fetch.call_count,1)
    async def test_check_failure_is_not_up_to_date(self):
        with patch('manager.updates.fetch_releases',side_effect=OSError()):
            self.assertTrue((await Updates('/tmp','2.0.0-beta.5').check())['error'])
    async def test_expired_or_replayed_confirmation_cannot_launch(self):
        u=Updates('/tmp','2.0.0-beta.5');u.nonces={'old':(time.time()-1,{})}
        for nonce in ['old','old','unknown']:
            with self.assertRaises(ValueError):await u.start(nonce)

class LaunchUpdateTests(unittest.IsolatedAsyncioTestCase):
    async def test_launch_is_independent_and_bound_to_installation(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp)/'install';jobs=Path(tmp)/'jobs';u=Updates(root,'2.0.0-beta.5');r={'version':'2.0.0-beta.6'}
            u.nonces={'yes':(time.time()+60,r)}
            with patch('manager.updates.JOBS',jobs),patch('manager.updates.os.geteuid',return_value=0),patch('manager.updates.sys.platform','linux'),patch('manager.updates.subprocess.check_output',return_value=str(root/'awg')+'\n'),patch('manager.updates.subprocess.run') as run:
                run.return_value.returncode=0
                result=await u.start('yes');self.assertEqual(result['phase'],'queued')
                argv=run.call_args.args[0];self.assertEqual(argv[0],'systemd-run');self.assertIn('--unit=awg-manager-update',argv)
                self.assertTrue(Path(argv[-2]).is_file());self.assertFalse(Path(argv[-2]).is_relative_to(root))
                with self.assertRaises(ValueError):await u.start('yes')
    async def test_wrong_service_directory_cannot_launch(self):
        u=Updates('/correct','2.0.0-beta.5');u.nonces={'yes':(time.time()+60,{})}
        with patch('manager.updates.JOBS',Path('/nonexistent')),patch('manager.updates.os.geteuid',return_value=0),patch('manager.updates.sys.platform','linux'),patch('manager.updates.subprocess.check_output',return_value='/wrong/awg'),patch('manager.updates.subprocess.run') as run:
            with self.assertRaises(ValueError):await u.start('yes')
            run.assert_not_called()
