"""Командный бот: команды в группе, топики, дайджесты. Вся бизнес-логика — через services."""
import asyncio
import csv
import io
import logging
import re
from datetime import timedelta
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (BotCommand, BufferedInputFile, ErrorEvent, InlineKeyboardButton, InlineKeyboardMarkup, Message,
                           ReactionTypeEmoji)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import services as svc
import sheets
from classify import NEED_RE, TYPE_ICON, classify, exp_category, is_dismiss, parse_amount
from config import APP_LINK, CLOSE_HOUR, CLOSE_MIN, GROUP_ID, TOPIC_NEEDS, TOPIC_SPENT, TOPIC_STOP
from sheets import run
from specs import card, find_spec, spec_names, spec_parts, tok_match
from utils import esc, fmt_money, norm, now, parse_stamp, ru_day, short, RU_MONTHS

log = logging.getLogger("barbot.team")
router = Router(name="team")

COMMANDS = [
    BotCommand(command="spec", description="карточка коктейля"),
    BotCommand(command="have", description="что собрать из ингредиентов"),
    BotCommand(command="menu", description="авторская линия"),
    BotCommand(command="stop", description="стоп-лист: /stop, /stop + x, /stop - x"),
    BotCommand(command="need", description="заявка: расходник / техника / поломка"),
    BotCommand(command="needs", description="открытые заявки"),
    BotCommand(command="done", description="закрыть заявку по номеру"),
    BotCommand(command="spent", description="расходы: сегодня / неделя / месяц"),
    BotCommand(command="app", description="открыть приложение"),
    BotCommand(command="help", description="справка"),
]


def register_jobs(sched: AsyncIOScheduler) -> None:
    sched.add_job(job_morning_stop, CronTrigger(hour=11, minute=0))
    sched.add_job(job_needs_digest, CronTrigger(day_of_week="mon", hour=11, minute=5))
    if TOPIC_SPENT:
        sched.add_job(job_daily_spent, CronTrigger(hour=CLOSE_HOUR, minute=CLOSE_MIN))
        sched.add_job(job_weekly_spent, CronTrigger(day_of_week="mon", hour=11, minute=10))
        sched.add_job(job_monthly_spent, CronTrigger(day=1, hour=11, minute=15))


def in_topic(m: Message, topic: int | None) -> bool:
    if m.chat.id != GROUP_ID:
        return False
    tid = m.message_thread_id if m.is_topic_message else None
    return tid == topic


class GroupOnly(BaseMiddleware):
    """Работаем только внутри нашей группы. Личка и чужие чаты — молча мимо."""

    async def __call__(self, handler: Callable[[Message, dict], Awaitable[Any]], m: Message, data: dict):
        if not GROUP_ID or m.chat.id == GROUP_ID:   # GROUP_ID пуст = ещё настраиваем, нужен /id
            return await handler(m, data)
        log.info("dropped message from chat %s", m.chat.id)
        return None


router.message.outer_middleware(GroupOnly())


def who(m: Message) -> str:
    return m.from_user.first_name if m.from_user else "кто-то"


HELP = (
    "🤖 <b>Бот бара: что умеет</b>\n"
    "Рецепты, стоп-лист, заявки. Пиши в любом топике — отвечу там же. "
    "Названия как угодно: negroni / негрони / олдфешен, опечатки прощаю.\n\n"
    "<b>Ситуация → команда</b>\n"
    "• Гость спросил, что в коктейле → /spec пейпер плейн\n"
    "• Остались джин и лайм → /have джин лайм\n"
    "• Авторская карта → /menu\n"
    "• Кончились лаймы → /stop + сок лайма\n"
    "• Привезли → /stop - сок лайма\n"
    "• Что в стопе → /stop\n"
    "• Кончилась бумага / сломался кран / нужен Campari → /need …\n"
    "• Купил или починил → /done номер\n"
    "• Что открыто → /needs\n\n"
    "<b>Стоп-лист</b>\n"
    "Ставь сразу, как увидел. Называй как в рецептах («сок лайма», не «лаймы») — "
    "тогда покажу, какие коктейли упали. Снимай, как только позиция на баре. "
    "Каждое утро в 11:00 публикую актуальный список.\n\n"
    "<b>Заявки</b>\n"
    "Одна заявка — одна вещь. Пиши количество и срочность в тексте. Тип ставлю сам:\n"
    "🔧 поломка · 📦 оборудование · 🍾 бар · 🧻 расходник\n"
    "По понедельникам напоминаю о заявках старше 3 дней.\n\n"
    "<b>Расходы</b> — топик «Расходы», команды не нужны\n"
    "Пиши «лаймы 12.50» или фото чека с подписью «47.80 bravo» — ставлю ✅ и записываю. "
    "Без суммы спрошу — ответь числом. «#14» в тексте закроет заявку 14. "
    "Ошибся — ответь ❌ на запись. /spent — итог сейчас; итог дня постится при закрытии, "
    "недели — в понедельник, месяца — 1-го с CSV.\n\n"
    "<b>Если что-то не так</b>\n"
    "Не ответил — подожди 10 сек, я хожу в таблицу. «Не нашёл» на /spec — коктейля нет в карте, "
    "напиши в General. Ответил ⚠️ — скриншот Чингизу. /reload — после правок в таблице спеков."
)


