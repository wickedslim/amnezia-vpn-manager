"""Bounded, on-demand host bandwidth tests, separate from VPN traffic polling."""
import asyncio
import json
import math
import time

from .domain import ManagerError
from . import audit

PROBE = '''if command -v speedtest-cli >/dev/null 2>&1; then printf 'python';
elif command -v speedtest >/dev/null 2>&1; then
 case "$(speedtest --version 2>&1)" in *Ookla*) printf 'ookla';; *speedtest-cli*) printf 'python';; *) printf 'unknown';; esac
else printf 'missing'; fi'''
INSTALL = '''set -eu
[ "$(id -u)" = 0 ] || { echo 'ROOT_REQUIRED' >&2; exit 1; }
command -v apt-get >/dev/null 2>&1 || { echo 'APT_REQUIRED' >&2; exit 1; }
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y speedtest-cli >/dev/null
command -v speedtest-cli >/dev/null'''


def parse_result(raw, tool):
    try:
        data = json.loads(raw)
        if tool == 'ookla':
            download, upload = data['download']['bandwidth']*8, data['upload']['bandwidth']*8
            ping = data['ping']['latency']
        else:
            download, upload, ping = data['download'], data['upload'], data['ping']
        values = [float(download)/1_000_000, float(upload)/1_000_000, float(ping)]
        if not all(math.isfinite(x) and x >= 0 for x in values):
            raise ValueError
        target = data.get('server', {}).get('name', '')
        return dict(download=values[0], upload=values[1], ping=values[2], target=str(target)[:160], ts=int(time.time()))
    except (ValueError, TypeError, KeyError, OverflowError):
        raise ManagerError('Speedtest не вернул корректный результат. Повторите проверку позже.') from None


class Speedtests:
    def __init__(self, store, service):
        self.store, self.service = store, service
        self.tasks, self.statuses, self.started = {}, {}, {}

    def status(self, sid):
        self.store.server(sid)
        return {**self.statuses.get(sid, {'phase':'idle','tool':None}), 'result':self.store.get_pref(f'speedtest:{sid}')}

    def start(self, sid, action):
        self.store.server(sid)
        if action not in {'check','install','run'}:
            raise ManagerError('Неизвестное действие Speedtest.')
        if sid in self.tasks and not self.tasks[sid].done():
            return self.status(sid)
        previous = self.statuses.get(sid, {})
        if action == 'run':
            if time.monotonic()-self.started.get(sid, -1000) < 60:
                raise ManagerError('Между проверками скорости нужна пауза в одну минуту.')
        self.statuses[sid] = {**previous, 'phase':{'check':'checking','install':'installing','run':'checking'}[action], 'error':None}
        self.tasks[sid] = asyncio.create_task(self._job(sid, action))
        return self.status(sid)

    async def _job(self, sid, action):
        audit.record(self.store,'speedtest.'+action,'started',sid)
        error_type=None
        tool = self.statuses[sid].get('tool')
        def operation():
            # Separate SSH channel/session: a long test does not block VPN sampling.
            runner = self.service.runner_factory(self.store.server(sid), self.store.directory)
            try:
                if action == 'install':
                    runner.run(INSTALL, timeout=120)
                    return 'python', None
                if action == 'check':
                    return runner.run(PROBE).strip(), None
                detected=runner.run(PROBE).strip()
                if detected not in {'python','ookla'}:return detected,None
                self.statuses[sid]={'phase':'running','tool':detected,'error':None}
                self.started[sid]=time.monotonic()
                if detected == 'ookla':
                    # Never auto-accept Ookla's licence/GDPR terms.
                    raw = runner.run('speedtest --format=json --progress=no </dev/null', timeout=90)
                else:
                    raw = runner.run('speedtest-cli --secure --json --timeout 15 </dev/null', timeout=90)
                return detected, parse_result(raw, detected)
            finally:
                runner.close()
        try:
            tool, result = await self.service.worker(operation)
            if tool not in {'python','ookla','missing','unknown'}:
                raise ManagerError('Не удалось определить установленный Speedtest.')
            if tool=='unknown':raise ManagerError('Неизвестная утилита Speedtest.')
            if result:
                self.store.set_pref(f'speedtest:{sid}', result)
            self.statuses[sid] = {'phase':'ready','tool':tool,'error':None}
        except asyncio.CancelledError:
            error_type='CancelledError'
            raise
        except Exception as exc:
            error_type=type(exc).__name__
            text = 'Не удалось выполнить Speedtest. Проверьте доступ сервера к тестовым узлам и повторите.'
            if action == 'install':
                text = 'Установка не выполнена. Нужны Ubuntu/Debian, root по SSH и доступ к пакетным репозиториям.'
            elif tool == 'ookla':
                text = 'Ookla не завершил тест. Возможно, на сервере ещё не приняты условия Ookla; автоматически они не принимаются.'
            self.statuses[sid] = {'phase':'error','tool':tool,'error':text}

        finally:
            phase=self.statuses.get(sid,{}).get('phase')
            audit.record(self.store,'speedtest.'+action,'ok' if phase=='ready' else 'failed' if phase=='error' else 'unknown',sid,error=error_type)

    async def close(self):
        for task in self.tasks.values():
            if not task.done():task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
