"""Persistent host-interface totals seeded from Linux's current counters."""
import asyncio
import json
import time
from .domain import ManagerError

READ = '''python3 - <<'HOST_COUNTERS'
import json,subprocess,re,time
from pathlib import Path
routes=json.loads(subprocess.check_output(['ip','-j','route','show','default'],text=True))
routes=sorted((r for r in routes if r.get('dev')),key=lambda r:r.get('metric',0))
if not routes:raise SystemExit(1)
iface=routes[0]['dev']
if not re.fullmatch(r'[a-zA-Z0-9_.:-]{1,64}',iface):raise SystemExit(1)
root=Path('/sys/class/net')/iface/'statistics'
boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
uptime=float(Path('/proc/uptime').read_text().split()[0])
print(json.dumps(dict(interface=iface,boot=boot,rx=int((root/'rx_bytes').read_text()),tx=int((root/'tx_bytes').read_text()),since=int(time.time()-uptime),updated=int(time.time()))))
HOST_COUNTERS'''


def accumulate(previous, current):
    if any(type(current.get(k)) is not int or current[k]<0 for k in ('rx','tx','since','updated')):
        raise ManagerError('Некорректные системные счётчики.')
    if not previous:
        return {**current,'last_rx':current['rx'],'last_tx':current['tx']}
    same=previous['boot']==current['boot'] and previous['interface']==current['interface']
    return {**current,'since':previous['since'],
            'rx':previous['rx']+(current['rx']-previous['last_rx'] if same and current['rx']>=previous['last_rx'] else current['rx']),
            'tx':previous['tx']+(current['tx']-previous['last_tx'] if same and current['tx']>=previous['last_tx'] else current['tx']),
            'last_rx':current['rx'],'last_tx':current['tx']}


class HostStats:
    def __init__(self,store,service):
        self.store,self.service=store,service
        self.lock=asyncio.Lock();self.last=0
    async def refresh(self):
        async with self.lock:
            if time.monotonic()-self.last<60:return
            async def one(s):
                try:
                    async with self.service.lock(s.id):
                        raw=await self.service.worker(lambda:self.service.runner(s.id).run(READ))
                        current=json.loads(raw)
                        key=f'hosttraffic:{s.id}'
                        result=accumulate(self.store.get_pref(key),current)
                        self.store.set_pref(key,result)
                except Exception:
                    # Preserve the last successful total when a host is unreachable.
                    pass
            await asyncio.gather(*(one(s) for s in self.store.servers()))
            self.last=time.monotonic()
    def get(self,sid):
        value=self.store.get_pref(f'hosttraffic:{sid}')
        if not value:return None
        return {k:value[k] for k in ('rx','tx','since','updated','interface')}
