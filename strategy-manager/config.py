"""
Configuration for strategy-manager.

A PURE ROUTER. This function reads nothing, computes nothing and writes
nothing - so almost everything that used to be here is gone: the Dhan
endpoints, the instrument identity, the candle history windows, the SMA and
RSI periods, the classification thresholds. All of it moved, and the two
places it moved to are the point of the change:

  * THE CLASSIFICATION moved UP, into the market-classifier layer, and its
    result is now written onto algo.intraday_market_sentiment by
    intraday-market-sentiment. The manager used to recompute a 15-minute
    classification of its own, which meant the rules lived in two places and
    the regime a strategy acted on was never stored anywhere.
  * THE DATA FETCH moved DOWN, into the playbooks. A playbook knows what bars
    it needs; the manager was fetching a fixed window on their behalf and
    guessing.

What is left is the decision "which playbooks are valid for a market that
looks like this", and the invoke.
"""

import os

# --------------------------------------------------------------------------
# The payload contract
#
# THIS NUMBER IS AN INTERFACE. Every strategy is written against a specific
# context shape, so an unannounced change is a silent mismatch: the strategy
# reads a key that is no longer there, gets None, and gates on it.
#
# v3 (2026-11-19) - bias, score, max_score and confidence are gone from the
#   snapshot and the daily read: Amar removed bias system-wide. Nothing else.
# v2 - the shape changed substantially from v1:
#   * `classification` is gone as a separate block. The classification is now
#     ON the snapshot (structure, regime, volatility, swing_*; bias, score,
#     max_score and confidence until v3), because the sentiment function
#     computes and stores it.
#   * `candles`, `live` and `daily_atr14` are gone. Playbooks fetch their own.
#   * `daily` is still here and may be None.
#   * sma20 no longer exists anywhere; the frame carries sma9.
#
# Bump it for ANY change to the dispatched payload. Strategies assert the
# version they were written for and raise on anything else, so a bump is loud
# on both sides.
# --------------------------------------------------------------------------
CONTEXT_VERSION = 3

# ---- alerts route on day_trend (Amar 2026-11-09; registry removed 2026-11-19) ------
# A v2 alert arriving with day_trend up/down goes to the TREND playbooks below. Any
# other alert routes nowhere: the range playbook (the sweep) runs on every tick
# through TICK_PLAYBOOKS, whatever the alert. Until 2026-11-19 a regime|bias
# registry routed those other alerts; its only filled cell (sideways|range-bound
# -> the sweep) was always left out as a tick playbook, so it dispatched nothing.
# Amar removed bias system-wide on 2026-11-19 and the registry went with it.
#
# Comma-separated function names. UNSET = OFF: trend-day alerts route to nothing,
# logged as a deliberate stand-down. Setting it needs lambda:InvokeFunction on
# each named ARN in this role's invoke-playbooks policy.
TREND_PLAYBOOKS = [x.strip() for x in os.environ.get("TREND_PLAYBOOKS", "").split(",") if x.strip()]

# Asynchronous, and the choice is load-bearing. This function returning is not
# a claim that any strategy succeeded - each has its own log group and
# error-notifier reports its failures directly. A synchronous invoke would put
# every strategy's runtime inside this function's timeout and let one slow
# playbook delay the rest.
# EVERY-TICK PLAYBOOKS (Amar 2026-11-14). pattern-recorder hands every 5-min
# tick here (source TICK_SOURCE) so a playbook can follow a trade candle by
# candle. Those ticks go to these playbooks WHATEVER the regime - a trade that
# is open must keep being watched after the day's context changes - and each
# applies its own entry cell. The same playbooks are left out of alert routing,
# so a tick with an alert does not run them twice. Unset = no tick routing.
TICK_PLAYBOOKS = [x.strip() for x in os.environ.get("TICK_PLAYBOOKS", "").split(",") if x.strip()]
TICK_SOURCE = "pattern-recorder-tick"

INVOCATION_TYPE = os.environ.get("INVOCATION_TYPE", "Event")

# Lambda caps an asynchronous invocation payload at 256 KB. The v2 context is
# two database rows and an instrument - far smaller than v1, which carried 200
# bars of OHLCV and measured 8 KB on a real session. Checked before every
# invoke all the same, because exceeding it raises a boto3 error that names
# bytes and not the reason.
MAX_PAYLOAD_BYTES = int(os.environ.get("MAX_PAYLOAD_BYTES", str(256 * 1024)))
