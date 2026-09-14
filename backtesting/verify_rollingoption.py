"""
One-off probe against Dhan's expired-options ("rolling option") endpoint.

This is NOT the backtest harness. Its only job is to make one real call and
print the raw response, so the request/response shape can be confirmed
against measurement rather than against secondhand summaries of the docs —
same discipline this repo's other Dhan clients follow (see
intraday-market-sentiment/dhan.py's module docstring).

Reads DHAN_ACCESS_TOKEN from backtesting/.env (gitignored, never committed).
"""

import json
import os
import ssl
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CA_BUNDLE = "/root/.ccr/ca-bundle.crt"

ROLLINGOPTION_URL = "https://api.dhan.co/v2/charts/rollingoption"

NIFTY_SECURITY_ID = "13"
NIFTY_SEG = "IDX_I"


def load_token():
    env_path = os.path.join(HERE, ".env")
    with open(env_path) as f:
        for line in f:
            if line.startswith("DHAN_ACCESS_TOKEN="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError(f"DHAN_ACCESS_TOKEN not found in {env_path}")


def ssl_context():
    if os.path.exists(CA_BUNDLE):
        return ssl.create_default_context(cafile=CA_BUNDLE)
    return ssl.create_default_context()


def post(url, payload, token):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "access-token": token,
    }
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=30, context=ssl_context()) as r:
            return r.getcode(), r.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def main():
    token = load_token()

    # Best-guess payload from secondhand documentation summaries — the whole
    # point of this script is to find out how wrong it is.
    payload = {
        "securityId": NIFTY_SECURITY_ID,
        "exchangeSegment": NIFTY_SEG,
        "instrument": "OPTIDX",
        "expiryFlag": "current",
        "expiryCode": 0,
        "strike": "ATM",
        "drvOptionType": "CE",
        "interval": 1,
        "fromDate": "2026-08-01",
        "toDate": "2026-08-30",
    }
    print("POST", ROLLINGOPTION_URL)
    print("payload:", json.dumps(payload, indent=2))

    code, body = post(ROLLINGOPTION_URL, payload, token)
    print("\nHTTP", code)
    print(body[:4000])


if __name__ == "__main__":
    main()
