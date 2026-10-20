"""Multi-line receipts in the «Расходы» topic: one row per priced line, «итого» skipped,
❌ removes all rows of the message, editing rebuilds them.
Run: python -m unittest discover -s tests -p test_multiline_expenses.py -v
"""
import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.update(BOT_TOKEN="123456:offline-test", SHEET_ID="offline", GOOGLE_CREDS_JSON="{}", GROUP_ID="-1001",
                  TOPIC_SPENT="2")

import services as svc
import sheets
from bots import team
from classify import split_expense_lines
from test_expense_saving import Worksheet, store

RECEIPT = "Вишня коктейльная 15 манат\nкрючки винтажные - 40 манат\nЛикер амаретто - 20 манат\n\nиз грузии итого  : 75 манат"


class SplitTests(unittest.TestCase):
    def test_lines_with_amounts_become_items_and_total_is_skipped(self):
        items = split_expense_lines(RECEIPT)
        self.assertEqual([a for _, a in items], [15.0, 40.0, 20.0])
        self.assertEqual(items[1][0], "крючки винтажные - 40 манат")

    def test_single_line_is_not_split(self):
        self.assertEqual(split_expense_lines("лаймы 12.50"), [])
        self.assertEqual(split_expense_lines("лаймы 12.50\nитого 12.50"), [])
        self.assertEqual(split_expense_lines("лаймы\nмята"), [])


def message(text, msg_id=500, edited=False, reply_to=None):
    user = SimpleNamespace(id=9, first_name="Qismet")
    return SimpleNamespace(text=text, caption=None, photo=None, document=None, media_group_id=None,
                           message_id=msg_id, message_thread_id=2, is_topic_message=True,
                           chat=SimpleNamespace(id=-1001), from_user=user, reply_to_message=reply_to,
                           reply=AsyncMock(), edit_date=1 if edited else None)


class TopicHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ws = Worksheet()
        self.store = store(self.ws)
        self.store.expenses_by_msg = lambda msg_id: sheets.Sheets.expenses_by_msg(self.store, msg_id)
        self.store._expense_headers = lambda ws: list(ws.rows[0])
        self.bot = SimpleNamespace(id=1, set_message_reaction=AsyncMock(), send_message=AsyncMock())
        for target, value in [("sheets._sh", self.store), ("services.bot", self.bot), ("services.GROUP_ID", -1001),
                              ("services._expense_mutate_lock", asyncio.Lock())]:
            p = patch(target, value)
            p.start()
            self.addCleanup(p.stop)

    def rows(self):
        return [dict(zip(self.ws.rows[0], r + [""] * (len(self.ws.rows[0]) - len(r)))) for r in self.ws.rows[1:]]

    async def test_receipt_creates_one_row_per_line(self):
        await team.spent_new(message(RECEIPT))
        rows = self.rows()
        self.assertEqual([(r["text"], r["amount"], r["category"]) for r in rows],
                         [("Вишня коктейльная 15 манат", 15.0, "бар"),
                          ("крючки винтажные - 40 манат", 40.0, "оборудование"),
                          ("Ликер амаретто - 20 манат", 20.0, "бар")])
        self.assertTrue(all(str(r["msg_id"]) == "500" for r in rows))
        self.bot.set_message_reaction.assert_awaited_once()

    async def test_dismiss_reply_removes_all_rows_of_the_message(self):
        await team.spent_new(message(RECEIPT))
        original = message(RECEIPT)
        await team.spent_reply(message("❌", msg_id=501, reply_to=original))
        self.assertTrue(all(str(r["deleted"]).startswith("Qismet · ") for r in self.rows()))

    async def test_amount_reply_to_multi_message_asks_to_edit_instead(self):
        await team.spent_new(message(RECEIPT))
        reply = message("90", msg_id=502, reply_to=message(RECEIPT))
        await team.spent_reply(reply)
        reply.reply.assert_awaited_once()
        self.assertEqual([r["amount"] for r in self.rows()], [15.0, 40.0, 20.0])

    async def test_edit_rebuilds_rows(self):
        await team.spent_new(message(RECEIPT))
        edited = "Вишня коктейльная 15 манат\nЛикер амаретто - 25 манат\nитого 40"
        await team.spent_edited(message(edited, edited=True))
        live = [r for r in self.rows() if not str(r["deleted"]).strip()]
        self.assertEqual([(r["text"], r["amount"]) for r in live],
                         [("Вишня коктейльная 15 манат", 15.0), ("Ликер амаретто - 25 манат", 25.0)])
        self.assertEqual(len([r for r in self.rows() if str(r["deleted"]).strip()]), 3)

    async def test_single_line_still_single_row(self):
        await team.spent_new(message("лаймы 12.50", msg_id=600))
        self.assertEqual([(r["text"], r["amount"]) for r in self.rows()], [("лаймы 12.50", 12.5)])


if __name__ == "__main__":
    unittest.main()
