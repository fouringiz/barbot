"""
Bar-bot: спеки / стоп-лист / заявки для группы с топиками.
Данные — Google Sheets (3 листа: specs, stoplist, needs).
"""
import asyncio
import html
import json
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

import gspread
from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import BotCommand, ErrorEvent, Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from rapidfuzz import fuzz, process

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("barbot")

# ---------------------------------------------------------------- config
TZ = ZoneInfo("Asia/Baku")
BOT_TOKEN = os.environ["BOT_TOKEN"]
GROUP_ID = int(os.environ.get("GROUP_ID", "0"))            # 0 = ещё не настроено, /id работает
TOPIC_STOP = int(os.environ.get("TOPIC_STOP", "0")) or None    # 0 = General
TOPIC_NEEDS = int(os.environ.get("TOPIC_NEEDS", "0")) or None
SHEET_ID = os.environ["SHEET_ID"]
CREDS = json.loads(os.environ["GOOGLE_CREDS_JSON"])

DATE_FMT = "%d.%m.%Y %H:%M"

# ---------------------------------------------------------------- utils
def norm(s) -> str:
    return re.sub(r"\s+", " ", str(s).replace("ё", "е").strip().lower())


def esc(s) -> str:
    return html.escape(str(s))


def now() -> datetime:
    return datetime.now(TZ)


def stamp(dt: datetime | None = None) -> str:
    return (dt or now()).strftime(DATE_FMT)


def parse_stamp(s: str) -> datetime | None:
    try:
        return datetime.strptime(str(s).strip(), DATE_FMT).replace(tzinfo=TZ)
    except ValueError:
        return None


# "сок лайма 25" -> "сок лайма"; "ангостура 2 дэш" -> "ангостура"
UNIT_RE = re.compile(
    r"\s*[\d.,/]+\s*(мл|ml|дэш\w*|dash\w*|шт|г|гр|бл|б\.?л\.?|ч\.?л\.?|капл\w*|top|топ)?\.?\s*$",
    re.I,
)


def ingr_name(part: str) -> str:
    return norm(UNIT_RE.sub("", part))


def spec_parts(spec: dict) -> list[str]:
    return [p.strip() for p in str(spec.get("ingredients", "")).split(";") if p.strip()]


def spec_names(spec: dict) -> list[str]:
    return [ingr_name(p) for p in spec_parts(spec)]


def tok_match(q: str, name: str) -> bool:
    """q — короткий запрос ('лайм'), name — имя ингредиента ('сок лайма')."""
    q, name = norm(q), norm(name)
    if not q:
        return False
    if re.search(r"(^|\s)" + re.escape(q), name):     # префикс по границе слова
        return True
    return any(fuzz.ratio(q, t) >= 85 for t in name.split())   # опечатки


def find_spec(specs: list[dict], query: str, cutoff: int = 70) -> dict | None:
    index: dict[str, dict] = {}
    for s in specs:
        keys = [s.get("name", "")] + str(s.get("aliases", "")).split(",")
        for k in keys:
            k = norm(k)
            if k:
                index[k] = s
    if not index:
        return None
    hit = process.extractOne(norm(query), list(index.keys()), scorer=fuzz.WRatio, score_cutoff=cutoff)
    return index[hit[0]] if hit else None


def card(spec: dict) -> str:
    lines = [f"🍸 <b>{esc(spec['name'])}</b>"]
    lines += [esc(p) for p in spec_parts(spec)]
    meta = " · ".join(esc(spec[k]) for k in ("method", "glass", "garnish") if spec.get(k))
    if meta:
        lines.append(f"<i>{meta}</i>")
    if spec.get("pitch"):
        lines.append(f"💬 {esc(spec['pitch'])}")
    return "\n".join(lines)


def affected(specs: list[dict], item: str) -> list[str]:
    return [s["name"] for s in specs if any(tok_match(item, n) for n in spec_names(s))]


