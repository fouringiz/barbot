"""HTTP API для Mini App. Публичные: /api/health, /api/menu. Остальное — только команда (см. auth)."""
import json
import logging
import math
from uuid import UUID

from aiohttp import web

import services as svc
import sheets
from sheets import ExpenseConflict, run
from api.auth import auth
from classify import CAT_ICON, TYPE_ICON, classify
from config import PORT, WEB_ORIGIN
from utils import short, stamp

log = logging.getLogger("barbot.api")


@web.middleware
async def cors(request: web.Request, handler):
    resp = web.Response(status=204) if request.method == "OPTIONS" else await handler(request)
    resp.headers["Access-Control-Allow-Origin"] = WEB_ORIGIN or "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Telegram-Init-Data, X-Dev-Token, Idempotency-Key"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
    return resp


def _who(request: web.Request) -> str:
    return request["user"].get("first_name") or request["user"].get("username") or "команда"


async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def menu(_: web.Request) -> web.Response:
    return web.json_response({"items": await svc.menu_for_guests()})


async def me(request: web.Request) -> web.Response:
    return web.json_response({"user": request["user"], "role": "team", "categories": list(CAT_ICON)})


async def expenses_get(request: web.Request) -> web.Response:
    period = request.query.get("period", "day")
    if period not in ("day", "week", "month"):
        return web.json_response({"error": "period: day|week|month"}, status=400)
    return web.json_response(await svc.expenses_summary(period))


async def expenses_post(request: web.Request) -> web.Response:
    try:
        key = str(UUID(request.headers.get("Idempotency-Key", "")))
    except ValueError:
        return web.json_response({"error": "обнови приложение: нужен ключ сохранения"}, status=400)
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError
        amount = round(float(str(body.get("amount", "")).replace(",", ".")), 2)
    except (ValueError, TypeError):
        return web.json_response({"error": "amount: число"}, status=400)
    text = body.get("text", "")
    text = text.strip() if isinstance(text, str) else ""
    if not math.isfinite(amount) or amount <= 0 or not text or len(text) > 1000:
        return web.json_response({"error": "нужны конечная сумма больше нуля и описание до 1000 символов"}, status=400)
    category = body.get("category") or None
    if category is not None and (not isinstance(category, str) or category not in CAT_ICON):
        return web.json_response({"error": "неизвестная категория"}, status=400)
    need_id = str(body.get("need_id", "") or "").lstrip("#")
    if need_id and (not need_id.isascii() or not need_id.isdigit() or len(need_id) > 15 or int(need_id) < 1):
        return web.json_response({"error": "номер заявки: целое число больше нуля"}, status=400)
    need_id = str(int(need_id)) if need_id else ""
    try:
        item, created = await svc.expense_create(_who(request), amount, text, category, need_id,
                                                request_key=f"app:{request['user']['id']}:{key}")
    except ExpenseConflict as e:
        return web.json_response({"error": str(e)}, status=409)
    return web.json_response(item, status=201 if created else 200)


async def expenses_patch(request: web.Request) -> web.Response:
    eid = int(request.match_info["id"])
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError
    except ValueError:
        return web.json_response({"error": "ожидается объект JSON"}, status=400)
    fields = {}
    if "amount" in body:
        try:
            fields["amount"] = round(float(str(body["amount"]).replace(",", ".")), 2)
        except ValueError:
            return web.json_response({"error": "amount: число"}, status=400)
        if not math.isfinite(fields["amount"]) or fields["amount"] <= 0:
            return web.json_response({"error": "сумма должна быть конечным числом больше нуля"}, status=400)
    if "text" in body:
        fields["text"] = body["text"].strip() if isinstance(body["text"], str) else ""
        if not fields["text"] or len(fields["text"]) > 1000:
            return web.json_response({"error": "описание: от 1 до 1000 символов"}, status=400)
    category = body.get("category")
    if category is not None and (not isinstance(category, str) or category not in CAT_ICON):
        return web.json_response({"error": "неизвестная категория"}, status=400)
    if category:
        fields["category"] = body["category"]
    if not fields:
        return web.json_response({"error": "нечего менять"}, status=400)
    ok = await svc.expense_update(eid, _who(request), "app", **fields)
    return web.json_response({"ok": ok}, status=200 if ok else 404)


async def expenses_delete(request: web.Request) -> web.Response:
    eid = int(request.match_info["id"])
    found = await run(sheets.get().expense_by_id, eid)
    if not found:
        return web.json_response({"ok": False}, status=404)
    done = await svc.expense_mark_deleted(found[0], found[1], _who(request), "app", notify=True)
    return web.json_response({"ok": True, "already": not done, "deleted_by": svc.deleted_by(found[1]) if not done else _who(request)})


def _json_object(body):
    if not isinstance(body, dict):
        raise ValueError
    return body


# --- stop-list
async def stop_get(_: web.Request) -> web.Response:
    return web.json_response(await svc.stop_state())


