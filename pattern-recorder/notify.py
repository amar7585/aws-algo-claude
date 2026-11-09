"""
Telegram delivery - a copy of pattern-detector/notify.py.

A copy rather than a shared module: sharing it would mean the neon-access layer.
Pure urllib. Reads /algo/telegram/brief (bot_token + chat_id), the SAME parameter
the daily brief, error-notifier and pattern-detector use, so v2 alerts land in
the same chat. The execution role needs ssm:GetParameter on that parameter plus
kms:Decrypt - its OWN grant, never borrowed.
"""

import json
import logging
import ssl
import urllib.parse
import urllib.request

import boto3

from config import TELEGRAM_PARAMETER_NAME, TELEGRAM_TIMEOUT_SECONDS

logger = logging.getLogger()

SSL_CONTEXT = ssl.create_default_context()

_ssm = None
_config = {}


def read_telegram_config():
    global _ssm
    if not _config:
        _ssm = _ssm or boto3.client("ssm")
        raw = _ssm.get_parameter(Name=TELEGRAM_PARAMETER_NAME, WithDecryption=True)["Parameter"]["Value"]
        parsed = json.loads(raw)
        token, chat_id = parsed.get("bot_token"), parsed.get("chat_id")
        if not token or not chat_id:
            raise RuntimeError(f"{TELEGRAM_PARAMETER_NAME} must hold bot_token and chat_id")
        _config.update(token=token, chat_id=chat_id)
    return _config["token"], _config["chat_id"]


def send_telegram(text):
    """Deliver one message; raises unless Telegram answers ok."""
    token, chat_id = read_telegram_config()
    payload = urllib.parse.urlencode(
        {"chat_id": str(chat_id), "text": text, "disable_web_page_preview": "true"}
    ).encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=payload, method="POST"
    )
    with urllib.request.urlopen(request, timeout=TELEGRAM_TIMEOUT_SECONDS, context=SSL_CONTEXT) as response:
        body = json.loads(response.read().decode())
    if not body.get("ok"):
        raise RuntimeError(f"telegram refused the message: {body}")
    logger.info("alert delivered to chat %s", chat_id)
