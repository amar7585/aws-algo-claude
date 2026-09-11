"""
The instrument filter: exchange segments, the 9 instrument rules, and the row
predicates that apply them.

This is the part of the loader that decides what an instrument IS, ported from
trading-algo/brokers/implementations/dhan/dhan_broker.py. It is kept apart from
the download and the upsert because it is the piece with real behaviour to
argue about - the other two are plumbing.
"""

from enum import Enum


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

