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

# ---- PAPER TRADING ON THE OPTION (Amar 2026-11-28: "Paper on the option"; build plan approved) ----------------
# OPTION_PAPER "off" (the default) = exactly the behaviour before: no Dhan read, no option row, no extra line - the
# rollback. "on" = every step that changes quantity also buys / sells the option 1 strike in the money (a CE one
# strike below ATM for a long, a PE one strike above for a short) at Dhan's LIVE price when the step is applied,
# in the same transaction, and the trade message gets one extra option line.
OPTION_PAPER = os.environ.get("OPTION_PAPER", "off").strip().lower() in ("1", "on", "true", "yes")
# Positions entered BEFORE this epoch (the cut-over) carry no option row and are left alone; from it on, a
# position with no option row RAISES (its option entry was lost). Set at deploy to the next session's midnight.
OPTION_PAPER_FROM = int(os.environ.get("OPTION_PAPER_FROM", "0"))
OPTION_TABLE = "algo.option_paper_position"
OPTION_DEPTH_ITM = 1            # Amar 2026-11-28: "1 ITM"
STRIKE_STEP = 50                # NIFTY strikes
OPTION_SEGMENT = "NSE_FNO"      # raw Dhan code

# Dhan DATA reads only (no order API, no static IP). The token is auth-dhan-broker's, read from SSM.
TOKEN_PARAMETER_NAME = os.environ.get("TOKEN_PARAMETER_NAME", "/algo/dhan/token")
EXPIRYLIST_URL = os.environ.get("DHAN_EXPIRYLIST_URL", "https://api.dhan.co/v2/optionchain/expirylist")
OPTIONCHAIN_URL = os.environ.get("DHAN_OPTIONCHAIN_URL", "https://api.dhan.co/v2/optionchain")
MARKETFEED_OHLC_URL = os.environ.get("DHAN_MARKETFEED_OHLC_URL", "https://api.dhan.co/v2/marketfeed/ohlc")
# Dhan's limits: the option-chain family 1 request per 3 s, quotes 1 per second. Paced per family inside one call;
# a DH-904 / 429 (another function used the allowance) is retried with backoff.
CHAIN_PACING_SECONDS = float(os.environ.get("DHAN_CHAIN_PACING_SECONDS", "3.1"))
QUOTE_PACING_SECONDS = float(os.environ.get("DHAN_QUOTE_PACING_SECONDS", "1.1"))
DHAN_RETRIES = int(os.environ.get("DHAN_RETRIES", "3"))
DHAN_TIMEOUT_SECONDS = float(os.environ.get("DHAN_TIMEOUT_SECONDS", "6"))

# Estimated charges for a BOUGHT option, per order. FROM MEMORY (2024-25 rates) - NOT VERIFIED against Dhan's
# charge sheet; the ₹20,000 daily-loss check counts them (as replayed, ~₹100 a trade).
OPTION_CHARGES = {"brokerage": "20", "stt_sell": "0.001", "exchange": "0.0003503", "sebi": "0.000001",
                  "stamp_buy": "0.00003", "gst": "0.18"}