# ---------------------------------------------------------------- needs classification
BREAK_KW = (
    "слома", "не работает", "не работают", "течет", "течь", "протек", "нет воды", "нет горячей",
    "нет света", "не включ", "сгорел", "глючит", "не морозит", "не холодит", "засор", "выбива",
    "искрит", "не греет", "трещин", "отвалил", "не закрыва", "не открыва",
)
EQUIP_KW = (
    "холодильник", "морозил", "льдогенератор", "блендер", "кофемашин", "посудомо", "техник",
    "шейкер", "джиггер", "стрейнер", "мадлер", "весы", "лампа", "светильник", "кран", "смесител",
    "бойлер", "водонагрев", "вытяжк", "кондиционер", "колонк", "принтер", "термина", "стул", "стол",
)
BAR_KW = (
    "кампари", "campari", "апероль", "aperol", "джин", "gin", "виски", "whisk", "бурбон", "ром ", "ром,", "rum",
    "текил", "tequila", "мескал", "водк", "vodka", "коньяк", "cognac", "бренди", "чача", "вермут", "vermouth",
    "ликер", "ликёр", "биттер", "ангостур", "пейшо", "абсент", "амаро", "фернет", "мараскино", "куантро",
    "трипл", "шартрез", "сироп", "сок", "пюре", "лайм", "лимон", "апельсин", "грейпфрут", "мят", "базилик",
    "тархун", "лед", "лёд", "тоник", "содов", "просекко", "шампанск", "вино", "пиво", "кола", "имбирн",
    "кофе", "эспрессо", "сливки", "белок", "яйц", "сахар", "мед ", "мёд", "соль", "гарнир", "вишн", "олив",
)
TYPE_ORDER = ("поломка", "оборудование", "бар", "расходник")
TYPE_ICON = {"поломка": "🔧", "оборудование": "📦", "бар": "🍾", "расходник": "🧻"}


def classify(text: str) -> str:
    t = norm(text)
    if any(k in t for k in BREAK_KW):
        return "поломка"
    if any(k in t for k in EQUIP_KW):
        return "оборудование"
    if any(k in (t + " ") for k in BAR_KW):
        return "бар"
    return "расходник"


def short(d: str) -> str:
    """02.10.2026 15:31 -> 02.10 15:31"""
    return re.sub(r"\.\d{4} ", " ", str(d))


def need_line(r: dict, closed: bool = False) -> str:
    t = r.get("type", "")
    icon = TYPE_ICON.get(t, "•")
    if closed:
        return (
            f"✅ <b>Заявка #{r['id']}</b> закрыта\n"
            f"<s>{esc(r['text'])}</s>\n"
            f"👤 {esc(r['closed_by'])} · {short(r['closed_at'])}"
        )
    label = f"<b>{esc(t).upper()}</b>" if t == "поломка" else esc(t)
    return (
        f"{icon} <b>Заявка #{r['id']}</b> · {label}\n"
        f"{esc(r['text'])}\n"
        f"👤 {esc(r['who'])} · {short(r['created'])}"
    )


