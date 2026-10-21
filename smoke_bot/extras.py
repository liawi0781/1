"""Траты с калькулятором и дневник настроения."""

import html
import re
import time
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .core import BTN_DIARY, BTN_SPEND, MENU_BUTTONS, db, day_start, money, unit_price, user_tz
from .texts import DIARY_TRIGGERS, EXPENSE_CATS, MOODS, plural

router = Router()

# Ввод в состоянии не должен «съедать» команды и кнопки меню
FREE_TEXT = F.text & ~F.text.startswith("/") & ~F.text.in_(MENU_BUTTONS)


class ExpenseState(StatesGroup):
    amount = State()


class CalcState(StatesGroup):
    value = State()


class DiaryState(StatesGroup):
    note = State()


# ---------- вспомогательные функции ----------

def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def kb(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=list(rows))


def days_word(n: int) -> str:
    return f"{n} {plural(n, 'день', 'дня', 'дней')}"


async def need_user(message: Message, state: FSMContext):
    user = db.get_user(message.from_user.id)
    if user is None:
        await message.answer("Сначала нужно пройти настройку — нажми /start.")
        return None
    await state.clear()
    return user


def date_label(user, ts: int) -> str:
    return datetime.fromtimestamp(ts, user_tz(user)).strftime("%d.%m %H:%M")


# =====================================================================
#                              ТРАТЫ
# =====================================================================

SPEND_KB = kb(
    [btn("➕ Добавить трату", "exp:add"), btn("🧮 Калькулятор", "exp:calc")],
    [btn("🧾 Последние траты", "exp:list")],
)

# сумма с пробелами-разделителями тысяч: «2 500,5»; остальное — комментарий
AMOUNT = r"\d{1,3}(?:[ \u00a0]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?"
EXPENSE_RE = re.compile(rf"^\s*({AMOUNT})\s*(.*)$", re.S)

# (название, ключевое слово, дней в периоде) — порядок важен для поиска
PERIODS = [("год", 365.0), ("мес", 365 / 12), ("нед", 7.0), ("ден", 1.0), ("дня", 1.0), ("дне", 1.0), ("сут", 1.0)]


def spend_text(user) -> str:
    uid, tz = user["user_id"], user_tz(user)
    lines = ["💰 <b>Траты</b>\n"]
    if db.first_expense_ts(uid) is None:
        lines.append(
            "Пока ничего не записано. Добавляй покупки — сигареты, жидкость, испарители, "
            "устройства — и я покажу, сколько уходит на курение."
        )
    else:
        lines += [
            f"Сегодня: {money(user, db.expense_sum(uid, day_start(tz)))}",
            f"7 дней: {money(user, db.expense_sum(uid, day_start(tz, 6)))}",
            f"30 дней: <b>{money(user, db.expense_sum(uid, day_start(tz, 29)))}</b>",
            f"Всего: {money(user, db.expense_sum(uid))}",
        ]
        rows = db.expense_by_category(uid, day_start(tz, 29))
        total = sum(r["total"] for r in rows) or 1
        if rows:
            lines.append("\n<b>На что уходит (30 дней):</b>")
            lines += [
                f"{EXPENSE_CATS.get(r['category'], r['category'])} — {money(user, r['total'])} "
                f"({r['total'] / total * 100:.0f}%)"
                for r in rows
            ]
        refills = []
        for cat in ("liquid", "coil"):
            times = db.expense_times(uid, cat)
            if len(times) >= 2:
                avg = (times[-1] - times[0]) / (len(times) - 1) / 86400
                refills.append(f"{EXPENSE_CATS[cat]}: хватает примерно на {days_word(max(1, round(avg)))}")
        if refills:
            lines.append("\n<b>Как часто покупаешь:</b>")
            lines += refills
    monthly = unit_price(user) * user["per_day"] * 30
    lines.append(f"\nДо начала ты тратил примерно {money(user, monthly)} в месяц (по настройкам).")
    return "\n".join(lines)


def projection(user, per_day: float) -> str:
    rows = [("День", 1), ("Неделя", 7), ("Месяц", 365 / 12), ("Год", 365), ("5 лет", 365 * 5), ("10 лет", 365 * 10)]
    return "\n".join(f"{name}: <b>{money(user, per_day * days)}</b>" for name, days in rows)


