"""Авторизация Mini App: подпись initData от Telegram + членство в группе бара."""
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timedelta
from urllib.parse import parse_qsl

from aiohttp import web

import services as svc
from config import BOT_TOKEN, DEV_TOKEN, GROUP_ID, TZ
from utils import now

log = logging.getLogger("barbot.auth")
PUBLIC = {"/api/health", "/api/menu"}
_members: dict[int, tuple[bool, datetime]] = {}


def parse_init_data(init_data: str) -> dict | None:
    """Проверяет подпись initData (HMAC-SHA256 ключом WebAppData+токен). Возвращает user или None."""
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop("hash", None)
    if not received:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received):
        return None
    if time.time() - int(pairs.get("auth_date", "0") or 0) > 86400:
        return None
    user = json.loads(pairs.get("user", "{}") or "{}")
    return user if user.get("id") else None


async def is_member(user_id: int) -> bool:
    ok, at = _members.get(user_id, (False, datetime.min.replace(tzinfo=TZ)))
    if now() - at < timedelta(minutes=10):
        return ok
    try:
        cm = await svc.bot.get_chat_member(GROUP_ID, user_id)
        ok = cm.status in {"creator", "administrator", "member", "restricted"}
    except Exception as e:
        log.warning("get_chat_member %s: %s", user_id, e)
        ok = False
    _members[user_id] = (ok, now())
    return ok


def _deny(status: int, msg: str):
    return web.json_response({"error": msg}, status=status)


@web.middleware
async def auth(request: web.Request, handler):
    if request.method == "OPTIONS" or request.path in PUBLIC:
        return await handler(request)
    if DEV_TOKEN and request.headers.get("X-Dev-Token") == DEV_TOKEN:
        request["user"] = {"id": 0, "first_name": "dev"}
        return await handler(request)
    user = parse_init_data(request.headers.get("X-Telegram-Init-Data", ""))
    if not user:
        return _deny(401, "открой приложение из Telegram")
    if not GROUP_ID or not await is_member(int(user["id"])):
        return _deny(403, "доступ только для команды бара")
    request["user"] = user
    return await handler(request)
