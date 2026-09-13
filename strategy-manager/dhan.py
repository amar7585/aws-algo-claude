"""
Dhan v2 client - intraday charts only.

WHY THIS FUNCTION CALLS DHAN AT ALL, when it has just been handed a snapshot
and reads candles from Neon: both of those are behind by construction.
snapshot_ts is the last CLOSED 5-minute bar, which at any run is up to five
minutes old, and candle_5min's newest closed bar is the same bar. The routing
decision is about what price is doing NOW - whether it is back inside a level
it swept four minutes ago - and neither stored source can answer that.

So this fetches the live series and keeps the bar the other two deliberately
exclude: the one still forming.

Measured facts inherited from intraday-market-sentiment/dhan.py, verified
2026-09-12 and load-bearing here too:

* CHARTS HAVE NO ENVELOPE. The six arrays (open/high/low/close/volume/
  timestamp) are top level - no {"status","data"} wrapper.

* `timestamp` is true UTC epoch seconds, stamped at the START of the candle.

* `fromDate` is EXCLUSIVE. The window starts at 00:00:00 of the session day,
  so the 09:15 bar - which IS the opening range - is never dropped.

* Dhan emits out-of-session bars. A zero-volume 19:20 candle appeared on
  2026-09-10; session_candles() drops them.
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
    def __init__(self, token):
        self._token = token
        self._last_call_at = 0.0
        self.calls = 0

    def _post(self, url, payload, retries=3):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "access-token": self._token,
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
        """
        One instrument, one interval, one whole session day.

        The window starts at 00:00:00 rather than 09:15:00 because fromDate is
        exclusive and 09:15 would drop the opening-range bar.
        """
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
            raise RuntimeError(
                f"chart arrays disagree: timestamp={len(stamps)} {field}={got}"
            )
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
    """
    Keep only bars stamped inside `day`'s trading session.

    SESSION_END is EXCLUSIVE. Bars are stamped at the START of their bucket,
    so the last legitimate bar of a session starts before 15:30 - a 15:30
    stamp is post-close data.
    """
    kept = [
        c
        for c in candles
        if SESSION_START <= ist_datetime(c["ts"]).time() < SESSION_END
        and ist_datetime(c["ts"]).date() == day
    ]
    if len(candles) != len(kept):
        logger.info("dropped %d out-of-session candle(s)", len(candles) - len(kept))
    return kept


def live_price(candles, interval_seconds, now):
    """
    The newest bar of the live series, and whether it is still forming.

    THE PARTIAL BAR IS THE POINT HERE. Everywhere else in this repo a partial
    bar is filtered out; this is the one read that wants it, because it is the
    only thing carrying the current price. `partial` says which it is, so the
    strategy can tell a confirmed close from a price that may still move - the
    sweep playbook cares a great deal about that difference, since a close
    back inside a level is the trade and a wick that has not closed yet is not.
    """
    if not candles:
        raise RuntimeError("live series is empty - no bar to take a price from")
    newest = max(candles, key=lambda c: c["ts"])
    return {
        "ts": newest["ts"],
        "price": newest["close"],
        "high": newest["high"],
        "low": newest["low"],
        "partial": (newest["ts"] + interval_seconds) > int(now),
    }
