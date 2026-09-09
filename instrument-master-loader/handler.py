"""
Instrument Master Loader — AWS Lambda function

Downloads Dhan's public scrip master CSV, filters it down to the instrument
categories defined in INSTRUMENT_RULES (all 9 rules, ported from
trading-algo/brokers/implementations/dhan/dhan_broker.py — deliberately not
narrowed to Nifty-only), and upserts the result into algo.instrument_master
on Neon (project: AI Trader APP, database: Algo).

Trigger: EventBridge Scheduler, monthly cron. This loader sits *outside* the
History -> Regime -> Strategy state machine.

Packaging notes:
  - pg8000 is a pure-Python Postgres driver supplied by the
    layers/pg8000-driver Lambda layer, so the function needs no compiled
    wheels and no container image.
  - The scrip master is parsed with the stdlib csv module rather than pandas,
    and fetched over plain HTTP rather than through the dhanhq SDK, both of
    which would drag pandas/numpy into the deployment package.

Conventions:
  - exchange_segment is stored as the raw Dhan segment code
    (ExchangeSegment.<X>.value), never a human-readable string.
  - Every time value written to the DB is epoch seconds.
"""

import csv
import io
import json
import logging
import os
import ssl
import time
import urllib.parse
import urllib.request
from enum import Enum

import pg8000.dbapi

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SCRIP_MASTER_URL = os.environ.get(
    "DHAN_SCRIP_MASTER_URL",
    "https://images.dhan.co/api-data/api-scrip-master.csv",
)

# Rows are pushed in batches of this many to keep a single INSERT statement
# (and its parameter list) within sane limits. The filtered master is ~10^5
# rows, dominated by OPTIDX.
UPSERT_BATCH_SIZE = int(os.environ.get("UPSERT_BATCH_SIZE", "5500"))

DOWNLOAD_TIMEOUT_SECONDS = int(os.environ.get("DOWNLOAD_TIMEOUT_SECONDS", "120"))


# ---------------------------------------------------------------------------
# Exchange segment enum — values are the dhanhq SDK constants, inlined so the
# function does not have to import dhanhq (which pulls in pandas). These are
# the Dhan API v2 segment codes; keep them in sync with
# trading-algo/bin/enums/dhan.py::ExchangeSegment.
# ---------------------------------------------------------------------------
class ExchangeSegment(Enum):
    NSE = "NSE_EQ"        # dhanhq.NSE
    INDEX = "IDX_I"       # dhanhq.INDEX
    FNO = "NSE_FNO"       # dhanhq.NSE_FNO
    BSE_FNO = "BSE_FNO"   # dhanhq.BSE_FNO
    MCX = "MCX_COMM"      # dhanhq.MCX
    CUR = "NSE_CURRENCY"  # dhanhq.CUR

    @classmethod
    def resolve(cls, value):
        if value is None:
            return None
        value = value.upper()
        if value in cls.__members__:
            return cls[value]
        for member in cls:
            if member.value == value:
                return member
        return None