def calc_text(user) -> str:
    uid = user["user_id"]
    per_day = unit_price(user) * user["per_day"]
    lines = [
        "🧮 <b>Калькулятор трат</b>\n",
        f"Столько уходило на курение до начала ({user['per_day']} в день):",
        projection(user, per_day),
    ]
    first = db.first_expense_ts(uid)
    now = time.time()
    if first and (now - first) >= 7 * 86400:
        window = min(30.0, (now - first) / 86400)
        actual = db.expense_sum(uid, int(now - window * 86400)) / window
        lines += ["\nА так — по твоим записанным тратам:", projection(user, actual)]
    lines.append(f"\n🎯 Бросив, за год ты сбережёшь около <b>{money(user, per_day * 365)}</b>.")
    return "\n".join(lines)


def parse_spend(text: str) -> float | None:
    """«500 в неделю + 300 в месяц» → расход в день. None, если не поняли."""
    total = 0.0
    for part in re.split(r"\s*\+\s*", text.lower()):
        number = re.search(AMOUNT, part)
        days = next((d for key, d in PERIODS if key in part), None)
        if not number or days is None:
            return None
        total += float(re.sub(r"\s", "", number.group()).replace(",", ".")) / days
    return total


@router.message(Command("spend"))
@router.message(F.text == BTN_SPEND)
async def on_spend(message: Message, state: FSMContext):
    user = await need_user(message, state)
    if user:
        await message.answer(spend_text(user), reply_markup=SPEND_KB)


@router.callback_query(F.data == "exp:add")
async def on_exp_add(callback: CallbackQuery):
    rows = [[btn(label, f"expcat:{key}")] for key, label in EXPENSE_CATS.items()]
    await callback.message.answer("На что потратил?", reply_markup=kb(*rows))
    await callback.answer()


@router.callback_query(F.data.startswith("expcat:"))
async def on_exp_category(callback: CallbackQuery, state: FSMContext):
    key = callback.data.split(":", 1)[1]
    if key not in EXPENSE_CATS:
        await callback.answer()
        return
    await state.set_state(ExpenseState.amount)
    await state.update_data(cat=key)
    await callback.message.edit_text(
        f"{EXPENSE_CATS[key]}\n\nСколько потратил? Можно добавить комментарий, "
        "например: <code>450 Elf Bar</code>"
    )
    await callback.answer()


@router.message(ExpenseState.amount, FREE_TEXT)
async def on_exp_amount(message: Message, state: FSMContext):
    m = EXPENSE_RE.match(message.text)
    amount = float(re.sub(r"\s", "", m[1]).replace(",", ".")) if m else 0
    if not m or not 0 < amount <= 10_000_000:
        await message.answer("Напиши сумму числом, например: 450")
        return
    data = await state.get_data()
    await state.clear()
    user = db.get_user(message.from_user.id)
    if user is None or data.get("cat") not in EXPENSE_CATS:
        return
    note = m[2].strip()[:100]
    expense_id = db.add_expense(user["user_id"], data["cat"], amount, note)
    month = db.expense_sum(user["user_id"], day_start(user_tz(user), 29))
    await message.answer(
        f"✅ Записал: {EXPENSE_CATS[data['cat']]} — {money(user, amount)}"
        + (f" ({html.escape(note)})" if note else "")
        + f"\nЗа 30 дней всего: <b>{money(user, month)}</b>.",
        reply_markup=kb([btn("↩️ Отменить", f"expundo:{expense_id}")]),
    )


@router.callback_query(F.data.startswith("expundo:"))
async def on_exp_undo(callback: CallbackQuery):
    if db.delete_expense(callback.from_user.id, int(callback.data.split(":", 1)[1])):
        await callback.message.edit_text("↩️ Трата удалена.")
        await callback.answer()
    else:
        await callback.answer("Уже удалено")


def render_expenses(user) -> tuple[str, InlineKeyboardMarkup | None]:
    rows = db.recent_expenses(user["user_id"], 10)
    if not rows:
        return "🧾 Трат пока нет.", None
    buttons = [
        [btn(
            f"❌ {datetime.fromtimestamp(r['ts'], user_tz(user)):%d.%m} "
            f"{EXPENSE_CATS.get(r['category'], '').split()[0]} {money(user, r['amount'])}"
            + (f" · {r['note'][:15]}" if r["note"] else ""),
            f"expdel:{r['id']}",
        )]
        for r in rows
    ]
    return "🧾 <b>Последние траты</b>\nНажми на запись, чтобы удалить её.", kb(*buttons)


@router.callback_query(F.data == "exp:list")
async def on_exp_list(callback: CallbackQuery):
    user = db.get_user(callback.from_user.id)
    if user:
        text, markup = render_expenses(user)
        await callback.message.answer(text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data.startswith("expdel:"))
