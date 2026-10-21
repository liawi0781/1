"""Телеграм-бот «Трекер курения» — помогает бросить курить."""

import asyncio
import logging
import os
import random
import re
import time
from datetime import date, datetime, timedelta, tzinfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from . import extras
from .core import (
    BTN_CRAVING,
    BTN_HEALTH,
    BTN_LIMIT,
    BTN_SETTINGS,
    BTN_SMOKE,
    BTN_STATS,
    MAIN_KB,
    TIMEZONES,
    db,
    day_start,
    many,
    money,
    parse_number,
    parse_tz,
    tz_key,
    tz_label,
    u,
    unit_price,
    user_tz,
)
from .texts import (
    CURRENCIES,
    HEALTH_MILESTONES,
    HELP,
    PRODUCTS,
    TIPS,
    duration,
    plural,
    product_forms,
)

router = Router()
fallback_router = Router()  # подключается последним: ловит всё, что не поймали остальные

WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MAX_PER_ENTRY = 20  # сколько штук можно отметить одним сообщением
BACKDATE_LIMIT = 86400  # насколько далеко в прошлое можно отметить

# «+2», «14:30», «+2 14:30», «2 в 14:30»
LOG_RE = re.compile(r"^\+(\d{1,2})(?:\s+(?:в\s+)?(\d{1,2})[:.](\d{2}))?$|^(\d{1,2})[:.](\d{2})$")
ADD_ARGS_RE = re.compile(r"^(?:(\d{1,2})(?![\d:.]))?\s*(?:(?:в\s+)?(\d{1,2})[:.](\d{2}))?$")


class Setup(StatesGroup):
    product = State()
    per_day = State()
    pack_price = State()
    pack_size = State()
    monthly = State()  # только для вейпа: вместо цены и размера пачки


class LimitState(StatesGroup):
    value = State()


class EditState(StatesGroup):
    value = State()


# ---------- вспомогательные функции ----------

def best_streak(user, now: int) -> int:
    """Самый долгий промежуток без курения с момента начала."""
    points = [user["started_at"], *db.smoke_times(user["user_id"]), now]
    return max(b - a for a, b in zip(points, points[1:]))


def limit_info(user) -> tuple[int | None, int | None]:
    """Текущий лимит с учётом плана снижения и сколько дней до следующего снижения."""
    limit = user["daily_limit"]
    step = user["plan_days"]
    if limit is None or not step or not user["limit_set_at"]:
        return limit, None
    tz = user_tz(user)
    start = datetime.fromtimestamp(user["limit_set_at"], tz).date()
    passed = (datetime.now(tz).date() - start).days
    current = max(0, limit - passed // step)
    return current, (step - passed % step if current > 0 else None)


def plan_text(step: int | None) -> str:
    if not step:
        return "без автоматического снижения"
    if step == 1:
        return "−1 каждый день"
    if step == 7:
        return "−1 каждую неделю"
    return f"−1 каждые {step} {plural(step, 'день', 'дня', 'дней')}"


def resolve_time(tz: tzinfo, hour: int, minute: int) -> int | None:
    """Время ЧЧ:ММ сегодня, а если оно ещё не наступило — вчера."""
    if hour > 23 or minute > 59:
        return None
    now = datetime.now(tz)
    moment = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if moment > now:
        moment -= timedelta(days=1)
    return int(moment.timestamp())


def fmt_time(user, ts: int) -> str:
    tz = user_tz(user)
    moment = datetime.fromtimestamp(ts, tz)
    today = datetime.now(tz).date()
    if moment.date() == today:
        prefix = "сегодня"
    elif moment.date() == today - timedelta(days=1):
        prefix = "вчера"
    else:
        prefix = moment.strftime("%d.%m")
    return f"{prefix} {moment:%H:%M}"


async def require_user(message: Message, state: FSMContext):
    """Возвращает пользователя или запускает настройку. Прерывает незаконченный ввод."""
    user = db.get_user(message.from_user.id)
    if user is None:
        await start_setup(message, state)
    else:
        await state.clear()
    return user


# ---------- первоначальная настройка ----------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    if db.get_user(message.from_user.id):
        await message.answer("С возвращением! Продолжаем 💪", reply_markup=MAIN_KB)
        return
    await message.answer(
        "Привет! 👋 Я помогу тебе бросить курить.\n\n"
        "Буду считать, сколько ты куришь, показывать прогресс, сэкономленные деньги "
        "и как восстанавливается твой организм.\n\n"
        "Сначала пара вопросов."
    )
    await start_setup(message, state)


async def start_setup(message: Message, state: FSMContext):
    await state.set_state(Setup.product)
    await message.answer(
        "Что ты куришь?",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=p["title"])] for p in PRODUCTS.values()],
            resize_keyboard=True,
            one_time_keyboard=True,
        ),
    )


