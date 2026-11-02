"""Read-only whole-host utilisation, sampled on demand without installing agents."""
import asyncio
import json
import time

READ = """python3 - <<'HOST_RESOURCES'
import json,os,time
from pathlib import Path
cpu=list(map(int,Path('/proc/stat').read_text().splitlines()[0].split()[1:9]))
mem={line.split(':')[0]:int(line.split()[1])*1024 for line in Path('/proc/meminfo').read_text().splitlines() if ':' in line}
disk=os.statvfs('/')
print(json.dumps(dict(boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),updated=int(time.time()),cpu_total=sum(cpu),cpu_idle=cpu[3]+cpu[4],memory_total=mem['MemTotal'],memory_used=mem['MemTotal']-mem['MemAvailable'],disk_total=disk.f_blocks*disk.f_frsize,disk_used=(disk.f_blocks-disk.f_bfree)*disk.f_frsize)))
HOST_RESOURCES"""


def measure(current, previous=None):
    keys=('updated','cpu_total','cpu_idle','memory_total','memory_used','disk_total','disk_used')
    if any(type(current.get(k)) is not int or current[k]<0 for k in keys):
        raise ValueError('Invalid host counters')
    if not isinstance(current.get('boot'),str) or not current['boot']:
        raise ValueError('Missing boot identity')
    for kind in ('memory','disk'):
        if not 0<=current[kind+'_used']<=current[kind+'_total'] or current[kind+'_total']==0:
            raise ValueError('Invalid capacity')
    if current['cpu_idle']>current['cpu_total']:raise ValueError('Invalid CPU counters')
    cpu=None
    if previous and previous['boot']==current['boot'] and 0 < current['updated']-previous['updated'] <= 20:
        total=current['cpu_total']-previous['cpu_total']
        idle=current['cpu_idle']-previous['cpu_idle']
        if total>0 and 0<=idle<=total:
            cpu=round(100*(total-idle)/total,1)
    return dict(updated=current['updated'],cpu=cpu,interval=current['updated']-previous['updated'] if cpu is not None else None,
                memory={k:current['memory_'+k] for k in ('used','total')},
                disk={k:current['disk_'+k] for k in ('used','total')})


class Resources:
    def __init__(self,store,service):
        self.store,self.service=store,service
        self.lock=asyncio.Lock();self.last=None
        self.previous={};self.values={};self.failed=set()

    async def refresh(self):
        async with self.lock:
            if self.last is not None and time.monotonic()-self.last<5:return
            async def one(s):
                try:
                    async with self.service.lock(s.id):
                        raw=await self.service.worker(lambda:self.service.runner(s.id).run(READ,timeout=5))
                    current=json.loads(raw)
                    value=measure(current,self.previous.get(s.id))
                    self.previous[s.id]=current;self.values[s.id]=value
                    self.failed.discard(s.id)
                except Exception:
                    self.failed.add(s.id)
            await asyncio.gather(*(one(s) for s in self.store.servers()))
            self.last=time.monotonic()

    def get(self,sid):
        value=self.values.get(sid)
        if value is None:return None
        return {**value,'stale':sid in self.failed or time.time()-value['updated']>20}
