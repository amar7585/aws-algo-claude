"""
Dhan v2 intraday charts - today's 5-min candles for the index and the future.

A trimmed copy of intraday-fno-poller/dhan.py: the chart call only, no expiry
list or option chain. A copy rather than a shared module for the same reason as
there: sharing it would mean the neon-access layer, where every change means
republishing it and repointing every function's pinned ARN.

Measured facts this relies on (see the poller's copy for the full list):
* Chart arrays come back top level, no envelope; `timestamp` is true UTC epoch
  seconds stamped at the START of the candle.
* `fromDate` is EXCLUSIVE, so the window starts at 00:00:00 - a 09:15 start
  would drop the 09:15 bar, and on the current day a start one bar back returns
  that bucket missing its first minute (CLAUDE.md, measured 2026-10-19).
* The future returns bars outside the session; session_candles() drops them.
"""

import json
import logging
import time
import urllib.error
import urllib.request

from neon_access import SSL_CONTEXT, ist_datetime

from config import (
    API_PACING_SECONDS,
    CHARTS_BASE,
    HTTP_TIMEOUT_SECONDS,
    SESSION_END,
    SESSION_START,
)

logger = logging.getLogger()

CHART_ARRAYS = ("open", "high", "low", "close", "volume")


class DhanClient:
    def __init__(self, token, client_id):
        self._token = token
        self._client_id = client_id
        self._last_call_at = 0.0
        self.calls = 0

    def _post(self, url, payload, retries=3):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "access-token": self._token,
            "client-id": self._client_id,
        }
        for attempt in range(retries + 1):
            elapsed = time.monotonic() - self._last_call_at
            if elapsed < API_PACING_SECONDS:
                time.sleep(API_PACING_SECONDS - elapsed)
            self._last_call_at = time.monotonic()
            self.calls += 1
            request = urllib.request.Request(
                url, data=json.dumps(payload).encode(), headers=headers, method="POST"
            )
            try:
                with urllib.request.urlopen(
                    request, timeout=HTTP_TIMEOUT_SECONDS, context=SSL_CONTEXT
                ) as response:
                    return json.loads(response.read().decode())
            except urllib.error.HTTPError as exc:
                body = exc.read().decode()[:400]
                if (exc.code == 429 or "DH-904" in body) and attempt < retries:
                    backoff = API_PACING_SECONDS * (2 ** (attempt + 1))
                    logger.warning("rate limited, retrying in %.0fs", backoff)
                    time.sleep(backoff)
                    continue
                raise RuntimeError(f"dhan {url} -> HTTP {exc.code}: {body}") from exc
        raise RuntimeError(f"dhan {url} exhausted {retries} retries")

    def intraday_candles(self, instrument, interval, day):
        """One instrument's candles for one whole session day (window from 00:00)."""
        return self._post(
            CHARTS_BASE + "intraday",
            {
                "securityId": instrument["security_id"],
                "exchangeSegment": instrument["exchange_segment"],
                "instrument": instrument["instrument_type"],
                "interval": int(interval),
                "oi": False,
                "fromDate": f"{day.isoformat()} 00:00:00",
                "toDate": f"{day.isoformat()} 23:59:00",
            },
        )


def to_candles(payload):
    """Columnar arrays -> list of dicts. Raises if the arrays disagree."""
    if not isinstance(payload, dict):
        raise RuntimeError(f"unexpected chart payload: {payload!r}")
    stamps = payload.get("timestamp")
    if stamps is None:
        raise RuntimeError(f"chart payload has no timestamp array: {sorted(payload)}")
    for field in CHART_ARRAYS:
        got = len(payload.get(field) or [])
        if got != len(stamps):
            raise RuntimeError(f"chart arrays disagree: timestamp={len(stamps)} {field}={got}")
    return [
        {
            "ts": int(stamps[i]),
            "open": float(payload["open"][i]),
            "high": float(payload["high"][i]),
            "low": float(payload["low"][i]),
            "close": float(payload["close"][i]),
            "volume": int(payload["volume"][i]),
        }
        for i in range(len(stamps))
    ]


def session_candles(candles, day):
    """Only bars stamped inside `day`'s session (SESSION_END exclusive)."""
    kept = [
        c for c in candles
        if SESSION_START <= ist_datetime(c["ts"]).time() < SESSION_END
        and ist_datetime(c["ts"]).date() == day
    ]
    if len(candles) != len(kept):
        logger.info("dropped %d out-of-session candle(s)", len(candles) - len(kept))
    return kept
