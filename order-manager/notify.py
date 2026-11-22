"""
Telegram delivery for order-manager - the trade messages, sent only when a step is applied.

A copy of strategy-range-liquidity-sweep/notify.py (itself trimmed from
daily-market-sentiment's) rather than a shared module: a pure urllib call with no
extra dependency.

THE TEXT IS THE PLAYBOOK'S (Amar 2026-11-22): each caller passes its own message,
word for word; this function only decides WHETHER it goes - once, when the step is
applied - and sends it inside the same transaction as the position update, so the
message and the record always agree. A refused send raises, the update rolls back,
and the caller's retry sends it again.

Reads /algo/telegram/brief (bot_token + chat_id), the same parameter the daily brief
uses - its own grant (iam/order-manager-role.algo-ssm-read.json), never borrowed.
"""

import json
import logging
import ssl
import urllib.parse
import urllib.request

import boto3

from config import HTTP_TIMEOUT_SECONDS, TELEGRAM_PARAMETER_NAME

logger = logging.getLogger()

SSL_CONTEXT = ssl.create_default_context()

_ssm = boto3.client("ssm")
_config = {}


def read_telegram_config():
    if not _config:
        raw = _ssm.get_parameter(Name=TELEGRAM_PARAMETER_NAME, WithDecryption=True)["Parameter"]["Value"]
        parsed = json.loads(raw)
        token, chat_id = parsed.get("bot_token"), parsed.get("chat_id")
        if not token or not chat_id:
            raise RuntimeError(f"{TELEGRAM_PARAMETER_NAME} must hold bot_token and chat_id")
        _config.update(token=token, chat_id=chat_id)
    return _config["token"], _config["chat_id"]


def send_telegram(text):
    token, chat_id = read_telegram_config()
    payload = urllib.parse.urlencode({"chat_id": str(chat_id), "text": text, "disable_web_page_preview": "true"}).encode()
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=payload, method="POST")
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS, context=SSL_CONTEXT) as response:
        body = json.loads(response.read().decode())
    if not body.get("ok"):
        raise RuntimeError(f"telegram refused the message: {body}")
    logger.info("message delivered to chat %s", chat_id)