# ---------------------------------------------------------------- sheets
class Sheets:
    def __init__(self):
        gc = gspread.service_account_from_dict(CREDS)
        self.book = gc.open_by_key(SHEET_ID)
        self._ensure_sheets()
        self._specs: list[dict] = []
        self._specs_at = datetime.min.replace(tzinfo=TZ)

    HEADERS = {
        "specs": ["name", "aliases", "ingredients", "method", "glass", "garnish", "pitch", "tags"],
        "stoplist": ["item", "since", "who", "note"],
        "needs": ["id", "type", "text", "who", "created", "closed_by", "closed_at", "msg_id"],
    }

    def _ensure_sheets(self):
        """Создаёт недостающие листы с заголовками; существующие не трогает."""
        existing = {w.title for w in self.book.worksheets()}
        for name, hdr in self.HEADERS.items():
            if name not in existing:
                w = self.book.add_worksheet(title=name, rows=200, cols=len(hdr))
                w.append_row(hdr)
                log.info("created sheet %s", name)

    def ws(self, name):
        return self.book.worksheet(name)

    # --- specs (кэш 10 мин)
    def specs(self, force: bool = False) -> list[dict]:
        if force or now() - self._specs_at > timedelta(minutes=10):
            self._specs = [r for r in self.ws("specs").get_all_records() if str(r.get("name", "")).strip()]
            self._specs_at = now()
            log.info("specs reloaded: %d", len(self._specs))
        return self._specs

    # --- stoplist
    def stoplist(self) -> list[dict]:
        return [r for r in self.ws("stoplist").get_all_records() if str(r.get("item", "")).strip()]

    def stop_add(self, item: str, who: str, note: str = ""):
        self.ws("stoplist").append_row([item, stamp(), who, note])

    def stop_remove(self, item: str) -> bool:
        ws = self.ws("stoplist")
        for i, r in enumerate(ws.get_all_records(), start=2):
            if norm(r.get("item")) == norm(item):
                ws.delete_rows(i)
                return True
        return False

    # --- needs
    def needs(self) -> list[dict]:
        return [r for r in self.ws("needs").get_all_records() if str(r.get("id", "")).strip()]

    def need_add(self, type_: str, text: str, who: str) -> int:
        ids = [int(r["id"]) for r in self.needs() if str(r["id"]).isdigit()]
        nid = max(ids, default=0) + 1
        self.ws("needs").append_row([nid, type_, text, who, stamp(), "", "", ""])
        return nid

    def need_set_msg(self, nid: int, msg_id: int):
        ws = self.ws("needs")
        cell = ws.find(str(nid), in_column=1)
        if cell:
            ws.update_cell(cell.row, 8, msg_id)

    def need_close(self, nid: int, who: str) -> dict | None:
        ws = self.ws("needs")
        cell = ws.find(str(nid), in_column=1)
        if not cell:
            return None
        row = ws.row_values(cell.row)
        row += [""] * (8 - len(row))
        rec = dict(zip(("id", "type", "text", "who", "created", "closed_by", "closed_at", "msg_id"), row))
        if rec["closed_at"]:
            rec["already"] = True
            return rec
        rec["closed_by"], rec["closed_at"] = who, stamp()
        ws.update_cell(cell.row, 6, who)
        ws.update_cell(cell.row, 7, rec["closed_at"])
        rec["already"] = False
        return rec


async def run(fn, *args):
    """gspread синхронный — уводим в тред, чтобы не блокировать бота."""
    return await asyncio.to_thread(fn, *args)


# ---------------------------------------------------------------- bot
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
sh = Sheets()


async def post(topic: int | None, text: str) -> Message | None:
    if not GROUP_ID:
        log.warning("GROUP_ID не задан — пост пропущен: %s", text[:60])
        return None
    return await bot.send_message(GROUP_ID, text, message_thread_id=topic)


def in_topic(m: Message, topic: int | None) -> bool:
    if m.chat.id != GROUP_ID:
        return False
    tid = m.message_thread_id if m.is_topic_message else None
    return tid == topic


class MembersOnly(BaseMiddleware):
    """Пускаем только сообщения из нашей группы и личку от её участников."""
    ALLOWED = {"creator", "administrator", "member", "restricted"}

    def __init__(self):
        self._cache: dict[int, tuple[bool, datetime]] = {}

    async def is_member(self, user_id: int) -> bool:
        ok, at = self._cache.get(user_id, (False, datetime.min.replace(tzinfo=TZ)))
        if now() - at < timedelta(minutes=10):
            return ok
        try:
            cm = await bot.get_chat_member(GROUP_ID, user_id)
            ok = cm.status in self.ALLOWED
        except Exception as e:
            log.warning("get_chat_member failed for %s: %s", user_id, e)
            ok = False
        self._cache[user_id] = (ok, now())
        return ok

    async def __call__(self, handler: Callable[[Message, dict], Awaitable[Any]], m: Message, data: dict):
        if not GROUP_ID:                      # ещё не настроен — пускаем всё, чтобы работал /id
            return await handler(m, data)
        if m.chat.id == GROUP_ID:
            return await handler(m, data)
        if m.chat.type == "private" and m.from_user and await self.is_member(m.from_user.id):
            return await handler(m, data)
        log.info("dropped message from chat %s user %s", m.chat.id, m.from_user.id if m.from_user else None)
        return None


