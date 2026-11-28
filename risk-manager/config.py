"""
risk-manager configuration.

THE RUN BLOCK IS AN INTERFACE. strategy-manager (context v4) and both playbooks read the `run`
block this function adds; RUN_VERSION names its shape and every reader asserts it.
"""

import datetime
import os

RUN_VERSION = 1
ORDER_MANAGER_CONTRACT_VERSION = 1

CONTROL_TABLE = "algo.risk_control"
POSITION_TABLE = "algo.position_master"
HOLIDAY_TABLE = "algo.trading_holiday"

# Where the decision goes (asynchronous), and the one writer of positions (synchronous - used only
# for the kill-switch closes this function makes itself). Both required.
STRATEGY_MANAGER_FUNCTION = os.environ.get("STRATEGY_MANAGER_FUNCTION", "strategy-manager").strip()
ORDER_MANAGER_FUNCTION = os.environ.get("ORDER_MANAGER_FUNCTION", "order-manager").strip()

# Which playbook runs on what. TICK playbooks run on every tick (the sweep: it re-derives the session
# and follows its trades itself). ALERT playbooks enter on an alert and are invoked on a tick only while
# they hold a position (trend: its exit mode). strategy-manager keeps its own allow-lists as well.
_list = lambda v: [x.strip() for x in os.environ.get(v, "").split(",") if x.strip()]  # noqa: E731
TICK_PLAYBOOKS = _list("TICK_PLAYBOOKS") or ["strategy-range-liquidity-sweep"]
ALERT_PLAYBOOKS = _list("ALERT_PLAYBOOKS") or ["strategy-trend-pullback"]

TICK_SOURCE = "pattern-recorder-tick"
ALERT_SOURCE = "pattern-recorder"

# THE HARD RULES (Amar 2026-11-20 / 11-22) - owned here, for every playbook:
#   no new trade on a candle that closes at or after 15:00 (the sweep's own window boundary);
#   the time exit on the 15:10 candle - its close is the 15:15 price; from 15:15 the closing auction
#   freezes the index (since 2026-08-03) and its 15:25 candle is the official-close jump.
NO_NEW_TRADE_FROM_CLOSE = datetime.time(15, 0)
TIME_EXIT_CANDLE = datetime.time(15, 10)
# A session's close, for the kill switch's "resets each morning": a row set after the previous
# session's close is on for the next session.
SESSION_CLOSE = datetime.time(15, 30)

INVOCATION_TYPE = os.environ.get("INVOCATION_TYPE", "Event")
MAX_PAYLOAD_BYTES = 256 * 1024

# THE DAILY-LOSS LIMIT IN OPTION RUPEES (Amar 2026-11-28: "Option rupees", "₹20,000"; build plan approved). The day's
# loss is read from the paper option record order-manager writes - cash so far (received - paid - estimated charges)
# + every open option marked at its LIVE price (one batched Dhan quote, only while an option is open) - and compared
# with risk_control.daily_loss_limit_rupees. It REPLACES the point-lot check.
OPTION_TABLE = "algo.option_paper_position"
TOKEN_PARAMETER_NAME = os.environ.get("TOKEN_PARAMETER_NAME", "/algo/dhan/token")
MARKETFEED_OHLC_URL = os.environ.get("DHAN_MARKETFEED_OHLC_URL", "https://api.dhan.co/v2/marketfeed/ohlc")
CHAIN_PACING_SECONDS = 3.1      # unused here (no option-chain call) - dhan.py is shared in shape with order-manager's
QUOTE_PACING_SECONDS = float(os.environ.get("DHAN_QUOTE_PACING_SECONDS", "1.1"))
DHAN_RETRIES = int(os.environ.get("DHAN_RETRIES", "3"))
DHAN_TIMEOUT_SECONDS = float(os.environ.get("DHAN_TIMEOUT_SECONDS", "6"))