@router.message(Command("start", "help"))
async def cmd_help(m: Message):
    await m.reply(HELP)


@router.message(Command("app"))
async def cmd_app(m: Message):
    if not APP_LINK:
        return await m.reply("Приложение ещё не подключено: BotFather → /newapp → URL фронта, затем APP_LINK в переменные.")
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть приложение", url=APP_LINK)]])
    await m.reply("Расходы, заявки, стоп-лист — в приложении:", reply_markup=kb)


@router.message(Command("id"))
async def cmd_id(m: Message):
    tid = m.message_thread_id if m.is_topic_message else 0
    await m.reply(f"chat_id: <code>{m.chat.id}</code>\ntopic_id: <code>{tid}</code> (0 = General)")


@router.message(Command("reload"))
async def cmd_reload(m: Message):
    specs = await run(sheets.get().specs, True)
    await m.reply(f"Перечитал: {len(specs)} спеков.")


# --- specs
@router.message(Command("spec"))
async def cmd_spec(m: Message, command: CommandObject):
    if not command.args:
        return await m.reply("Как: /spec негрони")
    specs = await run(sheets.get().specs)
    if not specs:
        return await m.reply(
            "В листе <b>specs</b> ни одной строки. Проверь: лист называется именно specs, "
            "в первой строке заголовки name, aliases, ingredients…, потом /reload."
        )
    s = find_spec(specs, command.args)
    if not s:
        return await m.reply(
            f"Не нашёл «{esc(command.args)}» среди {len(specs)} спеков. Проверь имя или добавь alias в таблицу."
        )
    stop = [r["item"] for r in await run(sheets.get().stoplist)]
    text = card(s)
    dead = [n for n in spec_names(s) if any(tok_match(st, n) for st in stop)]
    if dead:
        text += f"\n⛔ В стопе: {esc(', '.join(dead))}"
    await m.reply(text)


@router.message(Command("have"))
async def cmd_have(m: Message, command: CommandObject):
    if not command.args:
        return await m.reply("Как: /have джин лайм")
    tokens = [t for t in re.split(r"[,\s]+", command.args) if t]
    specs = await run(sheets.get().specs)
    stop = [r["item"] for r in await run(sheets.get().stoplist)]
    found = []
    for s in specs:
        names = spec_names(s)
        if not all(any(tok_match(t, n) for n in names) for t in tokens):
            continue
        if any(any(tok_match(st, n) for n in names) for st in stop):
            continue
        found.append(s)
    if not found:
        return await m.reply("Ничего с таким набором (или всё упёрлось в стоп-лист).")
    found.sort(key=lambda s: len(spec_parts(s)))
    lines = [f"• <b>{esc(s['name'])}</b> — {esc(', '.join(spec_names(s)))}" for s in found[:15]]
    tail = f"\n…и ещё {len(found) - 15}" if len(found) > 15 else ""
    await m.reply(f"С «{esc(' '.join(tokens))}» можно:\n" + "\n".join(lines) + tail)


@router.message(Command("menu"))
async def cmd_menu(m: Message):
    specs = await run(sheets.get().specs)
    house = [s for s in specs if "меню" in norm(s.get("tags", ""))]
    if not house:
        return await m.reply("В таблице нет спеков с тегом «меню».")
    lines = []
    for s in house:
        line = f"• <b>{esc(s['name'])}</b>"
        if s.get("pitch"):
            line += f" — {esc(s['pitch'])}"
        lines.append(line)
    await m.reply("🍸 <b>Авторская линия</b>\n" + "\n".join(lines))


@router.message(Command("stop"))
async def cmd_stop(m: Message, command: CommandObject):
    args = (command.args or "").strip()
    if not args:
        return await m.reply(await svc.stop_text())
    sign, item = args[0], args[1:].strip()
    if sign not in "+-" or not item:
        return await m.reply("Как: /stop + лайм  или  /stop - лайм")
    if sign == "+":
        if await svc.stop_add(item, who(m)) is None:
            return await m.reply(f"«{esc(item)}» уже в стопе.")
    else:
        if not await svc.stop_remove(item, who(m)):
            return await m.reply(f"«{esc(item)}» в стопе нет.")
    if not in_topic(m, TOPIC_STOP):
        await m.reply("Готово, запостил в «Стоп-лист».")