@router.message(Setup.product)
async def setup_product(message: Message, state: FSMContext):
    product = next((key for key, p in PRODUCTS.items() if p["title"] == message.text), None)
    if product is None:
        await message.answer("Выбери вариант кнопкой ниже 👇")
        return
    await state.update_data(product=product)
    await ask_per_day(message, state, product)


async def ask_per_day(message: Message, state: FSMContext, product: str, remove_keyboard: bool = True):
    await state.set_state(Setup.per_day)
    await message.answer(
        PRODUCTS[product]["ask"],
        reply_markup=ReplyKeyboardRemove() if remove_keyboard else None,
    )


@router.message(Setup.per_day)
async def setup_per_day(message: Message, state: FSMContext):
    value = parse_number(message.text)
    if value is None or not 1 <= value <= 200 or value != int(value):
        await message.answer("Напиши целое число от 1 до 200, например: 15")
        return
    data = await state.update_data(per_day=int(value))
    if data["product"] == "vape":
        await state.set_state(Setup.monthly)
        await message.answer(
            "Сколько ты в среднем тратишь в месяц на вейп: жидкости, испарители, устройства? "
            "Напиши число."
        )
        return
    await state.set_state(Setup.pack_price)
    await message.answer("Сколько стоит одна пачка? Напиши число.")


@router.message(Setup.monthly)
async def setup_monthly(message: Message, state: FSMContext):
    value = parse_number(message.text)
    if value is None or not 1 <= value <= 10_000_000:
        await message.answer("Напиши сумму числом, например: 3000")
        return
    data = await state.get_data()
    await state.clear()
    # у вейпа нет пачки: цена «штуки» = месячные траты / число подходов за месяц
    db.save_user(message.from_user.id, "vape", data["per_day"], value / (data["per_day"] * 30), 1)
    await finish_setup(message, db.get_user(message.from_user.id), per_month=value)


@router.message(Setup.pack_price)
async def setup_pack_price(message: Message, state: FSMContext):
    value = parse_number(message.text)
    if value is None or not 1 <= value <= 1_000_000:
        await message.answer("Напиши цену числом, например: 250")
        return
    data = await state.update_data(pack_price=value)
    await state.set_state(Setup.pack_size)
    await message.answer(
        f"Сколько {product_forms(data['product'])[2]} в пачке?",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="20"), KeyboardButton(text="10")]],
            resize_keyboard=True,
            one_time_keyboard=True,
        ),
    )


@router.message(Setup.pack_size)
async def setup_pack_size(message: Message, state: FSMContext):
    value = parse_number(message.text)
    if value is None or not 1 <= value <= 100 or value != int(value):
        await message.answer("Напиши целое число, например: 20")
        return
    data = await state.get_data()
    await state.clear()
    uid = message.from_user.id
    db.save_user(uid, data["product"], data["per_day"], data["pack_price"], int(value))
    user = db.get_user(uid)
    await finish_setup(message, user, per_month=user["per_day"] * 30 * unit_price(user))


