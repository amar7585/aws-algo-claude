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

import json
import os

# --------------------------------------------------------------------------
# The payload contract
#
# THIS NUMBER IS AN INTERFACE. Every strategy is written against a specific
# context shape, so an unannounced change is a silent mismatch: the strategy
# reads a key that is no longer there, gets None, and gates on it.
#
# v2 - the shape changed substantially from v1:
#   * `classification` is gone as a separate block. The classification is now
#     ON the snapshot (bias, structure, regime, volatility, score, max_score,
#     confidence, sma9/50/100/200, rsi, swing_*), because the sentiment
#     function computes and stores it.
#   * `candles`, `live` and `daily_atr14` are gone. Playbooks fetch their own.
#   * `daily` is still here and may be None.
#   * sma20 no longer exists anywhere; the frame carries sma9.
#
# Bump it for ANY change to the dispatched payload. Strategies assert the
# version they were written for and raise on anything else, so a bump is loud
# on both sides.
# --------------------------------------------------------------------------
CONTEXT_VERSION = 2

# --------------------------------------------------------------------------
# The registry
#
# ROUTING IS ON A COMBINATION, NOT ON regime ALONE. "sideways" says the swing
# read found no progression; it does not say whether the market is leaning up,
# leaning down, or genuinely balanced - and a playbook that is right on a
# balanced day can be wrong on a sideways day with a bearish lean. The key is
# therefore "<regime>|<bias>".
#
# EVERY COMBINATION IS LISTED, INCLUDING THE EMPTY ONES. A key that is present
# and maps to [] is a deliberate "no playbook is valid on this kind of day". A
# key that is MISSING means the classifier and this map have drifted apart -
# a new regime or bias value appeared and nobody told the router - and that
# raises, because routing nothing would otherwise look exactly like a correct
# stand-down.
#
# ONLY ONE CELL IS FILLED, AND IT IS A PLACEHOLDER. Which combinations should
# run which playbook is a decision to take against observed sessions, not one
# to infer from the taxonomy - and there are no observed sessions yet. See
# layers/market-classifier/README.md, "Revisit once sessions have
# accumulated".
#
# Held as JSON in the environment so filling a cell is a configuration change
# rather than a redeployment.
# --------------------------------------------------------------------------
_DEFAULT_REGISTRY = {
    # range-liquidity-sweep is a range-day playbook: it fades a level that was
    # swept and rejected, which is what a balanced market does to its own
    # extremes. The identical pattern on a trending day is a breakout retest,
    # and fading it is how the setup loses - so the trending cells are empty
    # rather than absent.
    "sideways|range-bound": ["strategy-range-liquidity-sweep"],
    "sideways|bullish": [],
    "sideways|bearish": [],

    "trending|bullish": [],
    "trending|bearish": [],
    "trending|range-bound": [],

    # An expanding range is the case where the structure read is least
    # trustworthy - it is the thing being disrupted. Nothing routes here until
    # there is evidence about what does work on such a day.
    "volatile-expansion|bullish": [],
    "volatile-expansion|bearish": [],
    "volatile-expansion|range-bound": [],
}

STRATEGY_REGISTRY = json.loads(
    os.environ.get("STRATEGY_REGISTRY", json.dumps(_DEFAULT_REGISTRY))
)

# Asynchronous, and the choice is load-bearing. This function returning is not
# a claim that any strategy succeeded - each has its own log group and
# error-notifier reports its failures directly. A synchronous invoke would put
# every strategy's runtime inside this function's timeout and let one slow
# playbook delay the rest.
INVOCATION_TYPE = os.environ.get("INVOCATION_TYPE", "Event")

# Lambda caps an asynchronous invocation payload at 256 KB. The v2 context is
# two database rows and an instrument - far smaller than v1, which carried 200
# bars of OHLCV and measured 8 KB on a real session. Checked before every
# invoke all the same, because exceeding it raises a boto3 error that names
# bytes and not the reason.
MAX_PAYLOAD_BYTES = int(os.environ.get("MAX_PAYLOAD_BYTES", str(256 * 1024)))