# --- needs
@router.message(Command("need"))
async def cmd_need(m: Message, command: CommandObject):
    text = (command.args or "").strip()
    if not text:
        return await m.reply("Как: /need туалетная бумага  /  /need нет горячей воды")
    type_ = classify(text)
    nid = await svc.add_need(type_, text, who(m))
    if not in_topic(m, TOPIC_NEEDS):
        await m.reply(f"Заявка #{nid} ({type_}) принята, в «Заявках».")


@router.message(Command("needs"))
async def cmd_needs(m: Message):
    rows = [r for r in await run(sheets.get().needs) if not str(r.get("closed_at", "")).strip()]
    if not rows:
        return await m.reply("✅ Открытых заявок нет")
    for page in svc.needs_pages(rows):
        await m.reply(page)


@router.message(Command("done"))
async def cmd_done(m: Message, command: CommandObject):
    arg = (command.args or "").strip().lstrip("#")
    if not arg.isdigit():
        return await m.reply("Как: /done 14")
    rec = await svc.need_close(int(arg), who(m), strict_summary=False)
    if rec is None:
        return await m.reply(f"Заявки #{arg} нет.")
    if rec["already"]:
        return await m.reply(f"#{arg} уже закрыта ({esc(rec['closed_by'])}, {esc(rec['closed_at'])}).")
    await m.reply(f"✅ #{arg} закрыта.")


def msg_link(m: Message) -> str:
    cid = str(m.chat.id).removeprefix("-100")
    tid = m.message_thread_id if m.is_topic_message else None
    return f"https://t.me/c/{cid}/{tid}/{m.message_id}" if tid else f"https://t.me/c/{cid}/{m.message_id}"


async def react(m: Message, emoji: str):
    try:
        await svc.bot.set_message_reaction(m.chat.id, m.message_id, reaction=[ReactionTypeEmoji(emoji=emoji)])
    except Exception as e:
        log.warning("reaction failed: %s", e)


async def close_need_from_text(text: str, who_: str, eid: int) -> str:
    """#14 в тексте → закрываем заявку, возвращаем need_id для строки расхода."""
    nm = NEED_RE.search(text)
    if not nm:
        return ""
    await svc.need_close(int(nm.group(1)), who_, strict_summary=False)
    return nm.group(1)


PROMPTS: dict[int, int] = {}        # msg_id вопроса «Сумма?» -> msg_id записи (живёт до рестарта)
MEDIA_GROUPS: dict[str, int] = {}   # media_group_id альбома -> msg_id первой записи


def is_spent_topic(m: Message) -> bool:
    return bool(TOPIC_SPENT) and m.chat.id == GROUP_ID and m.is_topic_message and m.message_thread_id == TOPIC_SPENT


def not_command(m: Message) -> bool:
    return not (m.text or m.caption or "").startswith("/")


@router.message(F.func(is_spent_topic), F.reply_to_message, F.func(not_command))
async def spent_reply(m: Message):
    """Реплай на запись (или на вопрос «Сумма?»): сумма — дописать, ❌ — удалить."""
    text = (m.text or m.caption or "").strip()
    target = m.reply_to_message
    if target.message_id == m.message_thread_id:        # не реплай, а обычное сообщение в топике
        return await spent_new(m)
    target_id = PROMPTS.get(target.message_id, target.message_id)
    from_bot = target.from_user and target.from_user.id == svc.bot.id
    found = await run(sheets.get().expense_by_msg, target_id)
    if not found:
        return                                           # реплай на разговор или на сводку бота — не запись
    row_idx, rec = found
    if is_dismiss(text):
        await run(sheets.get().expense_set, row_idx, deleted="да")
        await react(m, "👌")
        if from_bot:                                     # убираем свой вопрос «Сумма?»
            try:
                await target.delete()
            except Exception:
                pass
        return
    amt = parse_amount(text)
    if amt is None:
        return await react(m, "❓")
    await run(sheets.get().expense_set, row_idx, amount=amt)
    await react(m, "✅")
    if not from_bot:
        await react(target, "✅")