async def finish_setup(message: Message, user, per_month: float):
    await message.answer(
        "Готово! ✅\n\n"
        f"Сейчас ты тратишь на курение примерно <b>{money(user, per_month)}</b> в месяц "
        f"и <b>{money(user, per_month * 12.17)}</b> в год.\n\n"
        f"Каждый раз, когда куришь, нажимай «{BTN_SMOKE}». "
        "Забыл отметить — напиши время, например <code>14:30</code>, "
        "а несколько сразу — <code>+2</code>. "
        f"Когда тянет — жми «{BTN_CRAVING}», я помогу продержаться.\n\n"
        f"Часовой пояс: {tz_label(user)}, валюта: {user['currency']}. "
        f"Это и остальное меняется в «{BTN_SETTINGS}».\n\n"
        "А ещё есть «📓 Дневник» (настроение и триггеры) и «💰 Траты» (записи покупок и калькулятор).\n\n"
        "Удачи, у тебя всё получится! 🚭",
        reply_markup=MAIN_KB,
    )


# ---------- отметки ----------

@router.message(Command("help"))
async def cmd_help(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(HELP, reply_markup=MAIN_KB)


async def log_smoke(message: Message, user, n: int = 1, ts: int | None = None):
    uid = user["user_id"]
    now = int(time.time())
    backdated = ts is not None
    ts = ts or now
    last = db.last_smoke(uid) or user["started_at"]
    if ts < user["started_at"]:
        # отметили то, что было до начала трекера — сдвигаем начало
        db.update_user(uid, started_at=ts)
    first_id, last_id = db.add_events(uid, "smoke", ts, n)
    today = db.count(uid, "smoke", day_start(user_tz(user)))

    what = u(user, n) if n > 1 else "одну"
    lines = [
        (f"Записал {what} — {fmt_time(user, ts)}." if backdated else
         f"Записал{' ' + what if n > 1 else ''}.")
        + f" Сегодня: <b>{u(user, today)}</b>."
    ]
    if not backdated and now - last >= 3600:
        lines.append(f"Ты продержался {duration(now - last)} — это уже результат.")
    limit, _ = limit_info(user)
    if limit is not None:
        if today < limit:
            lines.append(f"До лимита осталось: {u(user, limit - today)}.")
        elif today == limit:
            lines.append("⚠️ Лимит на сегодня исчерпан.")
        else:
            lines.append(f"❗ Лимит ({limit}) превышен, лишних сегодня: {today - limit}. Не сдавайся, завтра новый день.")
    lines.append(random.choice([
        "Не ругай себя — главное, продолжать.",
        "Попробуй отложить следующую хотя бы на 15 минут дольше.",
        "Каждая пропущенная — это маленькая победа.",
    ]))

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="↩️ Отменить", callback_data=f"undo:{first_id}:{last_id}")
    ]])
    await message.answer("\n".join(lines), reply_markup=kb)


def parse_log(user, n_text: str | None, h_text: str | None, m_text: str | None):
    """Возвращает (количество, время) или текст ошибки."""
    n = int(n_text) if n_text else 1
    if not 1 <= n <= MAX_PER_ENTRY:
        return f"За раз можно отметить от 1 до {MAX_PER_ENTRY}."
    ts = None
    if h_text is not None:
        ts = resolve_time(user_tz(user), int(h_text), int(m_text))
        if ts is None:
            return "Не понял время. Пиши так: 14:30"
        if time.time() - ts > BACKDATE_LIMIT:
            return "Отметить можно только за последние сутки."
    return n, ts


@router.message(F.text == BTN_SMOKE)
async def on_smoke(message: Message, state: FSMContext):
    user = await require_user(message, state)
    if user:
        await log_smoke(message, user)


@router.message(Command("add"))
async def cmd_add(message: Message, command: CommandObject, state: FSMContext):
    user = await require_user(message, state)
    if not user:
        return
    m = ADD_ARGS_RE.match((command.args or "").strip())
    if not m:
        await message.answer(
            "Примеры: <code>/add</code>, <code>/add 2</code>, "
            "<code>/add 14:30</code>, <code>/add 2 14:30</code>"
        )
        return
    result = parse_log(user, m[1], m[2], m[3])
    if isinstance(result, str):
        await message.answer(result)
        return
    await log_smoke(message, user, *result)