async def on_exp_delete(callback: CallbackQuery):
    user = db.get_user(callback.from_user.id)
    if not user:
        await callback.answer()
        return
    deleted = db.delete_expense(user["user_id"], int(callback.data.split(":", 1)[1]))
    text, markup = render_expenses(user)
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer("Удалено" if deleted else "Уже удалено")


@router.callback_query(F.data == "exp:calc")
async def on_exp_calc(callback: CallbackQuery):
    user = db.get_user(callback.from_user.id)
    if user:
        await callback.message.answer(
            calc_text(user), reply_markup=kb([btn("✍️ Посчитать своё", "calc:custom")])
        )
    await callback.answer()


@router.callback_query(F.data == "calc:custom")
async def on_calc_custom(callback: CallbackQuery, state: FSMContext):
    await state.set_state(CalcState.value)
    await callback.message.answer(
        "Напиши сумму и период, я посчитаю, во что это выльется:\n\n"
        "<code>300 в день</code>\n"
        "<code>2000 в неделю</code>\n"
        "<code>8000 в месяц</code>\n\n"
        "Можно сложить несколько расходов: <code>1500 в неделю + 400 в месяц</code>"
    )
    await callback.answer()


@router.message(CalcState.value, FREE_TEXT)
async def on_calc_value(message: Message, state: FSMContext):
    per_day = parse_spend(message.text)
    user = db.get_user(message.from_user.id)
    if per_day is None or per_day <= 0 or user is None:
        await message.answer(
            "Не понял 🤔 Напиши, например: <code>300 в день</code> или <code>8000 в месяц</code>"
        )
        return
    await state.clear()
    await message.answer(
        "🧮 <b>Так это выглядит:</b>\n\n" + projection(user, per_day),
        reply_markup=kb([btn("✍️ Ещё раз", "calc:custom")]),
    )


# =====================================================================
#                             ДНЕВНИК
# =====================================================================

DIARY_KB = kb(
    [btn("➕ Новая запись", "dia:new")],
    [btn("📖 Записи", "dia:list"), btn("📈 Анализ", "dia:stats")],
)


def mean(values) -> float:
    values = list(values)
    return sum(values) / len(values)


def render_diary(user) -> tuple[str, InlineKeyboardMarkup | None]:
    rows = db.recent_diary(user["user_id"], 7)
    if not rows:
        return "📖 Записей пока нет.", None
    lines = ["📖 <b>Последние записи</b>"]
    for r in rows:
        line = f"<b>{date_label(user, r['ts'])}</b> {MOODS[r['mood']]} · {DIARY_TRIGGERS.get(r['trigger'], '')}"
        if r["note"]:
            note = r["note"] if len(r["note"]) <= 200 else r["note"][:200] + "…"
            line += f"\n{html.escape(note)}"
        lines.append(line)
    lines.append("Нажми на запись ниже, чтобы удалить её.")
    buttons = [[btn(f"❌ {date_label(user, r['ts'])} {MOODS[r['mood']]}", f"diadel:{r['id']}")] for r in rows]
    return "\n\n".join(lines), kb(*buttons)


def diary_analysis(user) -> str:
    rows = db.diary_all(user["user_id"])
    if len(rows) < 3:
        return "📈 Для анализа нужно хотя бы 3 записи в дневнике. Пока их " + str(len(rows)) + "."
    lines = [f"📈 <b>Что показывает дневник</b> ({len(rows)} {plural(len(rows), 'запись', 'записи', 'записей')})\n"]
    avg = mean(r["mood"] for r in rows)
    lines.append(f"Среднее настроение: <b>{avg:.1f}</b> из 5")

    week_ago = time.time() - 7 * 86400
    recent = [r["mood"] for r in rows if r["ts"] >= week_ago]
    older = [r["mood"] for r in rows if r["ts"] < week_ago]
    if recent and older:
        diff = mean(recent) - mean(older)
        trend = "лучше 📈" if diff > 0.2 else "хуже 📉" if diff < -0.2 else "примерно так же"
        lines.append(f"За последние 7 дней настроение {trend} ({mean(recent):.1f} против {mean(older):.1f} раньше)")

    by_trigger: dict[str, list[int]] = {}
    for r in rows:
        if r["trigger"] != "none":
            by_trigger.setdefault(r["trigger"], []).append(r["mood"])
    if by_trigger:
        lines.append("\n<b>Что чаще всего провоцирует:</b>")
        top = sorted(by_trigger.items(), key=lambda kv: len(kv[1]), reverse=True)[:5]
        lines += [
            f"{DIARY_TRIGGERS.get(key, key)} — {len(moods)} {plural(len(moods), 'раз', 'раза', 'раз')}, "
            f"настроение {mean(moods):.1f}"
            for key, moods in top
        ]

    bad = [r["smoked"] for r in rows if r["mood"] <= 2]
    good = [r["smoked"] for r in rows if r["mood"] >= 4]
    if bad and good:
        lines.append(
            f"\nВ плохие дни ты отмечаешь в среднем {mean(bad):.1f} за день, в хорошие — {mean(good):.1f}."
        )
        if mean(bad) > mean(good):
            lines.append("Тяга растёт вместе с плохим настроением — в такие дни заранее придумай замену сигарете.")
    return "\n".join(lines)


