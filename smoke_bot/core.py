"""Общие настройки и функции, которыми пользуются все модули бота."""

import os
import re
from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

from .db import DB
from .texts import TIMEZONES, product_forms, units

DEFAULT_TZ = ZoneInfo(os.getenv("TZ_NAME", "Europe/Moscow"))
db = DB(os.getenv("DB_PATH", "smoke_bot.sqlite3"))
BTN_SMOKE = "🚬 Закурил"
BTN_CRAVING = "💪 Хочу курить"
BTN_STATS = "📊 Статистика"
BTN_HEALTH = "❤️ Здоровье"
BTN_LIMIT = "🎯 Лимит"
BTN_SETTINGS = "⚙️ Настройки"
BTN_DIARY = "📓 Дневник"
BTN_SPEND = "💰 Траты"

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_SMOKE), KeyboardButton(text=BTN_CRAVING)],
        [KeyboardButton(text=BTN_STATS), KeyboardButton(text=BTN_HEALTH)],
        [KeyboardButton(text=BTN_LIMIT), KeyboardButton(text=BTN_SETTINGS)],
        [KeyboardButton(text=BTN_DIARY), KeyboardButton(text=BTN_SPEND)],
    ],
    resize_keyboard=True,
)

MENU_BUTTONS = {BTN_SMOKE, BTN_CRAVING, BTN_STATS, BTN_HEALTH, BTN_LIMIT, BTN_SETTINGS, BTN_DIARY, BTN_SPEND}


def parse_tz(name: str | None) -> tzinfo | None:
    """Принимает имя пояса IANA (Europe/Moscow) или смещение (UTC+3, +5:30)."""
    if not name:
        return None
    name = name.strip()
    m = re.fullmatch(r"(?:UTC|GMT)?\s*([+-])(\d{1,2})(?::?(\d{2}))?", name, re.IGNORECASE)
    if m:
        hours, minutes = int(m[2]), int(m[3] or 0)
        if hours > 14 or minutes >= 60:
            return None
        sign = 1 if m[1] == "+" else -1
        label = f"UTC{m[1]}{hours}" + (f":{minutes:02d}" if minutes else "")
        return timezone(sign * timedelta(hours=hours, minutes=minutes), label)
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def tz_key(tz: tzinfo) -> str:
    """Как сохранить часовой пояс в базе."""
    return getattr(tz, "key", None) or tz.tzname(None)


def user_tz(user) -> tzinfo:
    return parse_tz(user["tz"]) or DEFAULT_TZ


def tz_label(user) -> str:
    key = tz_key(user_tz(user))
    return next((title for title, k in TIMEZONES if k == key), key)


def day_start(tz: tzinfo, days_ago: int = 0) -> int:
    start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    start -= timedelta(days=days_ago)
    # пересобираем дату, чтобы смещение было верным и при переходе на летнее время
    return int(datetime(start.year, start.month, start.day, tzinfo=tz).timestamp())


def parse_number(text: str | None) -> float | None:
    try:
        value = float((text or "").replace(",", ".").strip())
    except ValueError:
        return None
    return value if value >= 0 else None


def u(user, n: int) -> str:
    """«3 сигареты» / «3 стика» — в зависимости от того, что курит человек."""
    return units(n, user["product"])


def many(user) -> str:
    """«сигарет» / «стиков» — для вопросов «сколько …»."""
    return product_forms(user["product"])[2]


def unit_price(user) -> float:
    return user["pack_price"] / user["pack_size"]


def money(user, value: float) -> str:
    return f"{value:,.0f} {user['currency']}".replace(",", " ")
