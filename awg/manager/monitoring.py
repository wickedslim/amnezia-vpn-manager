"""Notification acknowledgement is independent from shared observed health."""
import logging
from .views import e

log = logging.getLogger(__name__)

async def notify_health(store, bot, owner):
    for server in store.servers():
        health = store.get_pref(f"health:{server.id}")
        if not health:
            continue
        key = f"health_notified:{server.id}"
        previous = store.get_pref(key, True)
        current = bool(health["ok"])
        if previous != current:
            try:
                await bot.send_message(owner, f"{'🟢 Связь восстановлена' if current else '🔴 Нет связи'}: <b>{e(server.name)}</b>")
            except Exception as exc:
                log.warning("Health notification failed: server=%d type=%s", server.id, type(exc).__name__)
                continue
        store.set_pref(key, current)
