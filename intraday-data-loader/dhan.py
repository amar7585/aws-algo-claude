"""
Dhan v2 client - intraday charts, and the expiry list that resolves the
current-month future.

--------------------------------------------------------------------------
Measured against live data. Each of these is load-bearing; getting one wrong
fails quietly, not loudly.
--------------------------------------------------------------------------

* No {"status", "data"} envelope on charts - the six arrays (open/high/low/
  close/volume/timestamp) are top level. The dhanhq SDK adds that wrapper;
  REST does not. The expiry-list endpoint DOES use an envelope.

* `timestamp` is true UTC epoch seconds, stamped at the START of the candle.

* `fromDate` is EXCLUSIVE, `toDate` non-inclusive. Exclusivity is what makes
  incremental resume clean - but it is also why this function steps BACK one
  whole interval before resuming. The newest stored bar may be partial, and a
  resume from its own timestamp would never re-fetch it, leaving it partial
  forever. See fetch_window() in handler.py.

* Intraday is capped at 90 days per call (HTTP 400 DH-905 past it).

* `interval: 60` is SESSION-aligned, not clock-aligned - buckets start 09:15,
  10:15, 11:15 ... 15:15, seven per session with the last a 15-minute stub.
  Measured 2026-09-11 against the 2026-09-10 session by shortening `toDate`:

      toDate 10:10 -> 1 candle   (09:15 bucket still forming)
      toDate 10:20 -> 2 candles  (09:15 closed, 10:15 forming)

  Clock-aligned bucketing would have returned 2 and 2. A whole-session fetch
  returns 7 either way, which is why the short window was needed to tell them
  apart. The schedule's hourly trigger times derive from this, so it is
  asserted at runtime in handler.py.

* The same short-window test shows Dhan RETURNS THE IN-PROGRESS BUCKET. That
  is what makes storing partial candles workable: the bar is written as it
  forms and corrected by the primary-key upsert on a later pass.

* Volume arrives as a JSON float (334207690.0) into a bigint column - int() it.

* Intraday returns stray post-close candles with zero volume echoing the close
  (a 19:20 candle on 2026-09-10). session_candles() filters them out; without
  that they corrupt any volume-weighted calculation.

* The expiry list is an OPTION expiry list, ascending, mixing weeklies with
  the monthlies and quarterlies further out. The nearest entry's MONTH is the
  current futures contract's month, and it rolls on its own: once September's
  monthly expiry passes, the nearest expiry is already in October. Measured
  2026-09-11 - nearest 2026-09-15, September monthly 2026-09-29, then
  2026-10-06.
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
    OPTIONCHAIN_BASE,
    SESSION_END,
    SESSION_START,
)

logger = logging.getLogger()


class DhanClient:
    def __init__(self, token, client_id=None):
        self._token = token
        self._client_id = client_id
        self._last_call_at = 0.0
        self.calls = 0

    def use_client_id(self, client_id):
        """
        Supply the client id late.

        Only the expiry call needs it, and that happens after the index
        candles are already committed. Resolving it in __init__ would abort
        the whole run over a header that the charts fetches never use.
        """
        self._client_id = client_id

    def _post(self, url, payload, retries=3):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "access-token": self._token,
        }
        # Charts needs only access-token. The option-chain family also wants
        # client-id; sending it on both is harmless.
        if self._client_id:
            headers["client-id"] = self._client_id
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

    def intraday_candles(self, instrument, interval, from_dt, to_dt):
        """
        Candles for one instrument at one interval over an explicit window.

        The window is passed in rather than derived from a date because this
        function resumes mid-session: see fetch_window() in handler.py for how
        the start is chosen, and why it steps back one interval.
        """
        return self._post(
            CHARTS_BASE + "intraday",
            {
                "securityId": instrument["security_id"],
                "exchangeSegment": instrument["exchange_segment"],
                "instrument": instrument["instrument_type"],
                "interval": int(interval),
                "oi": False,
                "fromDate": from_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "toDate": to_dt.strftime("%Y-%m-%d %H:%M:%S"),
            },
        )

    def expiry_list(self, underlying_scrip, underlying_seg):
        """Option expiry dates, ascending. See the module docstring."""
        payload = self._post(
            OPTIONCHAIN_BASE + "expirylist",
            {
                "UnderlyingScrip": int(underlying_scrip),
                "UnderlyingSeg": underlying_seg,
            },
        )
        dates = payload.get("data") if isinstance(payload, dict) else None
        if not dates:
            raise RuntimeError(f"expiry list empty or unexpected: {payload!r}")
        return list(dates)


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


def session_candles(candles):
    """
    Keep only bars stamped inside the trading session, across any number of
    days - this function backfills up to 90 of them, so unlike the
    daily-market-sentiment version there is no single-day check.

    SESSION_END is EXCLUSIVE here. Bars are stamped at the START of their
    bucket, so the last legitimate bar of a session starts before 15:30 - a
    15:30 stamp is post-close, like the zero-volume 19:20 candle Dhan emitted
    on 2026-09-10.
    """
    kept = [
        c
        for c in candles
        if SESSION_START <= ist_datetime(c["ts"]).time() < SESSION_END
    ]
    if len(candles) != len(kept):
        logger.info("dropped %d out-of-session candle(s)", len(candles) - len(kept))
    return kept
