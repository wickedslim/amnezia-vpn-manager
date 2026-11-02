import hashlib,importlib.util,json,tempfile,unittest,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('release_builder',ROOT/'scripts/build-release.py');builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(builder)
class ReleaseTests(unittest.TestCase):
    def test_reproducible_archive_excludes_runtime_secrets_and_verifies_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'source';root.mkdir();(root/'VERSION').write_text('2.0-test')
            (root/'awg/manager').mkdir(parents=True);(root/'awg/manager/app.py').write_text('pass\n')
            (root/'awg/bot_manager.py').write_text('pass\n');(root/'awg/files').mkdir();(root/'awg/files/setting.ini').write_text('SECRET_TOKEN')
            (root/'DEPLOYMENT.local.md').write_text('SECRET_HOST');(root/'random.md').write_text('PRIVATE_NOTES')
            archive,info=builder.build(root,Path(d)/'out');first=archive.read_bytes();builder.build(root,Path(d)/'out');self.assertEqual(first,archive.read_bytes())
            with zipfile.ZipFile(archive) as z:
                self.assertNotIn('awg/files/setting.ini',z.namelist());self.assertNotIn('DEPLOYMENT.local.md',z.namelist());self.assertNotIn('random.md',z.namelist())
                self.assertEqual(json.loads(z.read('BUILD.json'))['id'],info['id'])
                for line in z.read('SHA256SUMS').decode().splitlines():
                    digest,name=line.split('  ',1);self.assertEqual(hashlib.sha256(z.read(name)).hexdigest(),digest)