dp.message.outer_middleware(MembersOnly())


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
    "<b>Если что-то не так</b>\n"
    "Не ответил — подожди 10 сек, я хожу в таблицу. «Не нашёл» на /spec — коктейля нет в карте, "
    "напиши в General. Ответил ⚠️ — скриншот Чингизу. /reload — после правок в таблице спеков."
)


@dp.message(Command("start", "help"))
async def cmd_help(m: Message):
    await m.reply(HELP)


@dp.message(Command("id"))
async def cmd_id(m: Message):
    tid = m.message_thread_id if m.is_topic_message else 0
    await m.reply(f"chat_id: <code>{m.chat.id}</code>\ntopic_id: <code>{tid}</code> (0 = General)")


@dp.message(Command("reload"))
async def cmd_reload(m: Message):
    specs = await run(sh.specs, True)
    await m.reply(f"Перечитал: {len(specs)} спеков.")


# --- specs
@dp.message(Command("spec"))
async def cmd_spec(m: Message, command: CommandObject):
    if not command.args:
        return await m.reply("Как: /spec негрони")
    specs = await run(sh.specs)
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
    stop = [r["item"] for r in await run(sh.stoplist)]
    text = card(s)
    dead = [n for n in spec_names(s) if any(tok_match(st, n) for st in stop)]
    if dead:
        text += f"\n⛔ В стопе: {esc(', '.join(dead))}"
    await m.reply(text)


@dp.message(Command("have"))
async def cmd_have(m: Message, command: CommandObject):
    if not command.args:
        return await m.reply("Как: /have джин лайм")
    tokens = [t for t in re.split(r"[,\s]+", command.args) if t]
    specs = await run(sh.specs)
    stop = [r["item"] for r in await run(sh.stoplist)]
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


@dp.message(Command("menu"))
async def cmd_menu(m: Message):
    specs = await run(sh.specs)
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


# --- stoplist
async def stop_text() -> str:
    rows = await run(sh.stoplist)
    if not rows:
        return "✅ Стоп-лист пуст"
    return "⛔ <b>Стоп-лист</b>\n" + "\n".join(
        f"• {esc(r['item'])} <i>— с {short(r['since'])}, {esc(r['who'])}</i>" for r in rows
    )


@dp.message(Command("stop"))
async def cmd_stop(m: Message, command: CommandObject):
    args = (command.args or "").strip()
    if not args:
        return await m.reply(await stop_text())
    sign, item = args[0], args[1:].strip()
    if sign not in "+-" or not item:
        return await m.reply("Как: /stop + лайм  или  /stop - лайм")
    specs = await run(sh.specs)
    if sign == "+":
        current = [norm(r["item"]) for r in await run(sh.stoplist)]
        if norm(item) in current:
            return await m.reply(f"«{esc(item)}» уже в стопе.")
        await run(sh.stop_add, item, who(m))
        text = f"⛔ <b>В стопе:</b> {esc(item)} — {esc(who(m))}, {stamp()}"
        hit = affected(specs, item)
        if hit:
            text += "\nПадают: " + esc(", ".join(hit))
    else:
        if not await run(sh.stop_remove, item):
            return await m.reply(f"«{esc(item)}» в стопе нет.")
        text = f"✅ <b>Снят со стопа:</b> {esc(item)} — {esc(who(m))}, {stamp()}"
    await post(TOPIC_STOP, text)
    if not in_topic(m, TOPIC_STOP):
        await m.reply("Готово, запостил в «Стоп-лист».")


