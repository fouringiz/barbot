"""HTTP API для Mini App. Пока только health и меню гостей; авторизация появится вместе с фронтом."""
import logging

from aiohttp import web

import services as svc
from config import PORT, WEB_ORIGIN

log = logging.getLogger("barbot.api")


@web.middleware
async def cors(request: web.Request, handler):
    if request.method == "OPTIONS":
        resp = web.Response(status=204)
    else:
        resp = await handler(request)
    resp.headers["Access-Control-Allow-Origin"] = WEB_ORIGIN or "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Telegram-Init-Data"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, OPTIONS"
    return resp


async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def menu(_: web.Request) -> web.Response:
    return web.json_response({"items": await svc.menu_for_guests()})


def make_app() -> web.Application:
    app = web.Application(middlewares=[cors])
    app.router.add_get("/api/health", health)
    app.router.add_get("/api/menu", menu)
    app.router.add_route("OPTIONS", "/api/{tail:.*}", health)
    return app


async def start() -> web.AppRunner:
    runner = web.AppRunner(make_app())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("api listening on :%d", PORT)
    return runner
