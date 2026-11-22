"""
order-manager configuration.

THE CONTRACT VERSION IS AN INTERFACE. Every caller (the playbooks from P2a, the
risk manager from P2b) sends `contract_version`; this function raises on any
other value, so a change to the signal shape is loud on both sides - the same
rule as strategy-manager's context_version.
"""

import os

CONTRACT_VERSION = 1

# The one table this function writes. ONLY order-manager writes it.
POSITION_TABLE = "algo.position_master"
INSTRUMENT_TABLE = "algo.instrument_master"
UNIQUE_CONSTRAINT = "position_master_one_per_entry"

# Paper mode only until the Dhan phase (P5) adds 'LIVE' with its own migration.
TRADE_MODE = "PAPER"
BROKER_ID = "paper"
PRODUCT_TYPE = "INTRADAY"

# UNITS (Amar 2026-11-22, "Units now"): quantities are lots x lot size. The paper
# position is on the NIFTY index price, whose own lot_units is 1, so the lot size
# is the near-month NIFTY future's lot_units, found by its symbol
# <underlying>-<MON><YEAR>-FUT (the session's month, else the next month).
LOT_SIZE_INSTRUMENT_TYPE = "FUTIDX"
LOT_SIZE_EXCHANGE_SEGMENT = "NSE_FNO"

# Telegram: on by default. "off" logs WOULD SEND instead - nothing calls this
# function until P2a, and in P2a the callers pass no message (record only).
TELEGRAM_ENABLED = os.environ.get("TELEGRAM_ENABLED", "on").strip().lower() not in ("0", "off", "false", "no")
TELEGRAM_PARAMETER_NAME = os.environ.get("TELEGRAM_PARAMETER_NAME", "/algo/telegram/brief")
HTTP_TIMEOUT_SECONDS = float(os.environ.get("HTTP_TIMEOUT_SECONDS", "8"))
