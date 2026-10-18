"""HTTP API для Mini App. Публичные: /api/health, /api/menu. Остальное — только команда (см. auth)."""
import logging

from aiohttp import web

import services as svc
from api.auth import auth
from classify import CAT_ICON
from config import PORT, WEB_ORIGIN

log = logging.getLogger("barbot.api")


@web.middleware
async def cors(request: web.Request, handler):
    resp = web.Response(status=204) if request.method == "OPTIONS" else await handler(request)
    resp.headers["Access-Control-Allow-Origin"] = WEB_ORIGIN or "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Telegram-Init-Data, X-Dev-Token"
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
        body = await request.json()
        amount = round(float(str(body.get("amount", "")).replace(",", ".")), 2)
    except (ValueError, TypeError):
        return web.json_response({"error": "amount: число"}, status=400)
    text = str(body.get("text", "")).strip()
    if amount <= 0 or not text:
        return web.json_response({"error": "нужны сумма больше нуля и описание"}, status=400)
    category = body.get("category") or None
    if category and category not in CAT_ICON:
        return web.json_response({"error": "неизвестная категория"}, status=400)
    need_id = str(body.get("need_id", "") or "").lstrip("#")
    item = await svc.expense_create(_who(request), amount, text, category, need_id)
    return web.json_response(item, status=201)


async def expenses_patch(request: web.Request) -> web.Response:
    eid = int(request.match_info["id"])
    body = await request.json()
    fields = {}
    if "amount" in body:
        try:
            fields["amount"] = round(float(str(body["amount"]).replace(",", ".")), 2)
        except ValueError:
            return web.json_response({"error": "amount: число"}, status=400)
    if "text" in body:
        fields["text"] = str(body["text"]).strip()
    if body.get("category") in CAT_ICON:
        fields["category"] = body["category"]
    if not fields:
        return web.json_response({"error": "нечего менять"}, status=400)
    ok = await svc.expense_update(eid, **fields)
    return web.json_response({"ok": ok}, status=200 if ok else 404)


async def expenses_delete(request: web.Request) -> web.Response:
    ok = await svc.expense_delete(int(request.match_info["id"]))
    return web.json_response({"ok": ok}, status=200 if ok else 404)


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
    r.add_route("OPTIONS", "/api/{tail:.*}", health)
    return app


async def start() -> web.AppRunner:
    runner = web.AppRunner(make_app())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("api listening on :%d", PORT)
    return runner
