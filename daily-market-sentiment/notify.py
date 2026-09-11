"""Telegram delivery."""

import json
import logging
import urllib.parse
import urllib.request

from neon_access import SSL_CONTEXT, ist_datetime

from config import HTTP_TIMEOUT_SECONDS
from params import read_telegram_config

logger = logging.getLogger()


def format_brief(instrument, sentiment):
    described = ist_datetime(sentiment["trade_date"])
    lines = [
        f"{instrument['trading_symbol']} daily read - {described:%a %d %b %Y}",
        "",
        sentiment["regime"].upper(),
        f"score {sentiment['score']}  confidence {sentiment['confidence']:.0f}%",
        "",
        f"prev day   H {sentiment['pd_high']:.2f}  L {sentiment['pd_low']:.2f}  "
        f"C {sentiment['pd_close']:.2f}",
        f"rsi {sentiment['rsi']:.2f}",
        f"sma 20/50/100/200  {sentiment['sma20']:.0f} / {sentiment['sma50']:.0f} / "
        f"{sentiment['sma100']:.0f} / {sentiment['sma200']:.0f}",
    ]
    if sentiment.get("vix") is not None:
        lines.append(f"india vix  {sentiment['vix']:.2f}")
    if sentiment.get("price") is not None:
        lines += [
            "",
            f"today open {sentiment['price']:.2f}",
            f"expected move +/- {sentiment['expected_move']:.2f}  "
            f"-> {sentiment['lower_volatility']:.2f} .. "
            f"{sentiment['upper_volatility']:.2f}",
        ]
    if sentiment.get("min15_high") is not None:
        lines.append(
            f"15-min range {sentiment['min15_low']:.2f} .. "
            f"{sentiment['min15_high']:.2f}"
        )
    lines += ["", "Not a trade recommendation."]
    return "\n".join(lines)


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
    logger.info("telegram brief delivered to chat %s", chat_id)