# ---------------------------------------------------------------------------
# Instrument rules — the full 9-rule set, copied as-is from dhan_broker.py.
#
# "exchange" narrows a rule to rows from one SEM_EXM_EXCH_ID. It is not in the
# original dict: FUTIDX and FUTIDXBSE carry byte-identical conditions and are
# distinguishable only by exchange, so without it the BSE rule can never win.
# ---------------------------------------------------------------------------
INSTRUMENT_RULES = {
    "EQUITY": {
        "exchange_segment": ExchangeSegment.NSE.value,
        "exchange": "NSE",
        "conditions": {
            "SEM_EXM_EXCH_ID": {"NSE"},
            "SEM_INSTRUMENT_NAME": {"EQUITY"},
            "SEM_EXCH_INSTRUMENT_TYPE": {"ES"},
            "SEM_SERIES": {"EQ"},
        },
    },
    "EQUITY_SYMBOLS": {
        "exchange_segment": ExchangeSegment.NSE.value,
        "exchange": "NSE",
        "conditions": {
            "SEM_INSTRUMENT_NAME": {"EQUITY"},
            "SEM_TRADING_SYMBOL": {
                "KOTAKBANK",
                "MCX",
                "CAMS",
                "BEML",
                "FCL",
                "E2E",
            },
        },
    },
    "INDEX": {
        "exchange_segment": ExchangeSegment.INDEX.value,
        "exchange": None,
        "conditions": {
            "SEM_INSTRUMENT_NAME": {"INDEX"},
            "SEM_EXCH_INSTRUMENT_TYPE": {"INDEX"},
            "SEM_SERIES": {"X"},
        },
    },
    "EQUITY_INVITU": {
        "exchange_segment": ExchangeSegment.NSE.value,
        "exchange": "NSE",
        "conditions": {
            "SEM_INSTRUMENT_NAME": {"EQUITY"},
            "SEM_EXCH_INSTRUMENT_TYPE": {"INVITU"},
            "SEM_SERIES": {"IV"},
        },
    },
    "EQUITY_REIT": {
        "exchange_segment": ExchangeSegment.NSE.value,
        "exchange": "NSE",
        "conditions": {
            "SEM_INSTRUMENT_NAME": {"EQUITY"},
            "SEM_EXCH_INSTRUMENT_TYPE": {"REIT"},
            "SEM_SERIES": {"RR"},
        },
    },
    "EQUITY_ETF": {
        "exchange_segment": ExchangeSegment.NSE.value,
        "exchange": "NSE",
        "conditions": {
            "SEM_INSTRUMENT_NAME": {"EQUITY"},
            "SEM_EXCH_INSTRUMENT_TYPE": {"ETF"},
            "SEM_SERIES": {"EQ"},
        },
    },
    "FUTIDX": {
        "exchange_segment": ExchangeSegment.FNO.value,
        "exchange": "NSE",
        "conditions": {
            "SEM_INSTRUMENT_NAME": {"FUTIDX"},
            "SEM_EXCH_INSTRUMENT_TYPE": {"FUT"},
        },
    },
    "FUTSTK": {
        "exchange_segment": ExchangeSegment.FNO.value,
        "exchange": "NSE",
        "conditions": {
            "SEM_INSTRUMENT_NAME": {"FUTSTK"},
            "SEM_EXCH_INSTRUMENT_TYPE": {"FUT"},
        },
    },
    "FUTIDXBSE": {
        "exchange_segment": ExchangeSegment.BSE_FNO.value,
        "exchange": "BSE",
        "conditions": {
            "SEM_INSTRUMENT_NAME": {"FUTIDX"},
            "SEM_EXCH_INSTRUMENT_TYPE": {"FUT"},
        },
    },
    "OPTIDX": {
        "exchange_segment": ExchangeSegment.FNO.value,
        "exchange": "NSE",
        "conditions": {
            "SEM_INSTRUMENT_NAME": {"OPTIDX"},
        },
    },
}

# Columns normalised to stripped upper-case before any rule is evaluated,
# matching the original pandas pipeline.
NORMALISED_COLUMNS = (
    "SEM_EXM_EXCH_ID",
    "SEM_INSTRUMENT_NAME",
    "SEM_EXCH_INSTRUMENT_TYPE",
    "SEM_SERIES",
    "SEM_TRADING_SYMBOL",
    "SEM_LOT_UNITS",
)


def match_rule(row):
    """Return the exchange_segment of the first rule this row satisfies, or
    None. Rule order is the declaration order of INSTRUMENT_RULES."""
    exch = row.get("SEM_EXM_EXCH_ID", "")
    for rule in INSTRUMENT_RULES.values():
        wanted_exchange = rule["exchange"]
        if wanted_exchange is not None and exch != wanted_exchange:
            continue
        if all(
            row.get(col, "") in allowed
            for col, allowed in rule["conditions"].items()
        ):
            return rule["exchange_segment"]
    return None


def parse_lot_units(val):
    if val is None or str(val).strip() == "":
        return 1
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return 1


def keep_row(row):
    """NSE only, but let SENSEX-derived BSE rows through — mirrors the
    nse_df / sensex_bse_df concat in the original loader."""
    exch = row.get("SEM_EXM_EXCH_ID", "")
    if exch.startswith("NSE"):
        return True
    return exch == "BSE" and row.get("SEM_TRADING_SYMBOL", "").startswith("SENSEX")


def download_scrip_master(url=SCRIP_MASTER_URL):
    """Fetch Dhan's public scrip master CSV. It needs no credentials, so this
    is a plain GET rather than a dhanhq.fetch_security_list() call."""
    logger.info("Downloading scrip master from %s", url)
    # An explicit User-Agent: the CDN serves the default python-urllib one
    # today, but browsers' UA is the better-trodden path through it.
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT_SECONDS) as resp:
        if resp.status != 200:
            raise RuntimeError("scrip master GET returned HTTP %s" % resp.status)
        payload = resp.read()
    logger.info("Downloaded %d bytes", len(payload))
    return payload.decode("utf-8-sig", errors="replace")


