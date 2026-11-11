"""Explicit, short-lived confirmation before scheduling a host reboot."""
import hmac
import secrets
import time

from .domain import ManagerError

# A transient systemd timer lets the HTTP response finish before the host stops.
# systemd fires transient timers with up to a minute of slack by default, so the accuracy is
# tightened to the promised delay. A unit left in the failed state by an earlier attempt would
# block the fixed unit name, so it is reset first.
REBOOT_COMMAND = '''set -eu
if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo -n"; fi
$SUDO systemctl reset-failed awg-manager-reboot.service awg-manager-reboot.timer >/dev/null 2>&1 || true
$SUDO systemd-run --quiet --unit=awg-manager-reboot --on-active=10s --timer-property=AccuracySec=1s /usr/bin/systemctl reboot'''


class Reboots:
    def __init__(self, store, service):
        self.store, self.service = store, service
        self.confirmations = {}

    def prepare(self, sid):
        server = self.store.server(sid)
        if time.time() - self.store.get_pref(f'reboot:{sid}', 0) < 120:
            raise ManagerError('Перезагрузка уже запрошена. Подождите две минуты и проверьте связь.')
        token = secrets.token_urlsafe(24)
        self.confirmations[sid] = (token, time.monotonic() + 120, server)
        return {'nonce': token, 'server': server.name, 'delay': 10}

    async def execute(self, sid, nonce, name):
        async with self.service.lock(sid):
            expected = self.confirmations.get(sid)
            if (not expected or not isinstance(nonce, str) or not hmac.compare_digest(nonce, expected[0])
                    or time.monotonic() > expected[1] or name != expected[2].name):
                raise ManagerError('Подтвердите имя сервера и запросите перезагрузку заново.')
            # Connection changes invalidate confirmation; never reboot a different host.
            if self.store.server(sid) != expected[2]:
                raise ManagerError('Настройки сервера изменились. Запросите подтверждение заново.')
            self.confirmations.pop(sid, None)
            try:
                await self.service.worker(lambda: self.service.runner(sid).run(REBOOT_COMMAND, timeout=12))
            except ManagerError:
                raise ManagerError('Не удалось запланировать перезагрузку. Нужны systemd и root либо sudo без запроса пароля. Проверьте связь с сервером перед повтором.') from None
            self.store.set_pref(f'reboot:{sid}', time.time())
            return {'scheduled': True, 'delay': 10}
