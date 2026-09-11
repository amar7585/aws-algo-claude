"""
Dhan v2 charts API client.

--------------------------------------------------------------------------
Measured against live data, 2026-09-10/11. Each of these is load-bearing;
getting one wrong fails quietly, not loudly.
--------------------------------------------------------------------------

* No {"status", "data"} envelope - the six arrays (open/high/low/close/volume/
  timestamp) are top level. The dhanhq SDK adds that wrapper; REST does not.

* `timestamp` is true UTC epoch seconds, stamped at the START of the candle
  (IST midnight for daily). No IST offset bug here, unlike `expiryTime` on the
  auth endpoint.

* `fromDate` is EXCLUSIVE. Passing "09:15:00" silently drops the 09:15 candle,
  which IS the opening range. intraday_candles() therefore starts at 00:00:00.
  The same property makes incremental resume clean: passing the last stored
  candle_ts back returns only genuinely new candles - no duplicate, no gap.

* `toDate` is non-inclusive, so it is always set past the last candle wanted.

* Intraday is capped at 90 days per call (HTTP 400 DH-905 past it). Daily has
  no cap - one call returned 5,129 candles back to 2006 - so the daily path
  needs no chunking however stale the last stored candle is.

* The daily endpoint LAGS A SESSION. On the evening of 2026-09-10, with the
  full 09-10 session present in intraday, the newest daily candle was still
  09-09; it appeared overnight. So the sentiment read describes YESTERDAY and
  today's open must come from intraday.

* Volume arrives as a JSON float (334207690.0) into a bigint column - int() it.

* Intraday returns stray post-close candles with zero volume echoing the close
  (a 19:20 candle on 2026-09-10). session_candles() filters them out; without
  that they corrupt any volume-weighted calculation.
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


class DhanClient:
    def __init__(self, token):
        self._token = token
        self._last_call_at = 0.0
        self.calls = 0

    def _post(self, path, payload, retries=3):
        url = CHARTS_BASE + path
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

    def daily_candles(self, instrument, from_date, to_date):
        return self._post(
            "historical",
            {
                "securityId": instrument["security_id"],
                "exchangeSegment": instrument["exchange_segment"],
                "instrument": instrument["instrument_type"],
                "expiryCode": 0,
                "oi": False,
                "fromDate": from_date.isoformat(),
                "toDate": to_date.isoformat(),
            },
        )

    def intraday_candles(self, instrument, interval, day):
        # 00:00:00, not 09:15:00 - fromDate is exclusive and 09:15 would drop
        # the opening-range candle.
        return self._post(
            "intraday",
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
    for field in ("open", "high", "low", "close", "volume"):
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
    """Keep only 09:15-15:30 candles belonging to `day`."""
    kept = [
        c
        for c in candles
        if SESSION_START <= ist_datetime(c["ts"]).time() <= SESSION_END
        and ist_datetime(c["ts"]).date() == day
    ]
    if len(candles) != len(kept):
        logger.info("dropped %d out-of-session candle(s)", len(candles) - len(kept))
    return kept
