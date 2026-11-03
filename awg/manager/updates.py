"""Owner-only updates from published releases of the fixed upstream repository.

The worker is copied outside the installation and launched as an independent
systemd unit, so restarting awg_bot cannot interrupt its installer/rollback.
"""
from __future__ import annotations
import asyncio, hashlib, json, os, re, secrets, shutil, subprocess, sys, time, urllib.request, zipfile
from pathlib import Path

REPO='wickedslim/amnezia-vpn-manager'
API=f'https://api.github.com/repos/{REPO}/releases?per_page=50'
JOBS=Path('/var/lib/amnezia-manager/updates')
UNIT='awg-manager-update'
ACTIVE={'queued','downloading','checking','installing','verifying'}

def version(value):
    m=re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)(?:-beta\.(\d+))?',value or '')
    return (*map(int,m.group(1,2,3)),int(m[4]) if m[4] is not None else 10**9) if m else None

def candidate(releases,current):
    valid=[]
    for release in releases:
        v=version(release.get('tag_name'))
        if release.get('draft') or not v or not version(current) or v<=version(current):continue
        tag=release['tag_name'];prefix=f'https://github.com/{REPO}/releases/download/{tag}/'
        for a in release.get('assets',[]):
            digest=a.get('digest') or ''
            if (a.get('name','').endswith('.zip') and a.get('state')=='uploaded' and
                re.fullmatch(r'sha256:[0-9a-f]{64}',digest) and
                a.get('browser_download_url','').startswith(prefix) and 0<a.get('size',0)<=25*1024**2):
                valid.append((v,a.get('id',0),dict(tag=tag,version=tag.removeprefix('v'),url=a['browser_download_url'],sha256=digest[7:],size=a['size'],notes=str(release.get('body') or '')[:50000])))
    return max(valid,key=lambda row:row[:2])[2] if valid else None

def fetch_releases():
    req=urllib.request.Request(API,headers={'User-Agent':'Amnezia-VPN-Manager','Accept':'application/vnd.github+json'})
    with urllib.request.urlopen(req,timeout=8) as response:data=response.read(2*1024**2+1)
    if len(data)>2*1024**2:raise ValueError('release metadata too large')
    result=json.loads(data)
    if not isinstance(result,list):raise ValueError('invalid release metadata')
    return result

def write_status(path,phase,**extra):
    data=dict(phase=phase,updated=time.time(),**extra)
    tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(data));tmp.chmod(0o600);os.replace(tmp,path)

def read_status(path):
    try:return json.loads(path.read_text())
    except (OSError,ValueError):return {'phase':'idle'}

def unpack(archive,dest):
    with zipfile.ZipFile(archive) as z:
        infos=z.infolist()
        if len(infos)>1500 or sum(i.file_size for i in infos)>100*1024**2:raise ValueError('archive too large')
        names=[i.filename for i in infos]
        if len(set(names))!=len(names):raise ValueError('duplicate paths')
        for i in infos:
            p=Path(i.filename)
            if p.is_absolute() or '..' in p.parts or '\\' in i.filename or (i.external_attr>>16)&0o170000==0o120000:raise ValueError('unsafe path')
        entries={}
        for line in z.read('SHA256SUMS').decode().splitlines():
            digest,name=line.split('  ',1)
            if not re.fullmatch('[0-9a-f]{64}',digest) or name in entries:raise ValueError('invalid manifest')
            entries[name]=digest
        if set(entries)!={i.filename for i in infos if not i.is_dir()}-{'SHA256SUMS'}:raise ValueError('incomplete manifest')
        for name,digest in entries.items():
            if hashlib.sha256(z.read(name)).hexdigest()!=digest:raise ValueError('checksum mismatch')
        for name in ['install.sh','update.sh','VERSION','awg/bot_manager.py']:
            if name not in entries:raise ValueError('incomplete release')
        z.extractall(dest)

