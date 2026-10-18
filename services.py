"""Общая логика для бота и API: посты в топики, стоп-лист, меню для гостей."""
import logging
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.types import Message

import sheets
from classify import CAT_ICON, exp_category, need_line
from config import CLOSE_HOUR, CLOSE_MIN, GROUP_ID, TOPIC_SPENT
from sheets import run
from specs import spec_names, tok_match
from utils import esc, fmt_money, now, parse_stamp, short, stamp

log = logging.getLogger("barbot.services")

bot: Bot | None = None   # выставляет main.py после создания Bot


def set_bot(b: Bot) -> None:
    global bot
    bot = b


async def post(topic: int | None, text: str) -> Message | None:
    if not GROUP_ID or bot is None:
        log.warning("GROUP_ID/бот не настроены — пост пропущен: %s", text[:60])
        return None
    return await bot.send_message(GROUP_ID, text, message_thread_id=topic)


async def stop_items() -> list[str]:
    return [r["item"] for r in await run(sheets.get().stoplist)]


async def stop_text() -> str:
    rows = await run(sheets.get().stoplist)
    if not rows:
        return "✅ Стоп-лист пуст"
    return "⛔ <b>Стоп-лист</b>\n" + "\n".join(
        f"• {esc(r['item'])} <i>— с {short(r['since'])}, {esc(r['who'])}</i>" for r in rows
    )


def spec_available(spec: dict, stop: list[str]) -> bool:
    return not any(tok_match(st, n) for st in stop for n in spec_names(spec))


async def menu_for_guests() -> list[dict]:
    """Авторская линия (тег «меню») с учётом стоп-листа — то, что увидит гость."""
    specs = await run(sheets.get().specs)
    stop = await stop_items()
    out = []
    for s in specs:
        tags = [t.strip() for t in str(s.get("tags", "")).split(";") if t.strip()]
        if "меню" not in [t.lower() for t in tags]:
            continue
        out.append({
            "name": s["name"],
            "pitch": s.get("pitch", ""),
            "method": s.get("method", ""),
            "glass": s.get("glass", ""),
            "garnish": s.get("garnish", ""),
            "tags": [t for t in tags if t.lower() != "меню"],
            "available": spec_available(s, stop),
        })
    return out


# ---------------------------------------------------------------- needs
async def close_need(nid: int, who: str) -> str:
    """Закрыть заявку и зачеркнуть её пост. Возвращает id строкой или '' если нет/уже закрыта."""
    rec = await run(sheets.get().need_close, nid, who)
    if not rec:
        return ""
    if not rec["already"] and rec.get("msg_id"):
        try:
            await bot.edit_message_text(need_line(rec, closed=True), chat_id=GROUP_ID, message_id=int(rec["msg_id"]))
        except Exception as e:
            log.warning("edit need post #%s: %s", nid, e)
    return str(nid)


# ---------------------------------------------------------------- expenses
def live_expenses(rows: list[dict]) -> list[dict]:
    """Строки с суммой и датой, не удалённые; добавляет _amt и _dt."""
    out = []
    for r in rows:
        if str(r.get("deleted", "")).strip():
            continue
        try:
            r["_amt"] = float(str(r.get("amount", "")).replace(",", "."))
        except ValueError:
            continue
        r["_dt"] = parse_stamp(r.get("date", ""))
        if r["_dt"]:
            out.append(r)
    return out


def between(rows: list[dict], a: datetime, b: datetime) -> list[dict]:
    return [r for r in rows if a <= r["_dt"] < b]


def by_category(rows: list[dict]) -> dict[str, float]:
    tot: dict[str, float] = {}
    for r in rows:
        c = r.get("category") or "прочее"
        tot[c] = round(tot.get(c, 0) + r["_amt"], 2)
    return dict(sorted(tot.items(), key=lambda x: -x[1]))


def by_category_text(rows: list[dict]) -> str:
    return " · ".join(f"{CAT_ICON.get(c, '•')} {c} {fmt_money(v)}" for c, v in by_category(rows).items())


def last_close(ref: datetime | None = None) -> datetime:
    ref = ref or now()
    c = ref.replace(hour=CLOSE_HOUR, minute=CLOSE_MIN, second=0, microsecond=0)
    return c if c <= ref else c - timedelta(days=1)


def period_bounds(period: str) -> tuple[datetime, datetime]:
    n = now()
    if period == "day":
        return last_close(), n
    if period == "week":
        return n - timedelta(days=7), n
    return n.replace(day=1, hour=0, minute=0, second=0, microsecond=0), n


def _item(r: dict) -> dict:
    return {
        "id": int(r["id"]), "date": r.get("date", ""), "who": r.get("who", ""),
        "amount": r.get("_amt"), "text": r.get("text", ""), "category": r.get("category", ""),
        "need_id": str(r.get("need_id", "")), "photo": r.get("photo", ""),
    }


async def expenses_summary(period: str) -> dict:
    """Для API и для /spent: итог, по категориям и записи за период + записи без суммы."""
    all_rows = await run(sheets.get().expenses)
    rows = live_expenses(all_rows)
    a, b = period_bounds(period)
    sel = between(rows, a, b)
    pending = [r for r in all_rows if not str(r.get("amount", "")).strip() and not str(r.get("deleted", "")).strip()]
    month = between(rows, *period_bounds("month"))
    return {
        "period": period, "from": a.isoformat(), "to": b.isoformat(),
        "total": round(sum(r["_amt"] for r in sel), 2), "count": len(sel),
        "by_category": by_category(sel),
        "items": [_item(r) for r in sorted(sel, key=lambda r: r["_dt"], reverse=True)],
        "pending": len(pending),
        "month_total": round(sum(r["_amt"] for r in month), 2),
    }


async def expense_create(who: str, amount: float, text: str, category: str | None = None,
                         need_id: str = "", source: str = "app") -> dict:
    """Запись расхода из приложения: строка в лист + пост в «Расходы», чтобы команда видела."""
    cat = category or exp_category(text)
    if need_id:
        need_id = await close_need(int(need_id), who) or ""
    eid = await run(sheets.get().expense_add, who, amount, text, cat, need_id, "", "")
    msg = await post(TOPIC_SPENT, f"{CAT_ICON.get(cat, '•')} {esc(text)} — <b>{fmt_money(amount)}</b>\n👤 {esc(who)} · {stamp()[:5]} · из приложения")
    if msg:
        found = await run(sheets.get().expense_by_id, eid)
        if found:
            await run(sheets.get().expense_set, found[0], msg_id=msg.message_id)
    return {"id": eid, "amount": amount, "text": text, "category": cat, "who": who, "need_id": need_id}


async def expense_update(eid: int, **fields) -> bool:
    found = await run(sheets.get().expense_by_id, eid)
    if not found:
        return False
    if "text" in fields and "category" not in fields:
        fields["category"] = exp_category(fields["text"])
    await run(sheets.get().expense_set, found[0], **fields)
    return True


async def expense_delete(eid: int) -> bool:
    return await expense_update(eid, deleted="да")