@router.message(StateFilter(None), F.text.regexp(LOG_RE).as_("m"))
async def on_log_text(message: Message, m: re.Match, state: FSMContext):
    user = await require_user(message, state)
    if not user:
        return
    if m[1]:
        result = parse_log(user, m[1], m[2], m[3])
    else:
        result = parse_log(user, None, m[4], m[5])
    if isinstance(result, str):
        await message.answer(result)
        return
    await log_smoke(message, user, *result)


@router.callback_query(F.data.startswith("undo:"))
async def on_undo(callback: CallbackQuery):
    parts = callback.data.split(":")[1:]
    first_id, last_id = int(parts[0]), int(parts[-1])
    if db.delete_events(callback.from_user.id, first_id, last_id):
        await callback.message.edit_text("↩️ Запись отменена.")
        await callback.answer()
    else:
        await callback.answer("Запись уже удалена")


# ---------- тяга ----------

@router.message(F.text == BTN_CRAVING)
async def on_craving(message: Message, state: FSMContext):
    user = await require_user(message, state)
    if not user:
        return
    tips = random.sample(TIPS, 3)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Я удержался!", callback_data="resisted")],
        [InlineKeyboardButton(text="🔄 Другие советы", callback_data="more_tips")],
    ])
    await message.answer(
        "Держись! 💪 Тяга пройдёт через несколько минут.\n\n"
        + "\n".join(f"• {t}" for t in tips),
        reply_markup=kb,
    )


@router.callback_query(F.data == "more_tips")
async def on_more_tips(callback: CallbackQuery):
    tips = random.sample(TIPS, 3)
    await callback.message.edit_text(
        "Вот ещё способы справиться:\n\n" + "\n".join(f"• {t}" for t in tips),
        reply_markup=callback.message.reply_markup,
    )
    await callback.answer()


@router.callback_query(F.data == "resisted")
async def on_resisted(callback: CallbackQuery):
    user = db.get_user(callback.from_user.id)
    if not user:
        await callback.answer("Сначала нажми /start", show_alert=True)
        return
    db.add_event(user["user_id"], "craving")
    total = db.count(user["user_id"], "craving", 0)
    saved = total * unit_price(user)
    await callback.message.edit_text(
        f"🎉 Отлично! Ты справился с тягой.\n\n"
        f"Всего побед над тягой: <b>{total}</b>\n"
        f"Только на этом ты сберёг примерно {money(user, saved)}."
    )
    await callback.answer("Так держать!")


# ---------- статистика ----------

STATS_KB = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="📅 По дням", callback_data="days"),
    InlineKeyboardButton(text="📝 История", callback_data="history"),
]])


@router.message(Command("stats"))
@router.message(F.text == BTN_STATS)
async def on_stats(message: Message, state: FSMContext):
    user = await require_user(message, state)
    if not user:
        return
    uid = user["user_id"]
    tz = user_tz(user)
    now = int(time.time())
    today = db.count(uid, "smoke", day_start(tz))
    yesterday = db.count(uid, "smoke", day_start(tz, 1), day_start(tz))
    week = db.count(uid, "smoke", max(day_start(tz, 6), user["started_at"]))
    total = db.count(uid, "smoke", user["started_at"])
    cravings = db.count(uid, "craving", 0)

    days = max((now - user["started_at"]) / 86400, 1 / 24)
    week_days = max(1, min(7, days))  # в первый день не экстраполируем на сутки
    expected = user["per_day"] * days
    avoided = max(0, expected - total)
    saved = avoided * unit_price(user)
    avg_week = week / week_days
    reduction = (1 - avg_week / user["per_day"]) * 100

    last = db.last_smoke(uid)
    streak = now - (last or user["started_at"])
    limit, _ = limit_info(user)

    lines = [
        "📊 <b>Твоя статистика</b>\n",
        f"Сегодня: <b>{u(user, today)}</b>" + (f" из {limit}" if limit is not None else ""),
        f"Вчера: {u(user, yesterday)}",
        f"В среднем за неделю: {avg_week:.1f} в день (было {user['per_day']})",
    ]
    if reduction > 0:
        lines.append(f"📉 Ты куришь на <b>{reduction:.0f}%</b> меньше!")
    lines += [
        "",
        f"⏱ Без курения уже: <b>{duration(streak)}</b>",
        f"🏆 Лучшая серия: {duration(best_streak(user, now))}",
        "",
        f"🚭 Пропущено: <b>{u(user, int(avoided))}</b>",
        f"💰 Сэкономлено: <b>{money(user, saved)}</b>",
        f"💪 Побед над тягой: {cravings}",
        f"📅 В трекере: {duration(now - user['started_at'])}",
    ]
    await message.answer("\n".join(lines), reply_markup=STATS_KB)