def fetch_and_filter_instruments(csv_text):
    """Apply the same filter/rename pipeline as the original
    load_instrument_master(), yielding tuples of
    (security_id, trading_symbol, exchange_segment, instrument_type, lot_units).
    """
    reader = csv.DictReader(io.StringIO(csv_text))

    missing = [c for c in NORMALISED_COLUMNS if c not in (reader.fieldnames or [])]
    if missing:
        raise RuntimeError("scrip master is missing expected columns: %s" % missing)

    rows = []
    seen = set()
    for raw in reader:
        # Cheap two-field exchange gate first: it drops ~37% of the file, and
        # normalising before it would pay the copy on every row instead of on
        # the survivors.
        if not keep_row(
            {
                "SEM_EXM_EXCH_ID": (raw.get("SEM_EXM_EXCH_ID") or "").strip().upper(),
                "SEM_TRADING_SYMBOL": (raw.get("SEM_TRADING_SYMBOL") or "")
                .strip()
                .upper(),
            }
        ):
            continue

        row = dict(raw)
        for col in NORMALISED_COLUMNS:
            value = row.get(col)
            row[col] = "" if value is None else str(value).strip().upper()

        exchange_segment = match_rule(row)
        if exchange_segment is None:
            continue

        security_id = str(raw.get("SEM_SMST_SECURITY_ID", "") or "").strip()
        if not security_id:
            continue

        instrument_type = row["SEM_INSTRUMENT_NAME"]

        # (security_id, instrument_type) is the primary key; a batch carrying
        # the same key twice makes Postgres abort the whole statement.
        key = (security_id, instrument_type)
        if key in seen:
            continue
        seen.add(key)

        rows.append(
            (
                security_id,
                row["SEM_TRADING_SYMBOL"],
                exchange_segment,
                instrument_type,
                parse_lot_units(row["SEM_LOT_UNITS"]),
            )
        )

    logger.info("Filtered scrip master down to %d instruments", len(rows))
    return rows


def connect(conn_string):
    """Open a pg8000 connection from a postgres:// URL. pg8000 takes discrete
    kwargs rather than a DSN, and Neon requires TLS."""
    parsed = urllib.parse.urlparse(conn_string)
    return pg8000.dbapi.connect(
        user=urllib.parse.unquote(parsed.username or ""),
        password=urllib.parse.unquote(parsed.password or ""),
        host=parsed.hostname,
        port=parsed.port or 5432,
        database=(parsed.path or "/").lstrip("/"),
        ssl_context=ssl.create_default_context(),
    )


UPSERT_SQL = """
    INSERT INTO algo.instrument_master
        (security_id, trading_symbol, exchange_segment, instrument_type,
         lot_units, updated_at)
    VALUES {values}
    ON CONFLICT (security_id, instrument_type) DO UPDATE SET
        trading_symbol   = EXCLUDED.trading_symbol,
        exchange_segment = EXCLUDED.exchange_segment,
        lot_units        = EXCLUDED.lot_units,
        updated_at       = EXCLUDED.updated_at
"""


def upsert_instrument_master(conn, rows, batch_size=UPSERT_BATCH_SIZE):
    """Insert/update rows into algo.instrument_master, keyed on
    (security_id, instrument_type). updated_at is epoch seconds.

    The whole load commits once: a partial instrument master is worse than a
    stale one, since downstream lookups would silently miss contracts.
    """
    if not rows:
        return 0

    now_epoch = int(time.time())
    written = 0

    cur = conn.cursor()
    try:
        for start in range(0, len(rows), batch_size):
            batch = rows[start : start + batch_size]
            values = ",".join(["(%s,%s,%s,%s,%s,%s)"] * len(batch))
            params = []
            for row in batch:
                params.extend(row)
                params.append(now_epoch)
            cur.execute(UPSERT_SQL.format(values=values), params)
            written += len(batch)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()

    return written


def lambda_handler(event, context):
    """EventBridge Scheduler entry point. Raises on failure so the invocation
    is recorded as an error and the schedule's retry policy applies."""
    neon_conn_string = os.environ["NEON_CONNECTION_STRING"]

    started = time.time()
    csv_text = download_scrip_master()
    rows = fetch_and_filter_instruments(csv_text)

    conn = None
    try:
        conn = connect(neon_conn_string)
        row_count = upsert_instrument_master(conn, rows)
    finally:
        if conn is not None:
            conn.close()

    result = {
        "status": "success",
        "rows_upserted": row_count,
        "elapsed_seconds": round(time.time() - started, 2),
    }
    logger.info("instrument_master upsert complete: %s", json.dumps(result))
    return result