@router.message(Command("diary"))
@router.message(F.text == BTN_DIARY)
async def on_diary(message: Message, state: FSMContext):
    user = await need_user(message, state)
    if not user:
        return
    count = len(db.diary_all(user["user_id"]))
    await message.answer(
        "📓 <b>Дневник</b>\n\n"
        "Отмечай настроение и что подтолкнуло закурить — со временем станет видно, "
        f"что именно тебя провоцирует.\n\nЗаписей: {count}",
        reply_markup=DIARY_KB,
    )


@router.callback_query(F.data == "dia:new")
async def on_diary_new(callback: CallbackQuery):
    await callback.message.answer(
        "Как ты сейчас себя чувствуешь?",
        reply_markup=kb([btn(emoji, f"dm:{mood}") for mood, emoji in MOODS.items()]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("dm:"))
async def on_diary_mood(callback: CallbackQuery):
    mood = int(callback.data.split(":", 1)[1])
    buttons = [btn(label, f"dt:{mood}:{key}") for key, label in DIARY_TRIGGERS.items()]
    await callback.message.edit_text(
        f"{MOODS.get(mood, '')} Что сейчас происходит? Что тянет закурить?",
        reply_markup=kb(*[buttons[i:i + 2] for i in range(0, len(buttons), 2)]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("dt:"))
async def on_diary_trigger(callback: CallbackQuery, state: FSMContext):
    _, mood, trigger = callback.data.split(":", 2)
    if trigger not in DIARY_TRIGGERS or int(mood) not in MOODS:
        await callback.answer()
        return
    await state.set_state(DiaryState.note)
    await state.update_data(mood=int(mood), trigger=trigger)
    await callback.message.edit_text(
        f"{MOODS[int(mood)]} {DIARY_TRIGGERS[trigger]}\n\nНапиши пару слов: что случилось, что чувствуешь?",
        reply_markup=kb([btn("Пропустить", "dia:skip")]),
    )
    await callback.answer()


async def save_diary(message: Message, user_id: int, state: FSMContext, note: str):
    data = await state.get_data()
    await state.clear()
    user = db.get_user(user_id)
    if user is None or "mood" not in data:
        await message.answer("Не получилось сохранить, начни запись заново.")
        return
    smoked = db.count(user_id, "smoke", day_start(user_tz(user)))
    db.add_diary(user_id, data["mood"], data["trigger"], note[:1000], smoked)
    reply = (
        "Тяжёлые дни бывают у всех. Хорошо, что ты это записал — это уже шаг вперёд. 💙"
        if data["mood"] <= 2 else "Записал в дневник. Так держать! 🙂"
    )
    await message.answer(reply, reply_markup=kb([btn("📖 Записи", "dia:list"), btn("📈 Анализ", "dia:stats")]))


@router.message(DiaryState.note, FREE_TEXT)
async def on_diary_note(message: Message, state: FSMContext):
    await save_diary(message, message.from_user.id, state, message.text.strip())


@router.callback_query(F.data == "dia:skip")
async def on_diary_skip(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await save_diary(callback.message, callback.from_user.id, state, "")


@router.callback_query(F.data == "dia:list")
async def on_diary_list(callback: CallbackQuery):
    user = db.get_user(callback.from_user.id)
    if user:
        text, markup = render_diary(user)
        await callback.message.answer(text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data.startswith("diadel:"))
async def on_diary_delete(callback: CallbackQuery):
    user = db.get_user(callback.from_user.id)
    if not user:
        await callback.answer()
        return
    deleted = db.delete_diary(user["user_id"], int(callback.data.split(":", 1)[1]))
    text, markup = render_diary(user)
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer("Удалено" if deleted else "Уже удалено")


@router.callback_query(F.data == "dia:stats")
async def on_diary_stats(callback: CallbackQuery):
    user = db.get_user(callback.from_user.id)
    if user:
        await callback.message.answer(diary_analysis(user))
    await callback.answer()
