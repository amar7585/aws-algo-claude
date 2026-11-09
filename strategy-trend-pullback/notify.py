"""
Telegram delivery for the trend playbook's candidates.

A copy of strategy-range-liquidity-sweep/notify.py, not a shared module: this
function carries no layers, and the notifier is a plain urllib call.

It fires ONLY on a candidate, never on a stand-down. TELEGRAM_ENABLED unset is
SHADOW - the caller logs "WOULD SEND" and this module is never called.

Reads /algo/telegram/brief (bot_token + chat_id) - this function's own grant.
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
_config = {}


def read_telegram_config():
    if not _config:
        raw = boto3.client("ssm").get_parameter(
            Name=TELEGRAM_PARAMETER_NAME, WithDecryption=True
        )["Parameter"]["Value"]
        parsed = json.loads(raw)
        token, chat_id = parsed.get("bot_token"), parsed.get("chat_id")
        if not token or not chat_id:
            raise RuntimeError(f"{TELEGRAM_PARAMETER_NAME} must hold bot_token and chat_id")
        _config.update(token=token, chat_id=chat_id)
    return _config["token"], _config["chat_id"]


def send_telegram(text):
    token, chat_id = read_telegram_config()
    payload = urllib.parse.urlencode(
        {"chat_id": str(chat_id), "text": text, "disable_web_page_preview": "true"}
    ).encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=payload, method="POST"
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS, context=SSL_CONTEXT) as response:
        body = json.loads(response.read().decode())
    if not body.get("ok"):
        raise RuntimeError(f"telegram refused the message: {body}")
    logger.info("candidate delivered to chat %s", chat_id)
