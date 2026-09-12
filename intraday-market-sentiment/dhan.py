"""
Dhan v2 client - intraday charts, the expiry list, and the option chain.

--------------------------------------------------------------------------
Measured against the live API on 2026-09-12. Each of these is load-bearing;
getting one wrong fails quietly, not loudly.
--------------------------------------------------------------------------

* THE CHAIN IS NOT UNDER THE EXPIRY LIST'S PATH. expirylist is at
  /v2/optionchain/expirylist, but the chain itself is the FLAT
  /v2/optionchain. /v2/optionchain/optionchain answers "404 page not found".

* CHARTS HAVE NO ENVELOPE; THE CHAIN DOES. The six chart arrays (open/high/
  low/close/volume/timestamp) are top level, while the chain returns
  {"status", "data"} with the payload under `data`.

* FUTURES OPEN INTEREST IS `open_interest`, NOT `oi`. The REQUEST parameter is
  `oi: true`; the RESPONSE adds a seventh top-level array named
  `open_interest`. Asking for `payload["oi"]` gets a KeyError, and asking for
  `"oi" in payload` quietly reports False while the data sits right there.
  Floats, into a bigint column - int() them.

* The future returns MORE BARS THAN THE INDEX for the same window - 77 against
  75 on 2026-09-11. The extra two are out of session; session_candles() drops
  them, which is what makes both series land on 75 bars a session.

* `timestamp` is true UTC epoch seconds, stamped at the START of the candle.

* `fromDate` is EXCLUSIVE, `toDate` non-inclusive. The window here always
  starts at 00:00:00 of the session day, so the 09:15 bar - which IS the
  opening range - is never dropped.

* INDIA VIX carries volume 0 on every bar. That is normal for an index and is
  why vwap is computed for the index only.

* The chain is keyed by strike as a SIX-DECIMAL STRING - "23950.000000", not
  "23950" and not a number. 232 strikes on 2026-09-12, 18150 to 29700, step
  50. The step is derived from the sorted keys, never hardcoded.

* A LEG'S implied_volatility AND GREEKS COME BACK AS 0 WHEN DHAN DID NOT
  COMPUTE THEM. An illiquid deep-ITM put returned implied_volatility 0 with
  delta/theta/gamma/vega all 0 while quoting a 490.6 last price. Zero is
  "absent", not "zero volatility" - chain.py maps it to None so it can never
  be averaged or subtracted as though it were a measurement.

* The expiry list is an OPTION expiry list, ascending, mixing weeklies with
  monthlies. Measured 2026-09-12: 2026-09-15, 09-22, 09-29, 10-06, 10-13,
  10-27, 11-23, 12-29. Note 10-20 is absent - the weekly grid has holes, so a
  monthly can only be found by grouping, never by counting weeks.
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
    EXPIRYLIST_URL,
    HTTP_TIMEOUT_SECONDS,
    OPTIONCHAIN_URL,
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
            # Charts needs only access-token; the option-chain family also
            # wants client-id. Sending it on both is harmless.
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

    def intraday_candles(self, instrument, interval, day, with_oi=False):
        """
        One instrument, one interval, one whole session day.

        The window starts at 00:00:00 rather than 09:15:00 because fromDate is
        exclusive and 09:15 would drop the opening-range bar.

        `with_oi` asks for open interest. It is meaningful for F&O instruments
        only, and the array comes back named `open_interest`.
        """
        return self._post(
            CHARTS_BASE + "intraday",
            {
                "securityId": instrument["security_id"],
                "exchangeSegment": instrument["exchange_segment"],
                "instrument": instrument["instrument_type"],
                "interval": int(interval),
                "oi": bool(with_oi),
                "fromDate": f"{day.isoformat()} 00:00:00",
                "toDate": f"{day.isoformat()} 23:59:00",
            },
        )

    def expiry_list(self, underlying_scrip, underlying_seg):
        """Option expiry dates, ascending. See the module docstring."""
        payload = self._post(
            EXPIRYLIST_URL,
            {
                "UnderlyingScrip": int(underlying_scrip),
                "UnderlyingSeg": underlying_seg,
            },
        )
        dates = payload.get("data") if isinstance(payload, dict) else None
        if not dates:
            raise RuntimeError(f"expiry list empty or unexpected: {payload!r}")
        return [str(d)[:10] for d in dates]

    def option_chain(self, underlying_scrip, underlying_seg, expiry):
        """
        The full chain for one expiry.

        Returns the `data` block - {"last_price", "oc"} - not the envelope.
        Raises if the envelope is not the measured shape, because every
        downstream calculation indexes into `oc` and a changed shape must not
        degrade into an empty chain and a silently null row.
        """
        payload = self._post(
            OPTIONCHAIN_URL,
            {
                "UnderlyingScrip": int(underlying_scrip),
                "UnderlyingSeg": underlying_seg,
                "Expiry": str(expiry)[:10],
            },
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or not isinstance(data.get("oc"), dict):
            raise RuntimeError(
                f"option chain for {expiry} is not the measured "
                f'{{"status","data":{{"last_price","oc"}}}} shape: '
                f"{json.dumps(payload)[:300]}"
            )
        if not data["oc"]:
            raise RuntimeError(f"option chain for {expiry} carries no strikes")
        return data


def to_candles(payload):
    """
    Columnar arrays -> list of dicts. Raises if the arrays disagree.

    `open_interest` is carried through when present - it is absent for the
    index and for any call made with oi=false, and present as a seventh array
    for an F&O instrument fetched with oi=true.
    """
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
    oi = payload.get("open_interest")
    if oi is not None and len(oi) != len(stamps):
        raise RuntimeError(
            f"open_interest disagrees: timestamp={len(stamps)} open_interest={len(oi)}"
        )
    candles = []
    for i in range(len(stamps)):
        candle = {
            "ts": int(stamps[i]),
            "open": float(payload["open"][i]),
            "high": float(payload["high"][i]),
            "low": float(payload["low"][i]),
            "close": float(payload["close"][i]),
            "volume": int(payload["volume"][i]),
        }
        if oi is not None:
            candle["open_interest"] = int(oi[i])
        candles.append(candle)
    return candles


def session_candles(candles, day):
    """
    Keep only bars stamped inside `day`'s trading session.

    SESSION_END is EXCLUSIVE. Bars are stamped at the START of their bucket,
    so the last legitimate bar of a session starts before 15:30 - a 15:30
    stamp is post-close, like the zero-volume 19:20 candle Dhan emitted on
    2026-09-10, and like the two extra bars the future returns.
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


def require_oi(candles, symbol):
    """
    Refuse a futures series that came back without open interest.

    The buildup label is the whole reason this function asks for oi=true. If
    Dhan ever stops sending `open_interest`, or renames it again, every
    OI-derived column would quietly go null and the row would still be
    written - a schema change degrading into a silent partial write, which is
    exactly what this repo does not allow.
    """
    missing = [c["ts"] for c in candles if "open_interest" not in c]
    if missing:
        raise RuntimeError(
            f"{symbol} returned {len(missing)} of {len(candles)} bars without "
            f"an open_interest array despite oi=true - Dhan's chart payload "
            f"has changed and the buildup label has no data source"
        )
    return candles