class Updates:
    def __init__(self,root,current):
        self.root=Path(root).resolve();self.current=current;self.cached=None;self.checked=0;self.lock=asyncio.Lock();self.nonces={}
    async def check(self,force=False):
        async with self.lock:
            age=time.time()-self.checked
            if self.cached is not None and age<(60 if force else 6*3600):return self.cached
            try:
                found=candidate(await asyncio.to_thread(fetch_releases),self.current)
                self.cached=dict(checked=time.time(),current=self.current,release=found,error=False)
            except Exception:
                self.cached=dict(checked=time.time(),current=self.current,release=None,error=True)
            self.checked=time.time();return self.cached
    def status(self):
        status=read_status(JOBS/'status.json')
        if status['phase'] in ACTIVE and time.time()-status.get('updated',0)>45:
            alive=subprocess.run(['systemctl','is-active','--quiet',UNIT],capture_output=True).returncode==0
            if not alive:return {**status,'phase':'interrupted'}
        return status
    async def prepare(self):
        result=await self.check(True)
        if result['error'] or not result['release']:raise ValueError('Нет проверенного обновления.')
        nonce=secrets.token_urlsafe(24);self.nonces={nonce:(time.time()+120,result['release'])}
        return dict(nonce=nonce,version=result['release']['version'])
    async def start(self,nonce):
        async with self.lock:
            confirmation=self.nonces.pop(nonce,None)
            if not confirmation or confirmation[0]<time.time():raise ValueError('Подтверждение устарело. Проверьте обновления ещё раз.')
            if self.status()['phase'] in ACTIVE:raise ValueError('Обновление уже выполняется.')
            if os.geteuid()!=0 or sys.platform!='linux':raise ValueError('Обновление из панели требует Linux, systemd и root.')
            work=subprocess.check_output(['systemctl','show','awg_bot','-p','WorkingDirectory','--value'],text=True).strip()
            if Path(work).resolve()!=self.root/'awg':raise ValueError('Служба awg_bot не соответствует этой установке.')
            JOBS.mkdir(parents=True,exist_ok=True,mode=0o700)
            job=JOBS/secrets.token_hex(12);job.mkdir(mode=0o700)
            plan=dict(root=str(self.root),release=confirmation[1]);(job/'plan.json').write_text(json.dumps(plan));(job/'plan.json').chmod(0o600)
            shutil.copy2(Path(__file__),job/'worker.py')
            write_status(JOBS/'status.json','queued',version=confirmation[1]['version'])
            result=subprocess.run(['systemd-run','--unit='+UNIT,'--collect','--property=UMask=0077',sys.executable,str(job/'worker.py'),str(job)],capture_output=True)
            if result.returncode:
                write_status(JOBS/'status.json','failed',reason='Не удалось запустить службу обновления.')
                raise ValueError('Не удалось запустить службу обновления.')
            return self.status()

def worker(job):
    import fcntl
    job=Path(job);plan=json.loads((job/'plan.json').read_text());r=plan['release'];status=job.parent/'status.json'
    lock=(job.parent/'worker.lock').open('w');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    def mark(phase,**extra):write_status(status,phase,version=r['version'],**extra)
    try:
        mark('downloading');archive=job/'release.zip'
        with urllib.request.urlopen(r['url'],timeout=30) as response,archive.open('wb') as out:
            count=0
            while block:=response.read(65536):
                count+=len(block)
                if count>25*1024**2:raise ValueError('archive too large')
                out.write(block)
        if hashlib.sha256(archive.read_bytes()).hexdigest()!=r['sha256']:raise ValueError('archive checksum mismatch')
        mark('checking');source=job/'source';source.mkdir();unpack(archive,source)
        if (source/'VERSION').read_text().strip()!=r['version']:raise ValueError('version mismatch')
        mark('installing')
        with (job/'installer.log').open('wb') as log:
            result=subprocess.run(['bash',str(source/'update.sh')],stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,env={**os.environ,'AWG_INSTALL_DIR':plan['root'],'AWG_SERVICE_NAME':'awg_bot','TASK_PYTHON':sys.executable})
        if result.returncode:raise RuntimeError('installer failed')
        mark('verifying')
        if subprocess.run(['systemctl','is-active','--quiet','awg_bot']).returncode:raise RuntimeError('service not active')
        if (Path(plan['root'])/'VERSION').read_text().strip()!=r['version']:raise RuntimeError('installed version mismatch')
        mark('complete')
    except Exception:
        # Installer owns transactional rollback. Never display raw logs/exception text.
        mark('failed',reason='Обновление не завершено. Проверьте службу и журнал установщика на сервере; при ошибке замены установщик выполняет откат.')

if __name__=='__main__':worker(sys.argv[1])
