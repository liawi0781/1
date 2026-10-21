"""Телеграм-бот «Трекер курения» — помогает бросить курить."""

import asyncio
import logging
import os
import random
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
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

from .db import DB
from .texts import HEALTH_MILESTONES, HELP, TIPS, cigs, duration

TZ = ZoneInfo(os.getenv("TZ_NAME", "Europe/Moscow"))
db = DB(os.getenv("DB_PATH", "smoke_bot.sqlite3"))
router = Router()

BTN_SMOKE = "🚬 Закурил"
BTN_CRAVING = "💪 Хочу курить"
BTN_STATS = "📊 Статистика"
BTN_HEALTH = "❤️ Здоровье"
BTN_LIMIT = "🎯 Лимит"
BTN_SETTINGS = "⚙️ Настройки"

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_SMOKE), KeyboardButton(text=BTN_CRAVING)],
        [KeyboardButton(text=BTN_STATS), KeyboardButton(text=BTN_HEALTH)],
        [KeyboardButton(text=BTN_LIMIT), KeyboardButton(text=BTN_SETTINGS)],
    ],
    resize_keyboard=True,
)


class Setup(StatesGroup):
    per_day = State()
    pack_price = State()
    pack_size = State()


class LimitState(StatesGroup):
    value = State()


# ---------- вспомогательные функции ----------

def day_start(days_ago: int = 0) -> int:
    now = datetime.now(TZ)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_ago)
    return int(start.timestamp())


def parse_number(text: str | None) -> float | None:
    try:
        value = float((text or "").replace(",", ".").strip())
    except ValueError:
        return None
    return value if value >= 0 else None


def cig_price(user) -> float:
    return user["pack_price"] / user["pack_size"]


def best_streak(user, now: int) -> int:
    """Самый долгий промежуток без курения с момента начала."""
    points = [user["started_at"], *db.smoke_times(user["user_id"]), now]
    return max(b - a for a, b in zip(points, points[1:]))


def money(value: float) -> str:
    return f"{value:,.0f} ₽".replace(",", " ")


async def require_user(message: Message, state: FSMContext):
    user = db.get_user(message.from_user.id)
    if user is None:
        await start_setup(message, state)
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
        "Буду считать сигареты, показывать прогресс, сэкономленные деньги "
        "и как восстанавливается твой организм.\n\n"
        "Сначала пара вопросов."
    )
    await start_setup(message, state)


async def start_setup(message: Message, state: FSMContext):
    await state.set_state(Setup.per_day)
    await message.answer(
        "Сколько сигарет в день ты обычно выкуриваешь? Напиши число.",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(Setup.per_day)
async def setup_per_day(message: Message, state: FSMContext):
    value = parse_number(message.text)
    if value is None or not 1 <= value <= 200 or value != int(value):
        await message.answer("Напиши целое число от 1 до 200, например: 15")
        return
    await state.update_data(per_day=int(value))
    await state.set_state(Setup.pack_price)
    await message.answer("Сколько стоит одна пачка (в рублях)?")


@router.message(Setup.pack_price)
async def setup_pack_price(message: Message, state: FSMContext):
    value = parse_number(message.text)
    if value is None or not 1 <= value <= 100_000:
        await message.answer("Напиши цену числом, например: 250")
        return
    await state.update_data(pack_price=value)
    await state.set_state(Setup.pack_size)
    await message.answer(
        "Сколько сигарет в пачке?",
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
    db.save_user(message.from_user.id, data["per_day"], data["pack_price"], int(value))
    user = db.get_user(message.from_user.id)
    per_month = user["per_day"] * 30 * cig_price(user)
    await message.answer(
        "Готово! ✅\n\n"
        f"Сейчас ты тратишь на сигареты примерно <b>{money(per_month)}</b> в месяц "
        f"и <b>{money(per_month * 12.17)}</b> в год.\n\n"
        f"Каждый раз, когда куришь, нажимай «{BTN_SMOKE}». "
        f"Когда тянет — жми «{BTN_CRAVING}», я помогу продержаться.\n\n"
        "Удачи, у тебя всё получится! 🚭",
        reply_markup=MAIN_KB,
    )


# ---------- основные действия ----------

@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(HELP, reply_markup=MAIN_KB)


@router.message(F.text == BTN_SMOKE)
async def on_smoke(message: Message, state: FSMContext):
    user = await require_user(message, state)
    if not user:
        return
    uid = user["user_id"]
    now = int(time.time())
    last = db.last_smoke(uid) or user["started_at"]
    event_id = db.add_event(uid, "smoke", now)
    today = db.count(uid, "smoke", day_start())

    lines = [f"Записал. Сегодня: <b>{cigs(today)}</b>."]
    if now - last >= 3600:
        lines.append(f"Ты продержался {duration(now - last)} — это уже результат.")
    limit = user["daily_limit"]
    if limit is not None:
        if today < limit:
            lines.append(f"До лимита осталось: {cigs(limit - today)}.")
        elif today == limit:
            lines.append("⚠️ Это была последняя сигарета по лимиту на сегодня.")
        else:
            lines.append(f"❗ Лимит ({limit}) превышен, лишних сегодня: {today - limit}. Не сдавайся, завтра новый день.")
    lines.append(random.choice([
        "Не ругай себя — главное, продолжать.",
        "Попробуй отложить следующую хотя бы на 15 минут дольше.",
        "Каждая невыкуренная сигарета — это победа.",
    ]))

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="↩️ Отменить", callback_data=f"undo:{event_id}")
    ]])
    await message.answer("\n".join(lines), reply_markup=kb)


