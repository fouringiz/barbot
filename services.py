"""Общая логика для бота и API: посты в топики, стоп-лист, меню для гостей."""
import asyncio
import hashlib
import json
import logging
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

import sheets
from classify import CAT_ICON, TYPE_ICON, TYPE_ORDER, exp_category, need_line
from config import CLOSE_HOUR, CLOSE_MIN, GROUP_ID, TOPIC_NEEDS, TOPIC_SPENT, TOPIC_STOP
from sheets import run
from specs import affected, spec_names, spec_parts, tok_match
from utils import esc, fmt_money, norm, now, parse_stamp, short, stamp

log = logging.getLogger("barbot.services")

bot: Bot | None = None   # выставляет main.py после создания Bot
_outbox_wakeup = asyncio.Event()
_outbox_lock = asyncio.Lock()
_needs_lock = asyncio.Lock()


def set_bot(b: Bot) -> None:
    global bot
    bot = b


async def post(topic: int | None, text: str) -> Message | None:
    if not GROUP_ID or bot is None:
        log.warning("GROUP_ID/бот не настроены — пост пропущен: %s", text[:60])
        return None
    return await bot.send_message(GROUP_ID, text, message_thread_id=topic)


async def audit(who: str, action: str, entity: str, entity_id, before="", after="", source="bot") -> None:
    """Запись в журнал; сбой журнала никогда не ломает основное действие."""
    try:
        await run(sheets.get().audit_log, who, action, entity, entity_id, before, after, source)
    except Exception:
        log.exception("audit failed: %s %s %s", action, entity, entity_id)


async def audit_tail(limit: int = 50) -> list[dict]:
    return await run(sheets.get().audit_tail, limit)


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


# ---------------------------------------------------------------- stop-list
async def stop_add(item: str, who: str, source: str = "bot") -> dict | None:
    """Поставить в стоп + пост с упавшими коктейлями. None — уже в стопе."""
    current = [norm(r["item"]) for r in await run(sheets.get().stoplist)]
    if norm(item) in current:
        return None
    await run(sheets.get().stop_add, item, who)
    await audit(who, "stop_add", "stop", item, "", item, source)
    hit = affected(await run(sheets.get().specs), item)
    text = f"⛔ <b>В стопе:</b> {esc(item)} — {esc(who)}, {short(stamp())}"
    if hit:
        text += "\nПадают: " + esc(", ".join(hit))
    await post(TOPIC_STOP, text)
    return {"item": item, "affected": hit}


async def stop_remove(item: str, who: str, source: str = "bot") -> bool:
    if not await run(sheets.get().stop_remove, item):
        return False
    await audit(who, "stop_remove", "stop", item, item, "", source)
    await post(TOPIC_STOP, f"✅ <b>Снят со стопа:</b> {esc(item)} — {esc(who)}, {short(stamp())}")
    return True


async def stop_state() -> dict:
    """Для экрана стоп-листа: что в стопе (с упавшими коктейлями) + все ингредиенты карты."""
    specs = await run(sheets.get().specs)
    rows = await run(sheets.get().stoplist)
    names = sorted({n for sp in specs for n in spec_names(sp)})
    in_stop = [norm(r["item"]) for r in rows]
    return {
        "stop": [{"item": r["item"], "since": short(r.get("since", "")), "who": r.get("who", ""),
                  "affected": affected(specs, r["item"])} for r in rows],
        "ingredients": [n for n in names if not any(tok_match(st, n) for st in in_stop)],
    }


async def specs_all() -> list[dict]:
    specs = await run(sheets.get().specs)
    stop = await stop_items()
    out = []
    for sp in specs:
        names = spec_names(sp)
        out.append({
            "name": sp["name"], "aliases": [a.strip() for a in str(sp.get("aliases", "")).split(",") if a.strip()],
            "ingredients": spec_parts(sp), "method": sp.get("method", ""), "glass": sp.get("glass", ""),
            "garnish": sp.get("garnish", ""), "pitch": sp.get("pitch", ""),
            "tags": [t.strip() for t in str(sp.get("tags", "")).split(";") if t.strip()],
            "dead": [n for n in names if any(tok_match(st, n) for st in stop)],
        })
    return out