def render_days(user, days: int = 14) -> str:
    tz = user_tz(user)
    today = datetime.now(tz).date()
    first_day = max(
        today - timedelta(days=days - 1),
        datetime.fromtimestamp(user["started_at"], tz).date(),
    )
    counts: dict[date, int] = {}
    for ts in db.smoke_times(user["user_id"], day_start(tz, (today - first_day).days)):
        d = datetime.fromtimestamp(ts, tz).date()
        counts[d] = counts.get(d, 0) + 1
    scale = max(user["per_day"], *counts.values(), 1)

    lines = [f"📅 <b>По дням</b> (было {user['per_day']} в день)\n"]
    d = today
    while d >= first_day:
        n = counts.get(d, 0)
        bar = "▇" * round(n / scale * 10) if n else "·"
        mark = " 🏆" if n == 0 else (" ✅" if n < user["per_day"] else "")
        lines.append(f"<code>{WEEKDAYS[d.weekday()]} {d:%d.%m} {n:>3}</code> {bar}{mark}")
        d -= timedelta(days=1)
    return "\n".join(lines)


def render_history(user) -> tuple[str, InlineKeyboardMarkup | None]:
    rows = db.recent_events(user["user_id"], "smoke", 10)
    if not rows:
        return "📝 Записей пока нет.", None
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"❌ {fmt_time(user, r['ts'])}", callback_data=f"del:{r['id']}")]
        for r in rows
    ])
    return "📝 <b>Последние записи</b>\nНажми на запись, чтобы удалить её.", kb


@router.message(Command("days"))
async def cmd_days(message: Message, state: FSMContext):
    user = await require_user(message, state)
    if user:
        await message.answer(render_days(user), reply_markup=MAIN_KB)


@router.callback_query(F.data == "days")
async def on_days(callback: CallbackQuery):
    user = db.get_user(callback.from_user.id)
    if user:
        await callback.message.answer(render_days(user))
    await callback.answer()


@router.message(Command("history"))
async def cmd_history(message: Message, state: FSMContext):
    user = await require_user(message, state)
    if user:
        text, kb = render_history(user)
        await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "history")
async def on_history(callback: CallbackQuery):
    user = db.get_user(callback.from_user.id)
    if user:
        text, kb = render_history(user)
        await callback.message.answer(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("del:"))
async def on_delete(callback: CallbackQuery):
    user = db.get_user(callback.from_user.id)
    if not user:
        await callback.answer()
        return
    deleted = db.delete_event(user["user_id"], int(callback.data.split(":", 1)[1]))
    text, kb = render_history(user)
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer("Удалено" if deleted else "Уже удалено")


@router.message(F.text == BTN_HEALTH)
async def on_health(message: Message, state: FSMContext):
    user = await require_user(message, state)
    if not user:
        return
    streak = int(time.time()) - (db.last_smoke(user["user_id"]) or user["started_at"])
    lines = [f"❤️ <b>Без курения: {duration(streak)}</b>\n"]
    next_shown = False
    for seconds, text in HEALTH_MILESTONES:
        if streak >= seconds:
            lines.append(f"✅ {duration(seconds)}: {text}")
        elif not next_shown:
            progress = int(streak / seconds * 10)
            bar = "▓" * progress + "░" * (10 - progress)
            lines.append(
                f"\n⏳ <b>Следующий этап</b> через {duration(seconds - streak)}:\n"
                f"{text}\n{bar} {streak / seconds * 100:.0f}%\n"
            )
            next_shown = True
        else:
            lines.append(f"▫️ {duration(seconds)}: {text}")
    await message.answer("\n".join(lines), reply_markup=MAIN_KB)