@router.callback_query(F.data.startswith("undo:"))
async def on_undo(callback: CallbackQuery):
    event_id = int(callback.data.split(":", 1)[1])
    if db.delete_event(callback.from_user.id, event_id):
        await callback.message.edit_text("↩️ Запись отменена.")
    else:
        await callback.answer("Запись уже удалена", show_alert=False)
        return
    await callback.answer()


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
    saved = total * cig_price(user)
    await callback.message.edit_text(
        f"🎉 Отлично! Ты справился с тягой.\n\n"
        f"Всего побед над тягой: <b>{total}</b>\n"
        f"Только на этом ты сберёг примерно {money(saved)}."
    )
    await callback.answer("Так держать!")


@router.message(Command("stats"))
@router.message(F.text == BTN_STATS)
async def on_stats(message: Message, state: FSMContext):
    user = await require_user(message, state)
    if not user:
        return
    uid = user["user_id"]
    now = int(time.time())
    today = db.count(uid, "smoke", day_start())
    yesterday = db.count(uid, "smoke", day_start(1), day_start())
    week = db.count(uid, "smoke", day_start(6))
    total = db.count(uid, "smoke", user["started_at"])
    cravings = db.count(uid, "craving", 0)

    days = max((now - user["started_at"]) / 86400, 1 / 24)
    week_days = min(7, days)
    expected = user["per_day"] * days
    avoided = max(0, expected - total)
    saved = avoided * cig_price(user)
    avg_week = week / week_days
    reduction = (1 - avg_week / user["per_day"]) * 100

    last = db.last_smoke(uid)
    streak = now - (last or user["started_at"])

    lines = [
        "📊 <b>Твоя статистика</b>\n",
        f"Сегодня: <b>{cigs(today)}</b>"
        + (f" из {user['daily_limit']}" if user["daily_limit"] is not None else ""),
        f"Вчера: {cigs(yesterday)}",
        f"В среднем за неделю: {avg_week:.1f} в день (было {user['per_day']})",
    ]
    if reduction > 0:
        lines.append(f"📉 Ты куришь на <b>{reduction:.0f}%</b> меньше!")
    lines += [
        "",
        f"⏱ Без сигарет уже: <b>{duration(streak)}</b>",
        f"🏆 Лучшая серия: {duration(best_streak(user, now))}",
        "",
        f"🚭 Не выкурено: <b>{cigs(int(avoided))}</b>",
        f"💰 Сэкономлено: <b>{money(saved)}</b>",
        f"💪 Побед над тягой: {cravings}",
        f"📅 В трекере: {duration(now - user['started_at'])}",
    ]
    await message.answer("\n".join(lines), reply_markup=MAIN_KB)


@router.message(F.text == BTN_HEALTH)
async def on_health(message: Message, state: FSMContext):
    user = await require_user(message, state)
    if not user:
        return
    streak = int(time.time()) - (db.last_smoke(user["user_id"]) or user["started_at"])
    lines = [f"❤️ <b>Без сигарет: {duration(streak)}</b>\n"]
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
    current = user["daily_limit"]
    suggestion = max(0, (user["per_day"] if current is None else current) - 1)
    await state.set_state(LimitState.value)
    await message.answer(
        (f"Текущий лимит: <b>{cigs(current)}</b> в день.\n\n" if current is not None else "Лимит не задан.\n\n")
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
    text = (message.text or "").strip().lower()
    if text in ("нет", "убрать", "no"):
        await state.clear()
        db.set_limit(message.from_user.id, None)
        await message.answer("Лимит убран.", reply_markup=MAIN_KB)
        return
    value = parse_number(text)
    if value is None or value != int(value) or value > 200:
        await message.answer("Напиши целое число, например: 10, или «нет».")
        return
    await state.clear()
    if value == 0:
        db.set_limit(message.from_user.id, 0)
        await message.answer("🚭 Цель — ни одной сигареты. Ты сможешь!", reply_markup=MAIN_KB)
        return
    db.set_limit(message.from_user.id, int(value))
    await message.answer(f"🎯 Новый лимит: {cigs(int(value))} в день.", reply_markup=MAIN_KB)


# ---------- настройки ----------

@router.message(Command("settings"))
@router.message(F.text == BTN_SETTINGS)
async def on_settings(message: Message, state: FSMContext):
    user = await require_user(message, state)
    if not user:
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить данные", callback_data="edit")],
        [InlineKeyboardButton(text="🗑 Сбросить всё", callback_data="reset_ask")],
    ])
    await message.answer(
        "⚙️ <b>Настройки</b>\n\n"
        f"Сигарет в день (до начала): {user['per_day']}\n"
        f"Цена пачки: {money(user['pack_price'])}\n"
        f"Сигарет в пачке: {user['pack_size']}\n"
        f"Дневной лимит: {cigs(user['daily_limit']) if user['daily_limit'] is not None else 'не задан'}",
        reply_markup=kb,
    )


@router.callback_query(F.data == "edit")
async def on_edit(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await start_setup(callback.message, state)


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


@router.message()
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
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