# ---------------------------------------------------------------- needs
def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def needs_lines(rows: list[dict]) -> list[str]:
    """Build escaped display rows, clipping only presentation text, never sheet data."""
    lines = ["📋 <b>Открытые заявки</b>"]
    for type_ in TYPE_ORDER:
        group = [r for r in rows if r.get("type") == type_]
        if not group:
            continue
        lines.append(f"{TYPE_ICON.get(type_, '•')} <b>{type_}</b>")
        for r in group:
            request = str(r.get("text", ""))
            if len(request) > 350:
                request = request[:349] + "…"
            author = str(r.get("who", ""))[:100]
            nid = str(r["id"])[:20]
            lines.append(f"#{esc(nid)} {esc(request)} <i>— {esc(author)}, {esc(short(r.get('created', '')))}</i>")
    return lines


def needs_pages(rows: list[dict], limit: int = 3900) -> list[str]:
    lines = needs_lines(rows)
    pages, current = [], ""
    for line in lines:
        candidate = f"{current}\n{line}" if current else line
        if current and _utf16_len(candidate) > limit:
            pages.append(current)
            current = line
        else:
            current = candidate
    if current:
        pages.append(current)
    pages[-1] += "\n\nЗакрыть: /done &lt;номер&gt;"
    return pages


def needs_summary(rows: list[dict], limit: int = 3900) -> str:
    lines = needs_lines(rows)
    if len(lines) == 1:
        return "✅ Открытых заявок нет"
    out = []
    overflow = f"\n… ещё {len(rows)} заявок — /needs"
    for line in lines:
        candidate = "\n".join([*out, line])
        if _utf16_len(candidate + overflow) > limit:
            break
        out.append(line)
    shown_requests = sum(line.startswith("#") for line in out)
    remaining = max(0, len(rows) - shown_requests)
    if remaining:
        out.append(f"… ещё {remaining} заявок — /needs")
    return "\n".join(out)


def _summary_key() -> str:
    # Reuse the key written by the original bot so upgrades edit the same message.
    return f"needs:{GROUP_ID}:{TOPIC_NEEDS}"


def _is_not_modified(exc: Exception) -> bool:
    return isinstance(exc, TelegramBadRequest) and "message is not modified" in str(exc).lower()


def _is_missing_message(exc: Exception) -> bool:
    msg = str(exc).lower()
    return isinstance(exc, TelegramBadRequest) and (
        "message to edit not found" in msg or "message can't be edited" in msg
    )


async def refresh_needs_summary() -> None:
    if not GROUP_ID or bot is None:
        log.info("needs summary refresh skipped: group or bot is not configured")
        return
    rows = [r for r in await run(sheets.get().needs) if not str(r.get("closed_at", "")).strip()]
    text = needs_summary(rows)
    sid = await run(sheets.get().summary_id, _summary_key())
    if sid:
        try:
            await bot.edit_message_text(text, chat_id=GROUP_ID, message_id=sid)
            return
        except Exception as exc:
            if _is_not_modified(exc):
                return
            if not _is_missing_message(exc):
                log.exception("refresh needs summary failed")
                raise
            log.info("needs summary message %s disappeared; recreating", sid)
    msg = await post(TOPIC_NEEDS, text)
    if msg:
        await run(sheets.get().set_summary_id, _summary_key(), msg.message_id)


async def add_need(type_: str, text: str, who: str, source: str = "bot") -> int:
    async with _needs_lock:
        nid = await run(sheets.get().need_add, type_, text, who)
        await audit(who, "need_add", "need", nid, "", f"{type_}: {text}", source)
        rec = {"id": nid, "type": type_, "text": text, "who": who, "created": stamp()}
        try:
            msg = await post(TOPIC_NEEDS, need_line(rec))
            if msg:
                await run(sheets.get().need_set_msg, nid, msg.message_id)
        except Exception:
            log.exception("need #%s saved but individual post failed", nid)
        try:
            await refresh_needs_summary()
        except Exception:
            # The durable request exists; do not report the add as failed to its author.
            log.exception("need #%s saved but summary refresh failed", nid)
        return nid