# ---------- лимит ----------

@router.message(Command("limit"))
@router.message(F.text == BTN_LIMIT)
async def on_limit(message: Message, state: FSMContext):
    user = await require_user(message, state)
    if not user:
        return
    current, next_in = limit_info(user)
    if current is None:
        status = "Лимит не задан.\n\n"
    else:
        status = f"Текущий лимит: <b>{u(user, current)}</b> в день"
        if user["plan_days"]:
            status += f", план: {plan_text(user['plan_days'])}"
            if next_in:
                status += f" (следующее снижение через {next_in} {plural(next_in, 'день', 'дня', 'дней')})"
        status += ".\n\n"
    suggestion = max(0, (user["per_day"] if current is None else current) - 1)
    await state.set_state(LimitState.value)
    await message.answer(
        status
        + "Напиши новый дневной лимит числом "
        f"(совет: снижай понемногу, например до {suggestion}).\n"
        "0 — совсем не курить, «нет» — убрать лимит.",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=str(suggestion)), KeyboardButton(text="нет")]],
            resize_keyboard=True,
            one_time_keyboard=True,
        ),
    )


@router.message(LimitState.value)
async def on_limit_value(message: Message, state: FSMContext):
    uid = message.from_user.id
    text = (message.text or "").strip().lower()
    if text in ("нет", "убрать", "no"):
        await state.clear()
        db.update_user(uid, daily_limit=None, plan_days=None, limit_set_at=None)
        await message.answer("Лимит убран.", reply_markup=MAIN_KB)
        return
    value = parse_number(text)
    if value is None or value != int(value) or value > 200:
        await message.answer("Напиши целое число, например: 10, или «нет».")
        return
    await state.clear()
    value = int(value)
    db.update_user(uid, daily_limit=value, plan_days=None, limit_set_at=int(time.time()))
    if value == 0:
        await message.answer("🚭 Цель — ни одной. Ты сможешь!", reply_markup=MAIN_KB)
        return
    user = db.get_user(uid)
    await message.answer(f"🎯 Новый лимит: {u(user, value)} в день.", reply_markup=MAIN_KB)
    await message.answer(
        "Снижать лимит автоматически? Я буду уменьшать его на 1, пока не дойдём до нуля.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=plan_text(1), callback_data="plan:1"),
             InlineKeyboardButton(text="−1 каждые 3 дня", callback_data="plan:3")],
            [InlineKeyboardButton(text=plan_text(7), callback_data="plan:7"),
             InlineKeyboardButton(text="Не нужно", callback_data="plan:0")],
        ]),
    )


@router.callback_query(F.data.startswith("plan:"))
async def on_plan(callback: CallbackQuery):
    user = db.get_user(callback.from_user.id)
    if not user or not user["daily_limit"]:
        await callback.answer("Сначала задай лимит", show_alert=True)
        return
    step = int(callback.data.split(":", 1)[1]) or None
    # отсчёт начинаем заново, чтобы снижение шло от текущего значения
    current, _ = limit_info(user)
    db.update_user(user["user_id"], daily_limit=current, plan_days=step, limit_set_at=int(time.time()))
    if step:
        days_to_zero = current * step
        await callback.message.edit_text(
            f"📉 План: {plan_text(step)}. "
            f"Если придерживаться его, через {days_to_zero} "
            f"{plural(days_to_zero, 'день', 'дня', 'дней')} лимит станет нулевым."
        )
    else:
        await callback.message.edit_text("Хорошо, лимит будет постоянным.")
    await callback.answer()


# ---------- настройки ----------