# --- needs
@dp.message(Command("need"))
async def cmd_need(m: Message, command: CommandObject):
    text = (command.args or "").strip()
    if not text:
        return await m.reply("Как: /need туалетная бумага  /  /need нет горячей воды")
    type_ = classify(text)
    nid = await run(sh.need_add, type_, text, who(m))
    rec = {"id": nid, "type": type_, "text": text, "who": who(m), "created": stamp()}
    msg = await post(TOPIC_NEEDS, need_line(rec))
    if msg:
        await run(sh.need_set_msg, nid, msg.message_id)
    if not in_topic(m, TOPIC_NEEDS):
        await m.reply(f"Заявка #{nid} ({type_}) принята, в «Заявках».")


@dp.message(Command("needs"))
async def cmd_needs(m: Message):
    rows = [r for r in await run(sh.needs) if not str(r.get("closed_at", "")).strip()]
    if not rows:
        return await m.reply("✅ Открытых заявок нет")
    out = ["📋 <b>Открытые заявки</b>"]
    for t in TYPE_ORDER:
        group = [r for r in rows if r.get("type") == t]
        if not group:
            continue
        out.append(f"\n{TYPE_ICON[t]} <b>{t}</b>")
        out += [f"#{r['id']} {esc(r['text'])} <i>— {esc(r['who'])}, {short(r['created'])}</i>" for r in group]
    out.append("\nЗакрыть: /done <номер>")
    await m.reply("\n".join(out))


@dp.message(Command("done"))
async def cmd_done(m: Message, command: CommandObject):
    arg = (command.args or "").strip().lstrip("#")
    if not arg.isdigit():
        return await m.reply("Как: /done 14")
    rec = await run(sh.need_close, int(arg), who(m))
    if rec is None:
        return await m.reply(f"Заявки #{arg} нет.")
    if rec["already"]:
        return await m.reply(f"#{arg} уже закрыта ({esc(rec['closed_by'])}, {esc(rec['closed_at'])}).")
    if rec.get("msg_id"):
        try:
            await bot.edit_message_text(
                need_line(rec, closed=True), chat_id=GROUP_ID, message_id=int(rec["msg_id"])
            )
        except Exception as e:  # пост могли удалить / старше 48ч
            log.warning("edit failed for #%s: %s", arg, e)
            await post(TOPIC_NEEDS, need_line(rec, closed=True))
    await m.reply(f"✅ #{arg} закрыта.")


# --- scheduler jobs
async def job_morning_stop():
    await post(TOPIC_STOP, "🕚 Утренняя сводка\n" + await stop_text())


async def job_needs_digest():
    rows = [r for r in await run(sh.needs) if not str(r.get("closed_at", "")).strip()]
    cutoff = now() - timedelta(days=3)
    stale = [r for r in rows if (d := parse_stamp(r.get("created", ""))) and d < cutoff]
    if not stale:
        return
    lines = [f"#{r['id']} {TYPE_ICON.get(r['type'], '•')} {esc(r['text'])} — висит с {short(r['created'])}" for r in stale]
    await post(TOPIC_NEEDS, "📌 <b>Висит больше 3 дней</b>\n" + "\n".join(lines) + "\n\nЗакрыть: /done <номер>")


# --- errors
@dp.errors()
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


async def main():
    await bot.set_my_commands([
        BotCommand(command="spec", description="карточка коктейля"),
        BotCommand(command="have", description="что собрать из ингредиентов"),
        BotCommand(command="menu", description="авторская линия"),
        BotCommand(command="stop", description="стоп-лист: /stop, /stop + x, /stop - x"),
        BotCommand(command="need", description="заявка: расходник / техника / поломка"),
        BotCommand(command="needs", description="открытые заявки"),
        BotCommand(command="done", description="закрыть заявку по номеру"),
        BotCommand(command="help", description="справка"),
    ])
    sched = AsyncIOScheduler(timezone=TZ)
    sched.add_job(job_morning_stop, CronTrigger(hour=11, minute=0))
    sched.add_job(job_needs_digest, CronTrigger(day_of_week="mon", hour=11, minute=5))
    sched.start()
    await bot.delete_webhook(drop_pending_updates=True)
    log.info("polling started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
