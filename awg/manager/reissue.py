"""Single-use confirmations bound to the exact client key being replaced."""
import hmac
import secrets
import time
from .domain import ManagerError


class Reissues:
    def __init__(self, store, service):
        self.store, self.service = store, service
        self.pending = {}

    def prepare(self, sid, cid):
        client = self.store.client(sid, cid)
        token = secrets.token_urlsafe(12)
        self.pending[(sid, cid)] = (token, time.monotonic() + 120, client['public_key'])
        return {'nonce': token, 'name': client['name'], 'server': self.store.server(sid).name}

    async def execute(self, sid, cid, token):
        expected = self.pending.get((sid, cid))
        if not expected or not isinstance(token, str) or not hmac.compare_digest(expected[0], token) or time.monotonic() > expected[1]:
            raise ManagerError('Подтверждение истекло или уже использовано. Откройте карточку и повторите действие.')
        self.pending.pop((sid, cid), None)
        return await self.service.reissue_client(sid, cid, expected[2])
