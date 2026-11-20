"""Owner-confirmed VPS migration through the installed awg-move script."""
import asyncio
import ipaddress
import os
import secrets
import subprocess
import sys
import time

from .domain import ManagerError


UNIT = 'awg-manager-move'


def move_target(host, port):
    if not isinstance(host, str) or len(host) > 45 or '%' in host:
        raise ManagerError('Укажите IP нового VPS.')
    try:
        address = ipaddress.ip_address(host.strip())
    except ValueError:
        raise ManagerError('Укажите IP нового VPS.') from None
    if type(port) is not int or not 1 <= port <= 65535:
        raise ManagerError('Укажите SSH-порт от 1 до 65535.')
    return f'[{address}]:{port}' if address.version == 6 else f'{address}:{port}'


class Moves:
    def __init__(self, root, store):
        self.root = root
        self.store = store
        self.confirmation = None
        self.check_lock = asyncio.Lock()

    async def run(self, argv, timeout=12):
        try:
            return await asyncio.to_thread(subprocess.run, argv, stdin=subprocess.DEVNULL,
                                           capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            raise ManagerError('Команда переноса недоступна или не ответила вовремя. Проверьте старый VPS.') from None

    async def active(self):
        if sys.platform != 'linux':
            return False
        result = await self.run(['systemctl', 'is-active', '--quiet', UNIT + '.service'])
        return result.returncode == 0

    async def prepare(self, sid, host, port):
        target = move_target(host, port)
        server = self.store.server(sid)
        if not server.local:
            raise ManagerError('Запустите перенос в консоли VPS, на котором работает VPN.')
        script = self.root / 'scripts/awg-move.sh'
        if sys.platform != 'linux' or os.geteuid() != 0 or not script.is_file():
            raise ManagerError('Для переноса из панели нужны Linux, systemd, root и установленный awg-move.')
        async with self.check_lock:
            self.confirmation = None
            if await self.active():
                raise ManagerError('Перенос уже выполняется.')
            work = await self.run(['systemctl', 'show', 'awg_bot', '-p', 'WorkingDirectory', '--value'])
            if work.returncode or work.stdout.strip() != str(self.root / 'awg'):
                raise ManagerError('Служба awg_bot не соответствует этой установке.')
            result = await self.run(['bash', str(script), target, '--batch', '--check'], timeout=40)
            if result.returncode:
                raise ManagerError('Проверка VPS не прошла. Проверьте доступ root по SSH и свободные порты. Подробности покажет awg-move --check в консоли старого VPS.')
            nonce = secrets.token_urlsafe(24)
            self.confirmation = (nonce, time.monotonic() + 120, server, target)
            return {'nonce': nonce, 'target': target}

    async def start(self, sid, nonce):
        async with self.check_lock:
            expected = self.confirmation
            if (not expected or not isinstance(nonce, str) or nonce != expected[0]
                    or time.monotonic() >= expected[1] or self.store.server(sid) != expected[2]):
                raise ManagerError('Проверка устарела. Проверьте новый VPS ещё раз.')
            self.confirmation = None
            if await self.active():
                raise ManagerError('Перенос уже выполняется.')
            result = await self.run(['systemd-run', '--unit='+UNIT, '--collect', '--property=Type=exec',
                                     '--property=UMask=0077', '/bin/bash', str(self.root / 'scripts/awg-move.sh'),
                                     expected[3], '--batch', '--yes'])
            if result.returncode:
                raise ManagerError('Не удалось запустить перенос. Проверьте systemd на старом VPS.')
            return {'target': expected[3], 'scheduled': True}
