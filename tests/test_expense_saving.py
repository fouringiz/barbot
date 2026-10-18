"""Offline regression tests: python -m unittest discover -s tests -p test_expense_saving.py -v."""
import asyncio
import json
import os
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import RLock
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

# Import real application modules with dummy configuration, never real credentials.
os.environ.update(BOT_TOKEN="123456:offline-test", SHEET_ID="offline", GOOGLE_CREDS_JSON="{}", GROUP_ID="-1001")

from aiohttp.test_utils import make_mocked_request
from gspread.utils import a1_to_rowcol

import services as svc
import sheets
from api import server


class Worksheet:
    def __init__(self, headers=None):
        self.rows = [list(headers or sheets.Sheets.EXP_COLS)]
        self.col_count = len(self.rows[0])
        self.fail_before_append = False
        self.fail_after_append = False

    def get_all_records(self, **kwargs):
        time.sleep(0.002)  # Yield to other writer threads while the read is in flight.
        return [dict(zip(self.rows[0], list(r) + [""] * (len(self.rows[0]) - len(r)))) for r in self.rows[1:]]

    def append_row(self, values, **kwargs):
        if self.fail_before_append:
            raise OSError("Sheets unavailable")
        self.rows.append(list(values))
        if self.fail_after_append:
            self.fail_after_append = False
            raise OSError("append accepted but response lost")

    def find(self, value, in_column):
        for i, row in enumerate(self.rows, 1):
            if len(row) >= in_column and str(row[in_column - 1]) == value:
                return SimpleNamespace(row=i)
        return None

    def row_values(self, row):
        return list(self.rows[row - 1])

    def resize(self, cols):
        self.col_count = cols

    def update(self, values, range_name):
        row, col = a1_to_rowcol(range_name.split(":")[0])
        data = self.rows[row - 1]
        data.extend([""] * max(0, col - 1 + len(values[0]) - len(data)))
        data[col - 1:col - 1 + len(values[0])] = values[0]

    def batch_update(self, updates, **kwargs):
        for update in updates:
            self.update(update["values"], update["range"])


def store(ws=None):
    obj = object.__new__(sheets.Sheets)
    obj._expense_lock = RLock()
    obj.ws = lambda name: ws
    return obj


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.ws = Worksheet()
        self.sh = store(self.ws)

    def test_concurrent_bot_and_app_writers_get_unique_ids(self):
        def write(i):
            if i % 2:
                return self.sh.expense_add("team", i + 1, "lime", "бар", msg_id=i)
            rec, _ = self.sh.expense_add_once("team", i + 1, "lime", "бар", request_key=f"app:{i}", request_hash=str(i))
            return rec["id"]
        with ThreadPoolExecutor(8) as pool:
            ids = list(pool.map(write, range(24)))
        self.assertEqual(len(set(ids)), 24)
        self.assertEqual(len(self.ws.rows), 25)

    def test_concurrent_replays_append_once(self):
        def write(_):
            return self.sh.expense_add_once("team", 12.5, "lime", "бар", request_key="same", request_hash="payload")
        with ThreadPoolExecutor(8) as pool:
            results = list(pool.map(write, range(12)))
        self.assertEqual(sum(created for _, created in results), 1)
        self.assertEqual({r["id"] for r, _ in results}, {1})

    def test_lost_append_response_survives_restart(self):
        self.ws.fail_after_append = True
        args = ("team", 12.5, "lime", "бар")
        with self.assertRaises(OSError):
            self.sh.expense_add_once(*args, request_key="same", request_hash="payload")
        rec, created = store(self.ws).expense_add_once(*args, request_key="same", request_hash="payload")
        self.assertFalse(created)
        self.assertEqual(rec["id"], 1)
        self.assertEqual(rec["notification_status"], "pending")
        self.assertEqual(len(self.ws.rows), 2)

    def test_key_cannot_change_purchase(self):
        self.sh.expense_add_once("team", 1, "lime", "бар", request_key="same", request_hash="one")
        with self.assertRaises(sheets.ExpenseConflict):
            self.sh.expense_add_once("team", 2, "mint", "бар", request_key="same", request_hash="two")
        self.assertEqual(len(self.ws.rows), 2)

    def test_telegram_message_replay(self):
        args = ("team", 12.5, "lime", "бар", "", "", 44)
        self.assertEqual(self.sh.expense_add(*args), self.sh.expense_add(*args))
        self.assertEqual(len(self.ws.rows), 2)

    def test_migration_preserves_existing_data_and_is_repeatable(self):
        old = sheets.Sheets.EXP_COLS[:10]
        self.ws = Worksheet(old)
        original = [7, "18.10.2026 20:00", "team", 12.5, "lime", "бар", "", "", 44, ""]
        self.ws.rows.append(original[:])
        sh = store(self.ws)
        sh.book = SimpleNamespace(worksheets=lambda: [SimpleNamespace(title=n) for n in sh.HEADERS])
        sh._ensure_sheets()
        sh._ensure_sheets()
        self.assertEqual(self.ws.rows[0], sheets.Sheets.EXP_COLS)
        self.assertEqual(self.ws.rows[1], original)
        self.assertEqual(self.ws.col_count, len(sheets.Sheets.EXP_COLS))

    def test_migration_refuses_to_overwrite_custom_columns(self):
        ws = Worksheet(sheets.Sheets.EXP_COLS[:10] + ["my notes"])
        sh = store(ws)
        sh.book = SimpleNamespace(worksheets=lambda: [SimpleNamespace(title=n) for n in sh.HEADERS])
        with self.assertRaises(ValueError):
            sh._ensure_sheets()
        self.assertEqual(ws.rows[0][-1], "my notes")


class ServiceAndApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ws = Worksheet()
        self.sh = store(self.ws)
        self.bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=55)))
        for target, value in [("sheets._sh", self.sh), ("services.bot", self.bot), ("services.GROUP_ID", -1001),
                              ("services._outbox_lock", asyncio.Lock()), ("services._outbox_wakeup", asyncio.Event())]:
            p = patch(target, value)
            p.start()
            self.addCleanup(p.stop)

    async def create(self, **kwargs):
        return await svc.expense_create("team", 12.5, "lime", **dict({"request_key": "app:7:same"}, **kwargs))

    async def request(self, body, key=None, user_id=7):
        headers = {"Content-Type": "application/json"}
        if key is not None:
            headers["Idempotency-Key"] = key
        req = make_mocked_request("POST", "/api/expenses", headers=headers)
        req._read_bytes = json.dumps(body).encode()
        req["user"] = {"id": user_id, "first_name": "team"}
        return await server.expenses_post(req)

    async def test_telegram_failure_does_not_fail_or_duplicate_purchase(self):
        self.bot.send_message.side_effect = OSError("Telegram offline")
        item, created = await self.create()
        self.assertTrue(created)
        self.bot.send_message.assert_not_awaited()
        with self.assertLogs("barbot.services", level="ERROR"):
            await svc.flush_expense_outbox()
        replay, created = await self.create()
        self.assertFalse(created)
        self.assertEqual(item["id"], replay["id"])
        self.assertEqual(replay["notification_status"], "pending")
        self.assertEqual(len(self.ws.rows), 2)

    async def test_outbox_resumes_after_restart_and_does_not_resend_completed_work(self):
        await self.create(need_id="14")
        with patch("sheets._sh", store(self.ws)), patch("services.close_need", AsyncMock(return_value="14")) as close:
            await svc.flush_expense_outbox()
            await svc.flush_expense_outbox()
            close.assert_awaited_once_with(14, "team")
        self.bot.send_message.assert_awaited_once()
        self.assertEqual(self.sh.expenses()[0]["notification_status"], "sent")
        self.assertEqual(self.sh.expenses()[0]["need_status"], "done")

    async def test_failed_save_never_closes_need_or_posts(self):
        self.ws.fail_before_append = True
        with patch("services.close_need", AsyncMock()) as close:
            with self.assertRaises(OSError):
                await self.create(need_id="14")
            await svc.flush_expense_outbox()
            close.assert_not_awaited()
        self.bot.send_message.assert_not_awaited()

    async def test_failed_need_closure_is_retried_without_reposting(self):
        await self.create(need_id="14")
        with patch("services.close_need", AsyncMock(side_effect=[OSError("Sheets offline"), "14"])) as close:
            with self.assertLogs("barbot.services", level="ERROR"):
                await svc.flush_expense_outbox()
            await svc.flush_expense_outbox()
            self.assertEqual(close.await_count, 2)
        self.bot.send_message.assert_awaited_once()

    async def test_deleted_expense_cancels_pending_work_and_cannot_be_recreated(self):
        item, _ = await self.create(need_id="14")
        await svc.expense_delete(item["id"])
        with patch("services.close_need", AsyncMock()) as close:
            await svc.flush_expense_outbox()
            close.assert_not_awaited()
        self.bot.send_message.assert_not_awaited()
        replay, created = await self.create(need_id="14")
        self.assertFalse(created)
        self.assertTrue(replay["deleted"])
        self.assertEqual(len(self.ws.rows), 2)

    async def test_concurrent_flushes_do_not_duplicate_notification(self):
        await self.create()
        await asyncio.gather(svc.flush_expense_outbox(), svc.flush_expense_outbox())
        self.bot.send_message.assert_awaited_once()

    async def test_post_contract_replay_and_user_scope(self):
        key = str(uuid4())
        body = {"amount": "12,50", "text": "lime"}
        first = await self.request(body, key)
        second = await self.request(body, key)
        other = await self.request(body, key, user_id=8)
        self.assertEqual([first.status, second.status, other.status], [201, 200, 201])
        self.assertEqual(json.loads(first.text)["id"], json.loads(second.text)["id"])
        self.assertNotEqual(json.loads(first.text)["id"], json.loads(other.text)["id"])

    async def test_conflicting_payload_returns_409(self):
        key = str(uuid4())
        await self.request({"amount": 12, "text": "lime"}, key)
        response = await self.request({"amount": 13, "text": "lime"}, key)
        self.assertEqual(response.status, 409)
        self.assertEqual(len(self.ws.rows), 2)

    async def test_invalid_inputs_do_not_write(self):
        base = {"amount": 12, "text": "lime"}
        cases = [[], None, dict(base, amount="NaN"), dict(base, amount="Infinity"), dict(base, amount=-1),
                 dict(base, amount=0), dict(base, need_id="oops"), dict(base, need_id="0"),
                 dict(base, category=["bar"]), dict(base, text=None), dict(base, text="x" * 1001)]
        for body in cases:
            with self.subTest(body=body):
                self.assertEqual((await self.request(body, str(uuid4()))).status, 400)
        self.assertEqual((await self.request(base)).status, 400)
        self.assertEqual((await self.request(base, "not-a-uuid")).status, 400)
        self.assertEqual(len(self.ws.rows), 1)

    async def test_cors_allows_idempotency_header(self):
        req = make_mocked_request("OPTIONS", "/api/expenses")
        response = await server.cors(req, AsyncMock())
        self.assertIn("Idempotency-Key", response.headers["Access-Control-Allow-Headers"])

    async def test_patch_cannot_corrupt_pending_expense(self):
        item, _ = await self.create()
        for body in [{"amount": "NaN"}, {"amount": -2}, {"text": ""}, {"category": ["bar"]}, []]:
            req = make_mocked_request("PATCH", "/api/expenses/1", match_info={"id": str(item["id"])})
            req._read_bytes = json.dumps(body).encode()
            self.assertEqual((await server.expenses_patch(req)).status, 400)
        self.assertEqual(self.sh.expenses()[0]["amount"], 12.5)


if __name__ == "__main__":
    unittest.main()
