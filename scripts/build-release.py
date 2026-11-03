#!/usr/bin/env python3
"""Build a deterministic source ZIP from an explicit allowlist; never runtime data."""
import argparse,hashlib,json,zipfile
from pathlib import Path

def sources(root):
    allowed={'.py','.js','.json','.css','.html','.md','.sh','.yml','.yaml','.svg','.woff','.woff2','.ttf'}
    files={}
    for directory in ['awg/manager','miniapp','tests','scripts','.github']:
        for p in (root/directory).rglob('*'):
            if p.is_file() and not p.is_symlink() and '__pycache__' not in p.parts and p.suffix in allowed:
                files[p.relative_to(root).as_posix()]=p.read_bytes()
    for p in root.iterdir():
        if p.is_file() and not p.is_symlink() and (p.name in {'README.md','CHANGELOG.md','INSTALL.ru.md','UNINSTALL.ru.md','UPDATE.ru.md','USAGE.ru.md','ADMINS.ru.md','ROLLBACK.ru.md','MINIAPP.ru.md','UI_RULES.md','THIRD_PARTY_NOTICES.md','VALIDATION.ru.md'} or p.name in {'install.sh','uninstall.sh','update.sh','requirements.txt','VERSION','LICENSE','package.json','package-lock.json','playwright.config.js'}):
            files[p.name]=p.read_bytes()
    files['awg/bot_manager.py']=(root/'awg/bot_manager.py').read_bytes()
    return files

def build(root,out):
    files=sources(root)
    hashes={name:hashlib.sha256(data).hexdigest() for name,data in sorted(files.items())}
    digest=hashlib.sha256(json.dumps(hashes,sort_keys=True).encode()).hexdigest()
    info={'version':(root/'VERSION').read_text().strip(),'id':digest[:16],'source_sha256':digest}
    files['BUILD.json']=(json.dumps(info,sort_keys=True,indent=2)+'\n').encode()
    files['SHA256SUMS']=''.join(hashlib.sha256(data).hexdigest()+'  '+name+'\n' for name,data in sorted(files.items())).encode()
    out.mkdir(parents=True,exist_ok=True)
    target=out/f"amnezia-vpn-manager-{info['version']}-{info['id']}.zip"
    with zipfile.ZipFile(target,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        for name,data in sorted(files.items()):
            item=zipfile.ZipInfo(name,date_time=(1980,1,1,0,0,0));item.compress_type=zipfile.ZIP_DEFLATED;item.external_attr=0o100644<<16
            archive.writestr(item,data)
    target.with_suffix('.zip.sha256').write_text(hashlib.sha256(target.read_bytes()).hexdigest()+'  '+target.name+'\n')
    return target,info
if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    target,info=build(Path(__file__).resolve().parents[1],args.output)
    print(json.dumps({'archive':str(target),**info},ensure_ascii=False))