async def need_close(nid: int, who: str, *, strict_summary: bool = False, source: str = "bot") -> dict | None:
    async with _needs_lock:
        rec = await run(sheets.get().need_close, nid, who)
        if rec and not rec["already"]:
            await audit(who, "need_close", "need", nid, rec.get("text", ""), "", source)
        if rec and not rec["already"] and rec.get("msg_id"):
            try:
                await bot.edit_message_text(need_line(rec, closed=True), chat_id=GROUP_ID,
                                            message_id=int(rec["msg_id"]))
            except Exception as exc:
                log.warning("edit need post #%s: %s", nid, exc)
                if _is_missing_message(exc):
                    try:
                        await post(TOPIC_NEEDS, need_line(rec, closed=True))
                    except Exception:
                        log.exception("closed need #%s saved but replacement post failed", nid)
        if rec:
            # Also refresh for already-closed rows: a prior refresh may have failed.
            try:
                await refresh_needs_summary()
            except Exception:
                if strict_summary:
                    raise
                log.exception("need #%s closure saved but summary refresh failed", nid)
        return rec


async def close_need(nid: int, who: str) -> str:
    """Close a need and refresh its summary; return its id if it exists."""
    rec = await need_close(nid, who, strict_summary=True)
    return str(nid) if rec else ""


async def needs_open() -> list[dict]:
    """Открытые заявки для приложения: по типам в порядке TYPE_ORDER, внутри — новые сверху."""
    rows = [r for r in await run(sheets.get().needs) if not str(r.get("closed_at", "")).strip()]
    order = {t: i for i, t in enumerate(TYPE_ORDER)}
    rows.sort(key=lambda r: (order.get(r.get("type"), 99), -int(r["id"]) if str(r["id"]).isdigit() else 0))
    return [{"id": int(r["id"]), "type": r.get("type", ""), "text": r.get("text", ""), "who": r.get("who", ""),
             "created": short(r.get("created", ""))} for r in rows]


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
        "notification_status": r.get("notification_status", ""),
        "need_status": r.get("need_status", ""),
        "deleted": bool(str(r.get("deleted", "")).strip()),
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
                         need_id: str = "", *, request_key: str) -> tuple[dict, bool]:
    """Сохраняет покупку один раз; уведомление и закрытие заявки выполняет очередь."""
    cat = category or exp_category(text)
    payload = json.dumps([amount, text, category, need_id], ensure_ascii=False, separators=(",", ":"))
    fingerprint = hashlib.sha256(payload.encode()).hexdigest()
    rec, created = await run(sheets.get().expense_add_once, who, amount, text, cat, need_id,
                             request_key=request_key, request_hash=fingerprint)
    if created:
        await audit(who, "expense_add", "expense", rec["id"], "", f"{text} — {fmt_money(amount)} ({cat})", "app")
    _outbox_wakeup.set()
    return _item(dict(rec, _amt=float(rec["amount"]))), created


async def flush_expense_outbox() -> None:
    """Pending work lives in Sheets and survives restarts. Never re-create the expense."""
    async with _outbox_lock:
        if bot is None or not GROUP_ID:
            return
        rows = await run(sheets.get().expenses)
        for rec in rows:
            if (rec.get("notification_status") != "pending" and rec.get("need_status") != "pending"):
                continue
            # Re-read: a user may have edited/deleted the expense since the scan.
            found = await run(sheets.get().expense_by_id, int(rec["id"]))
            if not found:
                continue
            row, rec = found
            if str(rec.get("deleted", "")).strip():
                await run(sheets.get().expense_set, row, notification_status="cancelled", need_status="cancelled")
                continue
            if rec.get("need_status") == "pending":
                try:
                    nid = await close_need(int(rec["need_id"]), rec["who"])
                    await run(sheets.get().expense_set, row, need_status="done" if nid else "not_found")
                except Exception:
                    log.exception("expense #%s: need closure deferred", rec["id"])
            if rec.get("notification_status") == "pending":
                try:
                    if rec.get("msg_id"):
                        await run(sheets.get().expense_set, row, notification_status="sent")
                        continue
                    msg = await post(TOPIC_SPENT,
                                     f"{CAT_ICON.get(rec['category'], '•')} {esc(rec['text'])} — "
                                     f"<b>{fmt_money(float(rec['amount']))}</b>\n"
                                     f"👤 {esc(rec['who'])} · {esc(rec['date'][:5])} · расход #{rec['id']}")
                    if msg:
                        await run(sheets.get().expense_set, row, msg_id=msg.message_id, notification_status="sent")
                except Exception:
                    log.exception("expense #%s: Telegram notification deferred", rec["id"])


