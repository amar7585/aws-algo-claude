"""
Dhan DATA reads for order-manager's paper option (2026-11-28): the expiry list, the option chain, a quote. No order
API is called, so no static IP is needed (Dhan: static IP only for order placement / modification / cancellation).

Shapes as measured elsewhere in this repo:
  * expirylist  POST /v2/optionchain/expirylist {UnderlyingScrip, UnderlyingSeg} -> {"data": ["2026-12-01", ...]}
                ascending, weeklies and monthlies mixed (intraday-fno-poller/dhan.py)
  * optionchain POST /v2/optionchain {UnderlyingScrip, UnderlyingSeg, Expiry} -> {"data": {"last_price", "oc":
                {"25750.000000": {"ce": {"security_id", "last_price", ...}, "pe": {...}}}}} (the poller's chain.py)
  * quote       POST /v2/marketfeed/ohlc {"NSE_FNO": [id]} -> {"data": {"NSE_FNO": {"<id>": {"last_price", "ohlc"}}}}
                keyed by the id AS A STRING; `last_price` is live (intraday-market-sentiment/dhan.py)
Both families want access-token AND client-id headers. Dhan allows the option-chain family 1 request per 3 s and
quotes 1 per second: calls are paced per family, and a DH-904 / 429 (another function used the allowance this
second) is retried with backoff. Anything else - or a shape that is not the measured one - RAISES (fail loudly).
"""

import json
import logging
import time
import urllib.error
import urllib.request

from neon_access import SSL_CONTEXT

from config import (CHAIN_PACING_SECONDS, DHAN_RETRIES, DHAN_TIMEOUT_SECONDS, EXPIRYLIST_URL, MARKETFEED_OHLC_URL,
                    OPTIONCHAIN_URL, QUOTE_PACING_SECONDS)

logger = logging.getLogger()


class DhanClient:
    def __init__(self, token, client_id, sleep=time.sleep, clock=time.monotonic, opener=None):
        self._token, self._client_id = token, client_id
        self._sleep, self._clock = sleep, clock
        self._open = opener or (lambda req: urllib.request.urlopen(req, timeout=DHAN_TIMEOUT_SECONDS, context=SSL_CONTEXT))
        self._last = {"chain": -1e9, "quote": -1e9}
        self.calls = 0

    def _post(self, family, url, payload):
        pacing = CHAIN_PACING_SECONDS if family == "chain" else QUOTE_PACING_SECONDS
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "access-token": self._token, "client-id": self._client_id}
        for attempt in range(DHAN_RETRIES + 1):
            wait = pacing - (self._clock() - self._last[family])
            if wait > 0:
                self._sleep(wait)
            self._last[family] = self._clock()
            self.calls += 1
            req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
            try:
                with self._open(req) as resp:
                    return json.loads(resp.read().decode())
            except urllib.error.HTTPError as exc:
                body = exc.read().decode()[:400]
                if (exc.code == 429 or "DH-904" in body) and attempt < DHAN_RETRIES:
                    logger.warning("dhan %s rate limited, retry %d", family, attempt + 1)
                    self._sleep(pacing * (2 ** attempt))
                    continue
                raise RuntimeError(f"dhan {url} -> HTTP {exc.code}: {body}") from exc
        raise RuntimeError(f"dhan {url} exhausted {DHAN_RETRIES} retries")

    def expiry_list(self, underlying_scrip, underlying_seg):
        payload = self._post("chain", EXPIRYLIST_URL, {"UnderlyingScrip": int(underlying_scrip), "UnderlyingSeg": underlying_seg})
        dates = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(dates, list) or not dates:
            raise RuntimeError(f"expiry list empty or not the measured shape: {json.dumps(payload)[:300]}")
        return sorted(str(d)[:10] for d in dates)

    def option_chain(self, underlying_scrip, underlying_seg, expiry):
        payload = self._post("chain", OPTIONCHAIN_URL, {"UnderlyingScrip": int(underlying_scrip),
                                                         "UnderlyingSeg": underlying_seg, "Expiry": str(expiry)[:10]})
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or not isinstance(data.get("oc"), dict) or not data["oc"]:
            raise RuntimeError(f"option chain for {expiry} is not the measured shape: {json.dumps(payload)[:300]}")
        return data

    def last_prices(self, segment, security_ids):
        """{security_id: last_price} for every id asked; a missing or non-positive price RAISES."""
        ids = sorted({str(s) for s in security_ids})
        payload = self._post("quote", MARKETFEED_OHLC_URL, {segment: [int(s) for s in ids]})
        data = payload.get("data") if isinstance(payload, dict) else None
        seg = data.get(segment) if isinstance(data, dict) else None
        if not isinstance(seg, dict):
            raise RuntimeError(f"marketfeed/ohlc is not the measured shape: {json.dumps(payload)[:300]}")
        out = {}
        for s in ids:
            px = (seg.get(s) or {}).get("last_price")
            if px is None or float(px) <= 0:
                raise RuntimeError(f"no live price for {segment} {s} in marketfeed/ohlc: {json.dumps(seg.get(s))[:200]}")
            out[s] = float(px)
        return out
