"""
Telegram delivery.

A trimmed copy of daily-market-sentiment/notify.py rather than a shared
module. Sharing it would mean putting it in the neon-access layer, and that
layer pulls in pg8000 - see config.py. Twenty lines duplicated is the cheaper
trade than a database driver in a notifier, and it keeps this function
independently deployable.
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

# Reused across warm invocations; creating a boto3 client is not cheap.
_ssm = boto3.client("ssm")
_config = {}


def read_telegram_config():
    if not _config:
        raw = _ssm.get_parameter(
            Name=TELEGRAM_PARAMETER_NAME, WithDecryption=True
        )["Parameter"]["Value"]
        parsed = json.loads(raw)
        token, chat_id = parsed.get("bot_token"), parsed.get("chat_id")
        if not token or not chat_id:
            raise RuntimeError(
                f"{TELEGRAM_PARAMETER_NAME} must hold bot_token and chat_id"
            )
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
    with urllib.request.urlopen(
        request, timeout=HTTP_TIMEOUT_SECONDS, context=SSL_CONTEXT
    ) as response:
        body = json.loads(response.read().decode())
    if not body.get("ok"):
        raise RuntimeError(f"telegram refused the message: {body}")
    logger.info("alert delivered to chat %s", chat_id)