async def expense_outbox_worker() -> None:
    while True:
        _outbox_wakeup.clear()
        try:
            await flush_expense_outbox()
        except Exception:
            log.exception("expense outbox unavailable; retrying later")
        try:
            await asyncio.wait_for(_outbox_wakeup.wait(), timeout=30)
        except asyncio.TimeoutError:
            pass


def _exp_desc(rec: dict) -> str:
    try:
        amt = fmt_money(float(str(rec.get("amount", "")).replace(",", ".")))
    except ValueError:
        amt = str(rec.get("amount", ""))
    cat = rec.get("category", "")
    return f"{rec.get('text', '')} — {amt}" + (f" ({cat})" if cat else "")


async def _notify(text: str) -> None:
    """Уведомление в «Расходы» — best effort: запись уже сохранена, сбой Telegram её не отменяет."""
    try:
        await post(TOPIC_SPENT, text)
    except Exception:
        log.exception("notification deferred to audit log only: %s", text[:80])


def deleted_between(rows: list[dict], a: datetime, b: datetime) -> list[dict]:
    """Удалённые записи, чья отметка «кто · когда» попадает в интервал."""
    out = []
    for r in rows:
        mark = str(r.get("deleted", "")).strip()
        if not mark:
            continue
        dt = parse_stamp(mark.split(" · ")[-1])
        if dt and a <= dt < b:
            out.append(r)
    return out


def deleted_by(rec: dict) -> str:
    mark = str(rec.get("deleted", "")).strip()
    return mark.split(" · ")[0] if " · " in mark else ""


async def expense_amend(row_idx: int, rec: dict, who: str, source: str = "bot", notify: bool = False, **fields) -> None:
    """Правка существующей записи (по строке) + журнал + при необходимости пост в «Расходы»."""
    if "text" in fields and "category" not in fields:
        fields["category"] = exp_category(fields["text"])
    await run(sheets.get().expense_set, row_idx, **fields)
    after = dict(rec, **fields)
    await audit(who, "expense_edit", "expense", rec["id"], _exp_desc(rec), _exp_desc(after), source)
    if notify:
        await _notify(f"✏️ {esc(who)} изменил расход #{rec['id']}: {esc(_exp_desc(rec))} → {esc(_exp_desc(after))}")


async def expense_mark_deleted(row_idx: int, rec: dict, who: str, source: str = "bot", notify: bool = False) -> bool:
    """Пометить удалённой: в колонке deleted — кто и когда; журнал; при удалении из приложения — пост в топик.
    Уже удалённая запись не трогается — первый автор остаётся. Возвращает False, если уже была удалена."""
    if str(rec.get("deleted", "")).strip():
        return False
    await run(sheets.get().expense_set, row_idx, deleted=f"{who} · {stamp()}")
    await audit(who, "expense_delete", "expense", rec["id"], _exp_desc(rec), "", source)
    if notify:
        await _notify(f"🗑 {esc(who)} удалил расход #{rec['id']} — {esc(_exp_desc(rec))}")
    return True


async def expense_update(eid: int, who: str = "", source: str = "bot", **fields) -> bool:
    found = await run(sheets.get().expense_by_id, eid)
    if not found:
        return False
    await expense_amend(found[0], found[1], who or "команда", source, notify=source == "app", **fields)
    return True


async def expense_delete(eid: int, who: str = "", source: str = "bot") -> bool:
    found = await run(sheets.get().expense_by_id, eid)
    if not found:
        return False
    await expense_mark_deleted(found[0], found[1], who or "команда", source, notify=source == "app")
    return True
