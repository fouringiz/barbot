"""Переменные окружения — единственное место, где читается os.environ."""
import json
import os
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Baku")
DATE_FMT = "%d.%m.%Y %H:%M"

BOT_TOKEN = os.environ["BOT_TOKEN"]
GROUP_ID = int(os.environ.get("GROUP_ID", "0"))                 # 0 = ещё не настроено, /id работает
TOPIC_STOP = int(os.environ.get("TOPIC_STOP", "0")) or None     # 0 = General
TOPIC_NEEDS = int(os.environ.get("TOPIC_NEEDS", "0")) or None
TOPIC_SPENT = int(os.environ.get("TOPIC_SPENT", "0")) or None   # топик «Расходы»; пусто = модуль выключен
CLOSE_HOUR, CLOSE_MIN = (int(x) for x in os.environ.get("CLOSE_TIME", "02:00").split(":"))

SHEET_ID = os.environ["SHEET_ID"]
CREDS = json.loads(os.environ["GOOGLE_CREDS_JSON"])

# HTTP API для Mini App
PORT = int(os.environ.get("PORT", "8080"))                       # Railway подставляет сам
WEB_ORIGIN = os.environ.get("WEB_ORIGIN", "")                    # https://<user>.github.io — CORS; пусто = любой

# Mini App
APP_LINK = os.environ.get("APP_LINK", "")                        # https://t.me/bakiliBarBot/app — после /newapp в BotFather
DEV_TOKEN = os.environ.get("DEV_TOKEN", "")                      # заголовок X-Dev-Token для теста API без Telegram; пусто = выключено