@router.message(F.func(is_spent_topic), F.text | F.photo | F.document, F.func(not_command))
async def spent_new(m: Message):
    text = (m.text or m.caption or "").strip()
    amt = parse_amount(text)
    photo = msg_link(m) if (m.photo or m.document) else ""
    if not photo and amt is None:
        return                                            # разговор в топике — не запись
    if m.media_group_id:                                  # альбом: одна запись на все фото
        first = MEDIA_GROUPS.get(m.media_group_id)
        if first is not None:
            if amt is not None:                           # подпись пришла на этом фото — дописываем первую запись
                found = await run(sheets.get().expense_by_msg, first)
                if found and not str(found[1].get("amount", "")).strip():
                    await run(sheets.get().expense_set, found[0], amount=amt, text=text, category=exp_category(text))
                    await react(m, "✅")
            return
        MEDIA_GROUPS[m.media_group_id] = m.message_id
    if not text:
        text = "чек"
    need_id = ""
    eid = await run(sheets.get().expense_add, who(m), amt, text, exp_category(text), "", photo, m.message_id)
    if NEED_RE.search(text):
        need_id = await close_need_from_text(text, who(m), eid)
        if need_id:
            found = await run(sheets.get().expense_by_msg, m.message_id)
            if found:
                await run(sheets.get().expense_set, found[0], need_id=need_id)
    if amt is None:
        asyncio.create_task(ask_amount_later(m))
    else:
        await react(m, "✅")


async def ask_amount_later(m: Message):
    """Фото без суммы: ждём секунду — в альбоме подпись может быть на другом фото."""
    await asyncio.sleep(2 if m.media_group_id else 0)
    found = await run(sheets.get().expense_by_msg, m.message_id)
    if found and str(found[1].get("amount", "")).strip():
        return
    await react(m, "❓")
    q = await m.reply("Сумма? Ответь числом — на это сообщение или на фото.")
    PROMPTS[q.message_id] = m.message_id


@router.edited_message(F.func(is_spent_topic))
async def spent_edited(m: Message):
    found = await run(sheets.get().expense_by_msg, m.message_id)
    if not found:
        return
    text = (m.text or m.caption or "").strip() or "чек"
    amt = parse_amount(text)
    await run(sheets.get().expense_set, found[0], text=text, amount=amt if amt is not None else "", category=exp_category(text))
    await react(m, "✅" if amt is not None else "❓")


@router.message(Command("del"))
async def cmd_del(m: Message):
    if not m.reply_to_message:
        return await m.reply("Ответь /del на запись, которую убрать.")
    found = await run(sheets.get().expense_by_msg, m.reply_to_message.message_id)
    if not found:
        return await m.reply("Это не запись расхода.")
    await run(sheets.get().expense_set, found[0], deleted="да")
    await react(m.reply_to_message, "👌")


async def spent_summary(period: str) -> str:
    rows = svc.live_expenses(await run(sheets.get().expenses))
    n = now()
    if period == "day":
        a, b = svc.last_close(), n
    elif period == "week":
        a, b = n - timedelta(days=7), n
    else:
        a, b = n.replace(day=1, hour=0, minute=0, second=0, microsecond=0), n
    sel = svc.between(rows, a, b)
    total = sum(r["_amt"] for r in sel)
    return f"{fmt_money(total)} · {len(sel)} зап." + (f"\n{svc.by_category_text(sel)}" if sel else "")


@router.message(Command("spent"))
async def cmd_spent(m: Message):
    if not TOPIC_SPENT:
        return await m.reply("Модуль расходов не настроен (TOPIC_SPENT).")
    d, w, mo = await spent_summary("day"), await spent_summary("week"), await spent_summary("month")
    await m.reply(f"💸 <b>Сегодня</b>: {d}\n\n<b>7 дней</b>: {w}\n\n<b>Месяц</b>: {mo}")


async def job_daily_spent():
    rows = svc.live_expenses(await run(sheets.get().expenses))
    n = now()
    day = svc.between(rows, n - timedelta(days=1), n)
    if not day:
        return
    month = svc.between(rows, n.replace(day=1, hour=0, minute=0, second=0, microsecond=0), n)
    label = ru_day(n - timedelta(hours=CLOSE_HOUR + 1))
    lines = [f"💸 <b>Итог дня · {label}</b>",
             f"{fmt_money(sum(r['_amt'] for r in day))} · {len(day)} зап.", svc.by_category_text(day), ""]
    lines += [f"• {esc(r['text'])} — {fmt_money(r['_amt'])} <i>({esc(r['who'])})</i>" for r in day]
    pending = [r for r in await run(sheets.get().expenses) if not str(r.get("amount", "")).strip() and not str(r.get("deleted", "")).strip()]
    if pending:
        lines += ["", f"❓ Без суммы: {len(pending)} — ответьте числом на эти записи"]
    nodesc = [r for r in day if r.get("category") == "без описания"]
    if nodesc:
        lines += [f"❔ Без описания: {len(nodesc)} — отредактируйте, добавьте что куплено"]
    lines += ["", f"За месяц: <b>{fmt_money(sum(r['_amt'] for r in month))}</b>"]
    await svc.post(TOPIC_SPENT, "\n".join(lines))


