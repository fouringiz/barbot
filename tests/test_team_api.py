"""Offline tests for the team Mini App routes: /api/stop, /api/needs, /api/specs.
Run: python -m unittest discover -s tests -p test_team_api.py -v
"""
import asyncio
import json
import os
import unittest
from threading import RLock
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

os.environ.update(BOT_TOKEN="123456:offline-test", SHEET_ID="offline", GOOGLE_CREDS_JSON="{}", GROUP_ID="-1001")

from aiohttp.test_utils import make_mocked_request

import services as svc
import sheets
from api import server
from test_expense_saving import Worksheet, store

SPECS = [
    {"name": "Негрони", "aliases": "negroni", "ingredients": "джин 30; кампари 30; сладкий вермут 30",
     "method": "stir", "glass": "рокс", "garnish": "цедра", "pitch": "горький", "tags": "stir; классика"},
    {"name": "Дайкири", "aliases": "daiquiri", "ingredients": "белый ром 60; сок лайма 25; сахарный сироп 20",
     "method": "shake", "glass": "купе", "garnish": "", "pitch": "", "tags": "sour; меню"},
]


class FakeStore:
    """Fake Sheets with stop/needs plus the real expense worksheet from the saving tests."""

    def __init__(self):
        self.ws_exp = Worksheet()
        self.exp = store(self.ws_exp)
        self.stop = []
        self.needs_rows = [{"id": 16, "type": "бар", "text": "Campari 2 бутылки", "who": "Чингиз",
                            "created": "18.10.2026 12:00", "closed_by": "", "closed_at": "", "msg_id": "7"}]
        self.state = {}
        self.audit = []

    def audit_log(self, who, action, entity, entity_id, before="", after="", source="bot"):
        self.audit.append({"who": who, "action": action, "entity": entity, "entity_id": str(entity_id),
                           "before": before, "after": after, "source": source})

    def audit_tail(self, limit=50):
        return list(reversed(self.audit[-limit:]))

    # specs / stop
    def specs(self, force=False):
        return SPECS

    def stoplist(self):
        return [dict(r) for r in self.stop]

    def stop_add(self, item, who, note=""):
        self.stop.append({"item": item, "since": "18.10.2026 20:00", "who": who, "note": note})

    def stop_remove(self, item):
        before = len(self.stop)
        self.stop = [r for r in self.stop if r["item"].lower() != item.lower()]
        return len(self.stop) < before

    # needs
    def needs(self):
        return [dict(r) for r in self.needs_rows]

    def need_add(self, type_, text, who):
        nid = max(r["id"] for r in self.needs_rows) + 1
        self.needs_rows.append({"id": nid, "type": type_, "text": text, "who": who, "created": "18.10.2026 20:00",
                                "closed_by": "", "closed_at": "", "msg_id": ""})
        return nid

    def need_set_msg(self, nid, msg_id):
        pass

    def need_close(self, nid, who):
        for r in self.needs_rows:
            if r["id"] == nid:
                if r["closed_at"]:
                    return dict(r, already=True)
                r["closed_by"], r["closed_at"] = who, "18.10.2026 20:05"
                return dict(r, already=False)
        return None

    def summary_id(self, key):
        return self.state.get(key)

    def set_summary_id(self, key, msg_id):
        self.state[key] = msg_id

    # expenses -> real implementation on the fake worksheet
    def __getattr__(self, name):
        if name.startswith("expense"):
            return getattr(self.exp, name)
        raise AttributeError(name)


class TeamApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = FakeStore()
        self.bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=55)),
                                   edit_message_text=AsyncMock())
        for target, value in [("sheets._sh", self.store), ("services.bot", self.bot), ("services.GROUP_ID", -1001),
                              ("services._outbox_lock", asyncio.Lock()), ("services._outbox_wakeup", asyncio.Event()),
                              ("services._needs_lock", asyncio.Lock()), ("services._expense_mutate_lock", asyncio.Lock())]:
            p = patch(target, value)
            p.start()
            self.addCleanup(p.stop)

    async def call(self, handler, method, path, body=None, headers=None, match_info=None):
        req = make_mocked_request(method, path, headers={"Content-Type": "application/json", **(headers or {})},
                                  match_info=match_info or {})
        req._read_bytes = json.dumps(body).encode() if body is not None else b""
        req["user"] = {"id": 7, "first_name": "team"}
        resp = await handler(req)
        return resp.status, json.loads(resp.text)

    # --- stop
    async def test_stop_toggle_posts_once_and_reports_affected(self):
        status, data = await self.call(server.stop_post, "POST", "/api/stop", {"item": "кампари", "on": True})
        self.assertEqual((status, data["affected"]), (200, ["Негрони"]))
        status, data = await self.call(server.stop_post, "POST", "/api/stop", {"item": "кампари", "on": True})
        self.assertTrue(data["already"])
        self.assertEqual(self.bot.send_message.await_count, 1)          # второй раз пост не уходит
        status, data = await self.call(server.stop_get, "GET", "/api/stop")
        self.assertEqual([s["item"] for s in data["stop"]], ["кампари"])
        self.assertNotIn("кампари", data["ingredients"])
        self.assertIn("джин", data["ingredients"])
        status, data = await self.call(server.stop_post, "POST", "/api/stop", {"item": "кампари", "on": False})
        self.assertEqual((status, data["ok"]), (200, True))
        status, _ = await self.call(server.stop_post, "POST", "/api/stop", {"item": "кампари", "on": False})
        self.assertEqual(status, 404)

    async def test_stop_rejects_bad_input(self):
        for body in [[], {"item": ""}, {"item": "x" * 101}]:
            status, _ = await self.call(server.stop_post, "POST", "/api/stop", body)
            self.assertEqual(status, 400, body)
        self.assertEqual(self.store.stop, [])

    # --- specs
    async def test_specs_mark_dead_ingredients(self):
        self.store.stop_add("кампари", "team")
        status, data = await self.call(server.specs_get, "GET", "/api/specs")
        negroni = next(s for s in data["items"] if s["name"] == "Негрони")
        self.assertEqual(negroni["dead"], ["кампари"])
        self.assertEqual(negroni["ingredients"], ["джин 30", "кампари 30", "сладкий вермут 30"])
        self.assertEqual(negroni["aliases"], ["negroni"])

    # --- needs
    async def test_needs_create_and_list(self):
        status, data = await self.call(server.needs_post, "POST", "/api/needs", {"text": "нет горячей воды"})
        self.assertEqual((status, data["type"]), (201, "поломка"))
        status, data = await self.call(server.needs_get, "GET", "/api/needs")
        self.assertEqual([i["type"] for i in data["items"]], ["поломка", "бар"])   # TYPE_ORDER
        status, _ = await self.call(server.needs_post, "POST", "/api/needs", {"text": "x", "type": "нечто"})
        self.assertEqual(status, 400)

    async def test_close_plain_and_not_found(self):
        status, data = await self.call(server.needs_close, "POST", "/api/needs/16/close", {}, match_info={"id": "16"})
        self.assertEqual((status, data["ok"], data["already"]), (200, True, False))
        self.assertTrue(self.store.needs_rows[0]["closed_at"])
        status, data = await self.call(server.needs_close, "POST", "/api/needs/16/close", {}, match_info={"id": "16"})
        self.assertTrue(data["already"])
        status, _ = await self.call(server.needs_close, "POST", "/api/needs/99/close", {}, match_info={"id": "99"})
        self.assertEqual(status, 404)

    async def test_close_with_expense_is_idempotent_and_closes_via_queue(self):
        key = str(uuid4())
        body = {"amount": "64", "text": "Campari 2 бут"}
        first = await self.call(server.needs_close, "POST", "/api/needs/16/close", body,
                                headers={"Idempotency-Key": key}, match_info={"id": "16"})
        second = await self.call(server.needs_close, "POST", "/api/needs/16/close", body,
                                 headers={"Idempotency-Key": key}, match_info={"id": "16"})
        self.assertEqual((first[0], second[0]), (201, 200))
        self.assertEqual(first[1]["expense"]["id"], second[1]["expense"]["id"])
        self.assertEqual(len(self.store.ws_exp.rows), 2)                  # одна строка расхода
        self.assertFalse(self.store.needs_rows[0]["closed_at"])           # закрывает очередь, не роут
        await svc.flush_expense_outbox()
        self.assertTrue(self.store.needs_rows[0]["closed_at"])
        _, rec = self.store.exp.expense_by_id(first[1]["expense"]["id"])
        self.assertEqual((rec["need_status"], rec["notification_status"]), ("done", "sent"))

    # --- audit
    async def test_app_delete_and_edit_are_attributed_and_announced(self):
        key = str(uuid4())
        status, item = await self.call(server.expenses_post, "POST", "/api/expenses", {"amount": "12.5", "text": "лаймы"},
                                       headers={"Idempotency-Key": key})
        self.assertEqual(status, 201)
        status, _ = await self.call(server.expenses_patch, "PATCH", f"/api/expenses/{item['id']}", {"amount": "13"},
                                    match_info={"id": str(item["id"])})
        self.assertEqual(status, 200)
        status, _ = await self.call(server.expenses_delete, "DELETE", f"/api/expenses/{item['id']}",
                                    match_info={"id": str(item["id"])})
        self.assertEqual(status, 200)
        _, rec = self.store.exp.expense_by_id(item["id"])
        self.assertTrue(rec["deleted"].startswith("team · "))                  # кто и когда вместо «да»
        actions = [(a["action"], a["who"], a["source"]) for a in self.store.audit]
        self.assertEqual(actions, [("expense_add", "team", "app"), ("expense_edit", "team", "app"),
                                   ("expense_delete", "team", "app")])
        self.assertIn("лаймы — 12.50 ₼", self.store.audit[1]["before"])
        self.assertIn("13 ₼", self.store.audit[1]["after"])
        posts = [c.args[1] for c in self.bot.send_message.await_args_list]
        self.assertTrue(any("✏️ team изменил расход" in p for p in posts))
        self.assertTrue(any("🗑 team удалил расход" in p for p in posts))
        status, data = await self.call(server.audit_get, "GET", "/api/audit?limit=2")
        self.assertEqual([a["action"] for a in data["items"]], ["expense_delete", "expense_edit"])

    async def test_repeat_delete_keeps_first_author_and_logs_once(self):
        key = str(uuid4())
        _, item = await self.call(server.expenses_post, "POST", "/api/expenses", {"amount": "5", "text": "мята"},
                                  headers={"Idempotency-Key": key})
        req_anna = await self.call(server.expenses_delete, "DELETE", f"/api/expenses/{item['id']}",
                                   match_info={"id": str(item["id"])})
        self.assertEqual((req_anna[0], req_anna[1]["already"]), (200, False))
        # второй пользователь повторяет удаление
        req = make_mocked_request("DELETE", f"/api/expenses/{item['id']}", match_info={"id": str(item["id"])})
        req["user"] = {"id": 8, "first_name": "Борис"}
        resp = await server.expenses_delete(req)
        data = json.loads(resp.text)
        self.assertEqual((resp.status, data["already"], data["deleted_by"]), (200, True, "team"))
        _, rec = self.store.exp.expense_by_id(item["id"])
        self.assertTrue(rec["deleted"].startswith("team · "))
        self.assertEqual([a["action"] for a in self.store.audit].count("expense_delete"), 1)
        self.assertEqual(sum("🗑" in c.args[1] for c in self.bot.send_message.await_args_list), 1)

    async def test_concurrent_deletes_keep_one_author_and_one_event(self):
        key = str(uuid4())
        _, item = await self.call(server.expenses_post, "POST", "/api/expenses", {"amount": "5", "text": "мята"},
                                  headers={"Idempotency-Key": key})
        eid = item["id"]

        async def delete_as(uid, name):
            req = make_mocked_request("DELETE", f"/api/expenses/{eid}", match_info={"id": str(eid)})
            req["user"] = {"id": uid, "first_name": name}
            return json.loads((await server.expenses_delete(req)).text)

        results = await asyncio.gather(delete_as(1, "Аня"), delete_as(2, "Борис"), delete_as(3, "Вова"))
        self.assertEqual(sorted(r["already"] for r in results), [False, True, True])
        winner = next(r for r in results if not r["already"])["deleted_by"]
        _, rec = self.store.exp.expense_by_id(eid)
        self.assertTrue(rec["deleted"].startswith(winner + " · "))
        self.assertTrue(all(r["deleted_by"] == winner for r in results))
        self.assertEqual([a["action"] for a in self.store.audit].count("expense_delete"), 1)
        self.assertEqual(sum("🗑" in c.args[1] for c in self.bot.send_message.await_args_list), 1)

    async def test_edit_of_deleted_record_is_rejected(self):
        key = str(uuid4())
        _, item = await self.call(server.expenses_post, "POST", "/api/expenses", {"amount": "5", "text": "мята"},
                                  headers={"Idempotency-Key": key})
        await self.call(server.expenses_delete, "DELETE", f"/api/expenses/{item['id']}", match_info={"id": str(item["id"])})
        status, _ = await self.call(server.expenses_patch, "PATCH", f"/api/expenses/{item['id']}", {"amount": "9"},
                                    match_info={"id": str(item["id"])})
        self.assertEqual(status, 404)
        _, rec = self.store.exp.expense_by_id(item["id"])
        self.assertEqual(rec["amount"], 5)
        self.assertNotIn("expense_edit", [a["action"] for a in self.store.audit])

    async def test_telegram_failure_does_not_fail_edit_or_delete(self):
        key = str(uuid4())
        _, item = await self.call(server.expenses_post, "POST", "/api/expenses", {"amount": "5", "text": "мята"},
                                  headers={"Idempotency-Key": key})
        self.bot.send_message.side_effect = OSError("Telegram offline")
        with self.assertLogs("barbot.services", level="ERROR"):
            status, _ = await self.call(server.expenses_patch, "PATCH", f"/api/expenses/{item['id']}", {"amount": "6"},
                                        match_info={"id": str(item["id"])})
        self.assertEqual(status, 200)
        with self.assertLogs("barbot.services", level="ERROR"):
            status, data = await self.call(server.expenses_delete, "DELETE", f"/api/expenses/{item['id']}",
                                           match_info={"id": str(item["id"])})
        self.assertEqual((status, data["ok"]), (200, True))
        _, rec = self.store.exp.expense_by_id(item["id"])
        self.assertEqual(rec["amount"], 6)
        self.assertTrue(rec["deleted"])
        self.assertEqual([a["action"] for a in self.store.audit][-2:], ["expense_edit", "expense_delete"])

    async def test_category_change_is_visible_in_audit(self):
        key = str(uuid4())
        _, item = await self.call(server.expenses_post, "POST", "/api/expenses", {"amount": "5", "text": "мята"},
                                  headers={"Idempotency-Key": key})
        await self.call(server.expenses_patch, "PATCH", f"/api/expenses/{item['id']}", {"category": "прочее"},
                        match_info={"id": str(item["id"])})
        a = self.store.audit[-1]
        self.assertIn("(бар)", a["before"])
        self.assertIn("(прочее)", a["after"])
        self.assertNotEqual(a["before"], a["after"])

    def test_deleted_between_uses_full_timestamp(self):
        from datetime import datetime, timedelta
        from utils import now
        n = now()
        y = (n - timedelta(hours=20)).strftime("%d.%m.%Y %H:%M")        # вчера вечером
        old = (n - timedelta(days=3)).strftime("%d.%m.%Y %H:%M")
        rows = [{"id": 1, "deleted": f"Аня · {y}"}, {"id": 2, "deleted": f"Боря · {old}"}, {"id": 3, "deleted": ""}]
        gone = svc.deleted_between(rows, n - timedelta(days=1), n)
        self.assertEqual([r["id"] for r in gone], [1])
        self.assertEqual(svc.deleted_by(rows[0]), "Аня")

    async def test_topic_delete_is_logged_without_extra_post(self):
        rec, _ = self.store.exp.expense_add_once("Ильхам", 5, "мята", "бар", msg_id=77)
        row, rec = self.store.exp.expense_by_msg(77)
        await svc.expense_mark_deleted(row, rec, "Ильхам", "topic")
        self.assertEqual(self.store.audit[-1]["source"], "topic")
        self.bot.send_message.assert_not_awaited()
        self.assertEqual(self.store.exp.expenses()[0]["deleted"].split(" · ")[0], "Ильхам")

    async def test_stop_and_needs_are_logged(self):
        await self.call(server.stop_post, "POST", "/api/stop", {"item": "кампари", "on": True})
        await self.call(server.needs_post, "POST", "/api/needs", {"text": "лаймы"})
        await self.call(server.needs_close, "POST", "/api/needs/16/close", {}, match_info={"id": "16"})
        self.assertEqual([a["action"] for a in self.store.audit], ["stop_add", "need_add", "need_close"])
        self.assertTrue(all(a["source"] == "app" for a in self.store.audit))

    async def test_close_with_expense_requires_key_and_valid_amount(self):
        status, _ = await self.call(server.needs_close, "POST", "/api/needs/16/close", {"amount": "5", "text": "x"},
                                    match_info={"id": "16"})
        self.assertEqual(status, 400)
        status, _ = await self.call(server.needs_close, "POST", "/api/needs/16/close", {"amount": "-5", "text": "x"},
                                    headers={"Idempotency-Key": str(uuid4())}, match_info={"id": "16"})
        self.assertEqual(status, 400)
        self.assertEqual(len(self.store.ws_exp.rows), 1)


if __name__ == "__main__":
    unittest.main()
