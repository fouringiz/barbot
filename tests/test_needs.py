"""Offline need-summary regression tests."""
import os
import unittest
from unittest.mock import AsyncMock

os.environ.update(BOT_TOKEN="123456:offline-test", SHEET_ID="offline", GOOGLE_CREDS_JSON="{}", GROUP_ID="-1001")

import services as svc
from aiogram.exceptions import TelegramBadRequest


class FakeSheets:
    def __init__(self, rows=None, summary_id=42):
        self.rows = list(rows or [])
        self.saved_id = summary_id

    def needs(self):
        return self.rows

    def summary_id(self, key):
        assert key == "needs:-1001:None"
        return self.saved_id

    def set_summary_id(self, key, msg_id):
        assert key == "needs:-1001:None"
        self.saved_id = msg_id

    def need_add(self, type_, text, who):
        nid = max([int(r["id"]) for r in self.rows] or [0]) + 1
        self.rows.append(dict(id=nid, type=type_, text=text, who=who, created="date", closed_at="", msg_id=""))
        return nid

    def audit_log(self, *args, **kwargs):
        pass

    def need_set_msg(self, nid, msg_id):
        next(r for r in self.rows if int(r["id"]) == nid)["msg_id"] = msg_id

    def need_close(self, nid, who):
        rec = next((r for r in self.rows if int(r["id"]) == nid), None)
        if rec is None:
            return None
        if rec.get("closed_at"):
            return dict(rec, already=True)
        rec.update(closed_by=who, closed_at="closed")
        return dict(rec, already=False)


class NeedSummaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.rows = [dict(id=i, type="бар", text="<номер> & тест", who="Имя", created="date", closed_at="")
                     for i in range(150)]
        self.sh = FakeSheets(self.rows)
        self.old_bot, self.old_post, self.old_get, self.old_run = svc.bot, svc.post, svc.sheets.get, svc.run
        self.old_group, self.old_topic = svc.GROUP_ID, svc.TOPIC_NEEDS
        svc.GROUP_ID, svc.TOPIC_NEEDS = -1001, None
        svc.bot = AsyncMock()
        svc.post = AsyncMock(return_value=type("Msg", (), {"message_id": 99})())
        svc.sheets.get = lambda: self.sh

        async def run(fn, *args):
            return fn(*args)
        svc.run = run

    def tearDown(self):
        svc.bot, svc.post, svc.sheets.get, svc.run = self.old_bot, self.old_post, self.old_get, self.old_run
        svc.GROUP_ID, svc.TOPIC_NEEDS = self.old_group, self.old_topic

    async def test_formatting_and_pagination(self):
        pages = svc.needs_pages(self.rows)
        self.assertGreater(len(pages), 1)
        for page in pages:
            self.assertLessEqual(svc._utf16_len(page), 4096)
        self.assertIn("&lt;номер&gt; &amp; тест", pages[0])
        long_row = dict(self.rows[0], text="x" * 1000)
        self.assertLessEqual(len(svc.needs_lines([long_row])[-1]), 500)
        self.assertIn("ещё", svc.needs_summary(self.rows))
        self.sh.rows = [dict(self.rows[0], closed_at="closed")]
        svc.bot.edit_message_text = AsyncMock()
        await svc.refresh_needs_summary()
        self.assertEqual(svc.bot.edit_message_text.await_args.args[0], "✅ Открытых заявок нет")

    async def test_summary_edits_persisted_message_and_ignores_unchanged(self):
        await svc.refresh_needs_summary()
        svc.bot.edit_message_text.assert_awaited_once()
        svc.post.assert_not_awaited()
        svc.bot.edit_message_text = AsyncMock(side_effect=TelegramBadRequest(method="editMessageText",
                                                                              message="Message is not modified"))
        await svc.refresh_needs_summary()
        svc.post.assert_not_awaited()

    async def test_deleted_summary_is_recreated_and_id_saved(self):
        svc.bot.edit_message_text = AsyncMock(side_effect=TelegramBadRequest(method="editMessageText",
                                                                              message="Message to edit not found"))
        await svc.refresh_needs_summary()
        svc.post.assert_awaited_once()
        self.assertEqual(self.sh.saved_id, 99)

    async def test_other_telegram_errors_propagate_without_duplicate_post(self):
        svc.bot.edit_message_text = AsyncMock(side_effect=TelegramBadRequest(method="editMessageText",
                                                                              message="Chat not found"))
        with self.assertRaises(TelegramBadRequest):
            await svc.refresh_needs_summary()
        svc.post.assert_not_awaited()

    async def test_add_persists_request_and_refreshes_summary(self):
        nid = await svc.add_need("бар", "&" * 1000, "Аня")
        self.assertEqual(nid, 150)
        self.assertEqual(self.sh.rows[-1]["text"], "&" * 1000)
        svc.post.assert_awaited_once()  # individual need post remains
        svc.bot.edit_message_text.assert_awaited_once()

    async def test_close_refreshes_and_queue_retry_propagates_summary_failure(self):
        row = dict(self.rows[0], msg_id="77")
        self.sh.rows = [row]
        svc.bot.edit_message_text = AsyncMock(side_effect=[None, TelegramBadRequest(method="editMessageText",
                                                                                     message="Chat not found")])
        result = await svc.need_close(0, "Аня")
        self.assertFalse(result["already"])
        self.assertTrue(row["closed_at"])

        # A later queue retry sees the durable closed row and still retries the failed summary refresh.
        svc.bot.edit_message_text = AsyncMock(side_effect=TelegramBadRequest(method="editMessageText",
                                                                              message="Chat not found"))
        with self.assertRaises(TelegramBadRequest):
            await svc.close_need(0, "Аня")
        svc.bot.edit_message_text = AsyncMock()
        self.assertEqual(await svc.close_need(0, "Аня"), "0")

    async def test_cmd_needs_paginates_escaped_emoji_text(self):
        from bots import team

        self.sh.rows = [dict(id=i, type="бар", text="😀&" * 1000, who="Имя", created="date", closed_at="")
                        for i in range(24)]
        old_run = team.run

        async def run(fn, *args):
            return fn(*args)
        team.run = run
        try:
            message = type("Message", (), {"reply": AsyncMock()})()
            await team.cmd_needs(message)
        finally:
            team.run = old_run
        self.assertGreater(message.reply.await_count, 1)
        for call in message.reply.await_args_list:
            self.assertLessEqual(svc._utf16_len(call.args[0]), 4096)
        self.assertIn("&amp;", message.reply.await_args_list[0].args[0])
        self.assertIn("😀", message.reply.await_args_list[0].args[0])


if __name__ == "__main__":
    unittest.main()