def settings_view(user) -> tuple[str, InlineKeyboardMarkup]:
    limit, _ = limit_info(user)
    product = PRODUCTS.get(user["product"], PRODUCTS["cig"])["title"]
    vape = user["product"] == "vape"
    cost_lines = (
        f"Траты в месяц (оценка): {money(user, unit_price(user) * user['per_day'] * 30)}\n"
        if vape else
        f"Цена пачки: {money(user, user['pack_price'])}\n"
        f"В пачке: {u(user, user['pack_size'])}\n"
    )
    cost_buttons = (
        [InlineKeyboardButton(text="💵 Траты в месяц", callback_data="set:monthly")]
        if vape else
        [InlineKeyboardButton(text="💵 Цена пачки", callback_data="set:pack_price"),
         InlineKeyboardButton(text="📦 Штук в пачке", callback_data="set:pack_size")]
    )
    text = (
        "⚙️ <b>Настройки</b>\n\n"
        f"Что куришь: {product}\n"
        f"В день (до начала): {u(user, user['per_day'])}\n"
        + cost_lines +
        f"Валюта: {user['currency']}\n"
        f"Часовой пояс: {tz_label(user)}\n"
        f"Дневной лимит: {u(user, limit) if limit is not None else 'не задан'}"
        + (f" ({plan_text(user['plan_days'])})" if limit is not None and user["plan_days"] else "")
        + "\n\nЧто изменить?"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚬 Что куришь", callback_data="set:product"),
         InlineKeyboardButton(text="📈 Сколько в день", callback_data="set:per_day")],
        cost_buttons,
        [InlineKeyboardButton(text="💱 Валюта", callback_data="set:currency"),
         InlineKeyboardButton(text="🕒 Часовой пояс", callback_data="set:tz")],
        [InlineKeyboardButton(text="🗑 Сбросить всё", callback_data="reset_ask")],
    ])
    return text, kb


@router.message(Command("settings"))
@router.message(F.text == BTN_SETTINGS)
async def on_settings(message: Message, state: FSMContext):
    user = await require_user(message, state)
    if not user:
        return
    text, kb = settings_view(user)
    await message.answer(text, reply_markup=kb)


NUMBER_FIELDS = {
    # поле: (вопрос, минимум, максимум, только целое)
    "per_day": ("Сколько {many} в день ты курил до начала? Напиши число.", 1, 200, True),
    "pack_price": ("Сколько стоит пачка? Напиши число.", 1, 1_000_000, False),
    "pack_size": ("Сколько {many} в пачке?", 1, 100, True),
    "monthly": ("Сколько в среднем тратишь в месяц на жидкости, испарители и устройства? Напиши число.", 1, 10_000_000, False),
}


@router.callback_query(F.data.startswith("set:"))
async def on_set(callback: CallbackQuery, state: FSMContext):
    user = db.get_user(callback.from_user.id)
    if not user:
        await callback.answer("Сначала нажми /start", show_alert=True)
        return
    field = callback.data.split(":", 1)[1]
    await callback.answer()
    if field == "product":
        await callback.message.edit_text("Что ты куришь?", reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=p["title"], callback_data=f"product:{key}")]
                             for key, p in PRODUCTS.items()]
        ))
        return
    await state.set_state(EditState.value)
    await state.update_data(field=field)
    if field == "currency":
        await callback.message.edit_text(
            "Выбери валюту или напиши свою (до 5 символов):",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=c, callback_data=f"cur:{c}") for c in CURRENCIES
            ]]),
        )
    elif field == "tz":
        buttons = [InlineKeyboardButton(text=title, callback_data=f"tz:{key}") for title, key in TIMEZONES]
        await callback.message.edit_text(
            "Выбери часовой пояс или напиши его: например <code>UTC+3</code> "
            "или <code>Europe/Berlin</code>.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[buttons[i:i + 2] for i in range(0, len(buttons), 2)]
            ),
        )
    elif field in NUMBER_FIELDS:
        await callback.message.answer(NUMBER_FIELDS[field][0].format(many=many(user)))
    else:
        await state.clear()


async def show_saved(message: Message, user_id: int, edit: bool = False):
    text, kb = settings_view(db.get_user(user_id))
    text = "✅ Сохранено.\n\n" + text
    if edit:
        await message.edit_text(text, reply_markup=kb)
    else:
        await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("product:"))
