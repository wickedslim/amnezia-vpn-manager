from __future__ import annotations

import html
import time
from datetime import datetime, timezone

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

e = html.escape


def keyboard(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
        for row in rows if row
    ])


def size(value: int | float) -> str:
    number = float(value)
    for unit in ("Б", "Кб", "Мб", "Гб", "Тб"):
        if abs(number) < 1024 or unit == "Тб":
            return f"{number:.1f} {unit}" if unit != "Б" else f"{number:.0f} Б"
        number /= 1024


def ago(timestamp: int | float) -> str:
    if not timestamp:
        return "ещё не подключался"
    delta = max(0, int(time.time() - timestamp))
    if delta < 60:
        return "только что"
    if delta < 3600:
        return f"{delta // 60} мин назад"
    if delta < 86400:
        return f"{delta // 3600} ч назад"
    return f"{delta // 86400} дн назад"


def progress(used: int, limit: int | None) -> str:
    if not limit:
        return "Без ограничения"
    ratio = min(1, max(0, used / limit))
    blocks = round(ratio * 10)
    return f"{'▰' * blocks}{'▱' * (10 - blocks)} {ratio:.0%}\n{size(used)} / {size(limit)}"


def sparkline(values: list[int | float]) -> str:
    if len(values) < 2:
        return "История появится после следующих обновлений"
    bars = "▁▂▃▄▅▆▇█"
    top = max(values)
    return "".join(bars[min(7, int(v / top * 7))] if top else bars[0] for v in values)


def rates(samples: list[dict]) -> tuple[float, float]:
    if len(samples) < 2:
        return 0, 0
    first, last = samples[-2:]
    seconds = last["ts"] - first["ts"]
    if seconds <= 0:
        return 0, 0
    return max(0, last["rx"] - first["rx"]) / seconds, max(0, last["tx"] - first["tx"]) / seconds


def client_status(client: dict) -> tuple[str, str]:
    handshake = client["handshake"]
    if handshake > 0 and 0 <= time.time() - handshake <= 180:
        return "🟢", "В сети"
    if handshake or client.get("last_handshake") or client["total_rx"] or client["total_tx"]:
        return "🔴", "Не в сети"
    return "⚪", "Нет истории подключения"


def client_text(server_name: str, client: dict) -> str:
    icon, status = client_status(client)
    last_handshake = max(client["handshake"], client.get("last_handshake", 0))
    last_seen = ago(last_handshake) if last_handshake else "Нет данных"
    used = client["total_rx"] + client["total_tx"]
    end = client["expires"]
    remaining = "Без срока" if end is None else (f"Осталось {max(0, int((end-time.time()) / 3600))} ч" if end > time.time() else "Срок истёк")
    expiry = "" if end is None else f"\nДо {datetime.fromtimestamp(end, timezone.utc):%d.%m.%Y %H:%M} UTC"
    return (
        f"<b>{e(client['name'])}</b>\n{e(server_name)} › Клиенты\n\n"
        f"{icon} {status}\nПоследнее соединение: {last_seen}\n"
        f"VPN-адрес: <code>{e(client['allowed_ips'] or '—')}</code>\n\n"
        f"<b>Трафик клиента · накопительный</b>\n↓ На устройство {size(client['total_tx'])}\n"
        f"↑ С устройства {size(client['total_rx'])}\nВсего: <b>{size(used)}</b>\n\n"
        f"С начала учёта, включая начальные счётчики VPN.\n\n<b>Лимит</b>\n{progress(used, client['traffic_limit'])}\n\n"
        f"<b>Срок действия</b>\n{remaining}{expiry}"
    )