async def job_weekly_spent():
    rows = svc.live_expenses(await run(sheets.get().expenses))
    n = now()
    week = svc.between(rows, n - timedelta(days=7), n)
    prev = svc.between(rows, n - timedelta(days=14), n - timedelta(days=7))
    if not week:
        return
    tw, tp = sum(r["_amt"] for r in week), sum(r["_amt"] for r in prev)
    delta = f" ({'+' if tw >= tp else ''}{(tw - tp) / tp * 100:.0f}% к прошлой)" if tp else ""
    days: dict[str, float] = {}
    for r in week:
        k = ru_day(r["_dt"])
        days[k] = days.get(k, 0) + r["_amt"]
    top = sorted(week, key=lambda r: -r["_amt"])[:5]
    lines = [f"📊 <b>Неделя</b>: {fmt_money(tw)} · {len(week)} зап.{delta}", svc.by_category_text(week), ""]
    lines += [f"{k}: {fmt_money(v)}" for k, v in days.items()]
    lines += ["", "<b>Крупные:</b>"] + [f"• {esc(r['text'])} — {fmt_money(r['_amt'])}" for r in top]
    nophoto = [r for r in week if not str(r.get("photo", "")).strip() and r["_amt"] >= 20]
    if nophoto:
        lines += ["", "📷 Без чека от 20 ₼: " + ", ".join(f"#{r['id']}" for r in nophoto)]
    await svc.post(TOPIC_SPENT, "\n".join(lines))


async def job_monthly_spent():
    rows = svc.live_expenses(await run(sheets.get().expenses))
    n = now()
    first_this = n.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    first_prev = (first_this - timedelta(days=1)).replace(day=1)
    month = svc.between(rows, first_prev, first_this)
    if not month:
        return
    total = sum(r["_amt"] for r in month)
    await svc.post(TOPIC_SPENT, f"🗓 <b>{RU_MONTHS[first_prev.month - 1]} {first_prev.year}</b>: {fmt_money(total)} · {len(month)} зап.\n{svc.by_category_text(month)}")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "who", "amount", "text", "category", "need_id", "photo"])
    for r in month:
        w.writerow([r["date"], r["who"], r["_amt"], r["text"], r["category"], r.get("need_id", ""), r.get("photo", "")])
    data = buf.getvalue().encode("utf-8-sig")
    await svc.bot.send_document(GROUP_ID, BufferedInputFile(data, f"expenses_{first_prev.strftime('%Y-%m')}.csv"),
                            message_thread_id=TOPIC_SPENT, caption="CSV для бухгалтера")


# --- scheduler jobs
async def job_morning_stop():
    await svc.post(TOPIC_STOP, "🕚 Утренняя сводка\n" + await svc.stop_text())


async def job_needs_digest():
    rows = [r for r in await run(sheets.get().needs) if not str(r.get("closed_at", "")).strip()]
    cutoff = now() - timedelta(days=3)
    stale = [r for r in rows if (d := parse_stamp(r.get("created", ""))) and d < cutoff]
    if not stale:
        return
    lines = [f"#{r['id']} {TYPE_ICON.get(r['type'], '•')} {esc(r['text'])} — висит с {short(r['created'])}" for r in stale]
    await svc.post(TOPIC_NEEDS, "📌 <b>Висит больше 3 дней</b>\n" + "\n".join(lines) + "\n\nЗакрыть: /done &lt;номер&gt;")


# --- errors
@router.errors()
async def on_error(event: ErrorEvent):
    log.exception("update failed: %s", event.exception)
    msg = event.update.message if event.update else None
    if msg:
        e = event.exception
        name = type(e).__name__
        hint = {
            "WorksheetNotFound": "нет листа с таким именем — проверь: specs / stoplist / needs, латиницей, без пробелов",
            "SpreadsheetNotFound": "таблица не найдена — проверь SHEET_ID или доступ для client_email",
            "APIError": "Google отказал — таблица не расшарена на client_email (Редактор) или не включён Sheets API",
            "KeyError": "в листе нет нужного столбца — проверь заголовки первой строки",
        }.get(name, "")
        text = f"⚠️ {name}: {esc(str(e)[:200])}"
        if hint:
            text += f"\n💡 {hint}"
        try:
            await msg.reply(text)
        except Exception:
            pass