async def on_product(callback: CallbackQuery, state: FSMContext):
    key = callback.data.split(":", 1)[1]
    if key not in PRODUCTS or not db.get_user(callback.from_user.id):
        await callback.answer()
        return
    old = db.get_user(callback.from_user.id)["product"]
    await callback.answer()
    if (key == "vape") != (old == "vape"):
        # у вейпа другие вопросы про цену, поэтому заново проходим короткую настройку
        await state.clear()
        await state.update_data(product=key)
        await callback.message.edit_text("Для этого вида нужно уточнить пару данных.")
        await ask_per_day(callback.message, state, key)
        return
    db.update_user(callback.from_user.id, product=key)
    await show_saved(callback.message, callback.from_user.id, edit=True)


@router.callback_query(F.data.startswith("cur:"))
async def on_currency(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    if db.get_user(callback.from_user.id):
        db.update_user(callback.from_user.id, currency=callback.data.split(":", 1)[1][:5])
        await show_saved(callback.message, callback.from_user.id, edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith("tz:"))
async def on_tz(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    tz = parse_tz(callback.data.split(":", 1)[1])
    if tz and db.get_user(callback.from_user.id):
        db.update_user(callback.from_user.id, tz=tz_key(tz))
        await show_saved(callback.message, callback.from_user.id, edit=True)
    await callback.answer()


@router.message(EditState.value)
async def on_edit_value(message: Message, state: FSMContext):
    field = (await state.get_data()).get("field")
    uid = message.from_user.id
    text = (message.text or "").strip()
    if field == "currency":
        if not 1 <= len(text) <= 5:
            await message.answer("Валюта — от 1 до 5 символов, например: ₽ или руб.")
            return
        value = text
    elif field == "tz":
        tz = parse_tz(text)
        if tz is None:
            await message.answer("Не знаю такой пояс. Примеры: <code>UTC+3</code>, <code>Asia/Tbilisi</code>")
            return
        value = tz_key(tz)
    elif field in NUMBER_FIELDS:
        _, low, high, integer = NUMBER_FIELDS[field]
        number = parse_number(text)
        if number is None or not low <= number <= high or (integer and number != int(number)):
            await message.answer(f"Напиши {'целое ' if integer else ''}число от {low} до {high}.")
            return
        value = int(number) if integer else number
    else:
        await state.clear()
        return
    await state.clear()
    user = db.get_user(uid)
    if user is None:
        return
    if field == "monthly":
        fields = {"pack_price": value / (user["per_day"] * 30), "pack_size": 1}
    elif field == "per_day" and user["product"] == "vape":
        # месячные траты не меняются, пересчитываем цену одного подхода
        monthly = unit_price(user) * user["per_day"] * 30
        fields = {"per_day": value, "pack_price": monthly / (value * 30), "pack_size": 1}
    else:
        fields = {field: value}
    db.update_user(uid, **fields)
    await show_saved(message, uid)


@router.callback_query(F.data == "reset_ask")
async def on_reset_ask(callback: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Да, удалить", callback_data="reset_yes"),
        InlineKeyboardButton(text="Отмена", callback_data="reset_no"),
    ]])
    await callback.message.edit_text("Точно удалить все данные и начать заново?", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "reset_no")
async def on_reset_no(callback: CallbackQuery):
    await callback.message.edit_text("Хорошо, ничего не удалено.")
    await callback.answer()


@router.callback_query(F.data == "reset_yes")
async def on_reset_yes(callback: CallbackQuery, state: FSMContext):
    db.reset(callback.from_user.id)
    await callback.message.edit_text("Все данные удалены.")
    await callback.answer()
    await start_setup(callback.message, state)


@fallback_router.callback_query()
async def stale_callback(callback: CallbackQuery):
    await callback.answer("Эта кнопка устарела. Открой меню заново.")


@fallback_router.message()
async def fallback(message: Message, state: FSMContext):
    if await require_user(message, state):
        await message.answer("Не понял 🤔 Воспользуйся кнопками ниже или /help.", reply_markup=MAIN_KB)


async def main():
    logging.basicConfig(level=logging.INFO)
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Укажи токен бота в переменной окружения BOT_TOKEN")
    bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(extras.router)
    dp.include_router(router)
    dp.include_router(fallback_router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