async def stop_post(request: web.Request) -> web.Response:
    try:
        body = _json_object(await request.json())
    except ValueError:
        return web.json_response({"error": "ожидается объект JSON"}, status=400)
    item = body.get("item", "")
    item = item.strip() if isinstance(item, str) else ""
    if not item or len(item) > 100:
        return web.json_response({"error": "item: от 1 до 100 символов"}, status=400)
    if body.get("on", True):
        res = await svc.stop_add(item, _who(request), source="app")
        return web.json_response(res or {"item": item, "affected": [], "already": True})
    ok = await svc.stop_remove(item, _who(request), source="app")
    return web.json_response({"ok": ok}, status=200 if ok else 404)


# --- needs
async def needs_get(_: web.Request) -> web.Response:
    return web.json_response({"items": await svc.needs_open(), "types": list(TYPE_ICON)})


async def needs_post(request: web.Request) -> web.Response:
    try:
        body = _json_object(await request.json())
    except ValueError:
        return web.json_response({"error": "ожидается объект JSON"}, status=400)
    text = body.get("text", "")
    text = text.strip() if isinstance(text, str) else ""
    if not text or len(text) > 1000:
        return web.json_response({"error": "text: от 1 до 1000 символов"}, status=400)
    type_ = body.get("type") or None
    if type_ is not None and (not isinstance(type_, str) or type_ not in TYPE_ICON):
        return web.json_response({"error": "неизвестный тип"}, status=400)
    type_ = type_ or classify(text)
    nid = await svc.add_need(type_, text, _who(request), source="app")
    return web.json_response({"id": nid, "type": type_, "text": text, "who": _who(request), "created": short(stamp())},
                             status=201)


async def needs_close(request: web.Request) -> web.Response:
    """Закрыть заявку; с суммой — ещё и записать расход по ней (идемпотентно по Idempotency-Key)."""
    nid = int(request.match_info["id"])
    try:
        raw = (await request.text()).strip()
        body = _json_object(json.loads(raw)) if raw else {}
    except ValueError:
        return web.json_response({"error": "ожидается объект JSON"}, status=400)
    amount = body.get("amount")
    if amount in (None, ""):
        rec = await svc.need_close(nid, _who(request), strict_summary=False, source="app")
        return web.json_response({"ok": rec is not None, "already": bool(rec and rec.get("already"))},
                                 status=200 if rec is not None else 404)
    try:
        key = str(UUID(request.headers.get("Idempotency-Key", "")))
    except ValueError:
        return web.json_response({"error": "обнови приложение: нужен ключ сохранения"}, status=400)
    try:
        amount = round(float(str(amount).replace(",", ".")), 2)
    except (ValueError, TypeError):
        return web.json_response({"error": "amount: число"}, status=400)
    text = body.get("text", "")
    text = text.strip() if isinstance(text, str) else ""
    if not math.isfinite(amount) or amount <= 0 or not text or len(text) > 1000:
        return web.json_response({"error": "нужны конечная сумма больше нуля и описание до 1000 символов"}, status=400)
    category = body.get("category") or None
    if category is not None and (not isinstance(category, str) or category not in CAT_ICON):
        return web.json_response({"error": "неизвестная категория"}, status=400)
    try:
        item, created = await svc.expense_create(_who(request), amount, text, category, str(nid),
                                                request_key=f"app:{request['user']['id']}:{key}")
    except ExpenseConflict as e:
        return web.json_response({"error": str(e)}, status=409)
    return web.json_response({"ok": True, "expense": item}, status=201 if created else 200)


# --- audit
async def audit_get(request: web.Request) -> web.Response:
    try:
        limit = max(1, min(200, int(request.query.get("limit", "50"))))
    except ValueError:
        limit = 50
    return web.json_response({"items": await svc.audit_tail(limit)})


# --- specs
async def specs_get(_: web.Request) -> web.Response:
    return web.json_response({"items": await svc.specs_all()})


def make_app() -> web.Application:
    app = web.Application(middlewares=[cors, auth])
    r = app.router
    r.add_get("/api/health", health)
    r.add_get("/api/menu", menu)
    r.add_get("/api/me", me)
    r.add_get("/api/expenses", expenses_get)
    r.add_post("/api/expenses", expenses_post)
    r.add_patch(r"/api/expenses/{id:\d+}", expenses_patch)
    r.add_delete(r"/api/expenses/{id:\d+}", expenses_delete)
    r.add_get("/api/stop", stop_get)
    r.add_post("/api/stop", stop_post)
    r.add_get("/api/needs", needs_get)
    r.add_post("/api/needs", needs_post)
    r.add_post(r"/api/needs/{id:\d+}/close", needs_close)
    r.add_get("/api/specs", specs_get)
    r.add_get("/api/audit", audit_get)
    r.add_route("OPTIONS", "/api/{tail:.*}", health)
    return app


async def start() -> web.AppRunner:
    runner = web.AppRunner(make_app())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("api listening on :%d", PORT)
    return runner
