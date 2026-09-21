"""
Telegram delivery for the sweep playbook's trade signals.

A trimmed copy of daily-market-sentiment/notify.py rather than a shared module -
sharing it would mean the neon-access layer, which pulls in pg8000, and this
playbook already carries that layer for connect() but keeps the notifier
separate so it stays a pure urllib call with no extra dependency.

WHY A NOTIFIER IN A PLAYBOOK THAT "WRITES NOTHING". It still writes nothing - no
table, no order. A Telegram message is not persistence; it is the same log
output pushed to where it is seen. It fires ONLY on a qualifying candidate (a
real signal), never on a rejection, so the chat is not spammed with the
stand-down days that are most of them.

Reads /algo/telegram/brief (bot_token + chat_id), the same parameter the daily
brief uses. This function already reads /algo/neon/connection, so its role has
an SSM-read policy; it needs that policy widened to this one extra parameter
plus kms:Decrypt - its own grant, never borrowed.
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
    logger.info("signal delivered to chat %s", chat_id)
