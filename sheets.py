"""Единственная точка доступа к Google Sheets. Чтение спеков кэшируется."""
import asyncio
import logging
from datetime import datetime, timedelta
from threading import RLock

import gspread

from config import CREDS, SHEET_ID, TZ
from utils import norm, now, stamp

log = logging.getLogger("barbot.sheets")

class Sheets:
    def __init__(self):
        self._expense_lock = RLock()
        gc = gspread.service_account_from_dict(CREDS)
        self.book = gc.open_by_key(SHEET_ID)
        self._ensure_sheets()
        self._specs: list[dict] = []
        self._specs_at = datetime.min.replace(tzinfo=TZ)

    EXP_COLS = ["id", "date", "who", "amount", "text", "category", "need_id", "photo", "msg_id", "deleted",
                "request_key", "request_hash", "notification_status", "need_status"]

    HEADERS = {
        "specs": ["name", "aliases", "ingredients", "method", "glass", "garnish", "pitch", "tags"],
        "stoplist": ["item", "since", "who", "note"],
        "needs": ["id", "type", "text", "who", "created", "closed_by", "closed_at", "msg_id"],
        "expenses": EXP_COLS,
        "state": ["key", "value"],
        "audit": ["at", "who", "action", "entity", "entity_id", "before", "after", "source"],
    }

    def _ensure_sheets(self):
        """Создаёт листы; добавляет служебные колонки расходов без изменения старых строк."""
        existing = {w.title for w in self.book.worksheets()}
        for name, hdr in self.HEADERS.items():
            if name not in existing:
                w = self.book.add_worksheet(title=name, rows=200, cols=len(hdr))
                w.append_row(hdr)
                log.info("created sheet %s", name)
        ws = self.ws("expenses")
        headers = ws.row_values(1)
        self._validate_expense_headers(headers, require_metadata=False)
        missing = [name for name in self.EXP_COLS[10:] if name not in headers]
        if missing:
            width = len(headers) + len(missing)
            if ws.col_count < width:
                ws.resize(cols=width)
            start = gspread.utils.rowcol_to_a1(1, len(headers) + 1)
            end = gspread.utils.rowcol_to_a1(1, width)
            ws.update(values=[missing], range_name=f"{start}:{end}")

    def _validate_expense_headers(self, headers, require_metadata=True):
        if headers[:10] != self.EXP_COLS[:10]:
            raise ValueError("expenses: первые 10 заголовков должны быть " + ", ".join(self.EXP_COLS[:10])
                             + "; получено: " + repr(headers[:10]))
        for name in self.EXP_COLS[10:]:
            if headers.count(name) > 1 or (require_metadata and name not in headers):
                raise ValueError(f"expenses: нужна ровно одна служебная колонка {name!r}")

    def _expense_headers(self, ws):
        headers = ws.row_values(1)
        self._validate_expense_headers(headers)
        return headers

    def _expense_record(self, headers, row):
        return {name: row[i] if i < len(row) else ""
                for i, name in enumerate(headers) if name in self.EXP_COLS}

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

    def audit_log(self, who: str, action: str, entity: str, entity_id, before: str = "", after: str = "",
                  source: str = "bot") -> None:
        """Журнал действий: кто, что, с чем сделал. Пишется отдельной строкой, ничего не меняет."""
        self.ws("audit").append_row([stamp(), who, action, entity, str(entity_id), str(before)[:500], str(after)[:500], source],
                                    value_input_option="RAW")

    def audit_tail(self, limit: int = 50) -> list[dict]:
        rows = self.ws("audit").get_all_records()
        return list(reversed(rows[-limit:]))

    def summary_id(self, key: str) -> int | None:
        ws = self.ws("state")
        for rec in ws.get_all_records():
            if str(rec.get("key", "")) == key and str(rec.get("value", "")).isdigit():
                return int(rec["value"])
        return None

    def set_summary_id(self, key: str, msg_id: int) -> None:
        ws = self.ws("state")
        for row, rec in enumerate(ws.get_all_records(), start=2):
            if str(rec.get("key", "")) == key:
                ws.update_cell(row, 2, msg_id)
                return
        ws.append_row([key, msg_id])


    # --- expenses
    def expenses(self) -> list[dict]:
        values = self.ws("expenses").get_all_values()
        headers = values[0] if values else []
        self._validate_expense_headers(headers)
        rows = [self._expense_record(headers, row) for row in values[1:]]
        return [r for r in rows if str(r["id"]).strip()]

    def expense_add(self, who, amount, text, category, need_id="", photo="", msg_id="", dedup_msg=True) -> int:
        rec, _ = self.expense_add_once(who, amount, text, category, need_id, photo, msg_id, dedup_msg=dedup_msg)
        return int(rec["id"])

    def expense_add_once(self, who, amount, text, category, need_id="", photo="", msg_id="",
                         request_key="", request_hash="", dedup_msg=True) -> tuple[dict, bool]:
        """dedup_msg=False — несколько строк от одного сообщения (многострочный чек, пересборка после правки)."""
        # All writers (bot and API) share this lock. Deploy exactly one worker.
        # Keep the key and pending work in the SAME append as the expense itself.
        with self._expense_lock:
            rows = self.expenses()
            for rec in rows:
                if request_key and rec.get("request_key") == request_key:
                    if rec.get("request_hash") != request_hash:
                        raise ExpenseConflict("этот запрос уже использован для другой покупки")
                    return rec, False
                if dedup_msg and msg_id and str(rec.get("msg_id", "")) == str(msg_id):
                    return rec, False
            ids = [int(r["id"]) for r in rows if str(r["id"]).isdigit()]
            values = [max(ids, default=0) + 1, stamp(), who, amount if amount is not None else "", text,
                      category, need_id, photo, msg_id, "", request_key, request_hash,
                      "pending" if request_key else "", "pending" if request_key and need_id else ""]
            rec = dict(zip(self.EXP_COLS, values))
            ws = self.ws("expenses")
            headers = self._expense_headers(ws)
            ws.append_row([rec.get(name, "") for name in headers], value_input_option="RAW")
            return rec, True

    def expense_by_msg(self, msg_id: int) -> tuple[int, dict] | None:
        return self._expense_find("msg_id", msg_id)

    def expense_by_id(self, eid: int) -> tuple[int, dict] | None:
        return self._expense_find("id", eid)

    def expenses_by_msg(self, msg_id: int) -> list[tuple[int, dict]]:
        """Все строки, созданные из одного сообщения (многострочный чек даёт несколько записей)."""
        ws = self.ws("expenses")
        headers = self._expense_headers(ws)
        out = []
        for i, row in enumerate(ws.get_all_values()[1:], start=2):
            rec = self._expense_record(headers, row)
            if str(rec.get("msg_id", "")).strip() == str(msg_id):
                out.append((i, rec))
        return out

    def _expense_find(self, name, value) -> tuple[int, dict] | None:
        ws = self.ws("expenses")
        headers = self._expense_headers(ws)
        cell = ws.find(str(value), in_column=headers.index(name) + 1)
        if not cell:
            return None
        row = ws.row_values(cell.row)
        return cell.row, self._expense_record(headers, row)

    def expense_set(self, row_idx: int, **fields):
        ws = self.ws("expenses")
        headers = self._expense_headers(ws)
        # One request: a sent message ID must not be separated from its sent flag.
        ws.batch_update([{"range": gspread.utils.rowcol_to_a1(row_idx, headers.index(k) + 1),
                          "values": [[v]]} for k, v in fields.items()], value_input_option="RAW")


class ExpenseConflict(ValueError):
    """An idempotency key was reused with a different request body."""


async def run(fn, *args, **kwargs):
    """gspread синхронный — уводим в тред, чтобы не блокировать бота."""
    return await asyncio.to_thread(fn, *args, **kwargs)


_sh: Sheets | None = None
_init_lock = RLock()


def get() -> Sheets:
    """Ленивый синглтон — таблица открывается при первом обращении."""
    global _sh
    with _init_lock:
        if _sh is None:
            _sh = Sheets()
        return _sh
