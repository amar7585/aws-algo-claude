"""
market-classifier tunables and the chain wiring.

The scoring RULES are the market-classifier LAYER's, shared unchanged with
daily-market-sentiment. These are only the per-function inputs to them - the
expected-move and session-elapsed maths and the buildup epsilon - plus the name
of the function this one invokes.
"""

import datetime
import os

# --------------------------------------------------------------------------
# Session clock (IST). SESSION_START is the market open, NOT a run time.
# --------------------------------------------------------------------------
SESSION_START = datetime.time(9, 15)
# The trading session is 09:15-15:30 = 375 minutes. The range-expansion test
# scales the expected move by sqrt(elapsed / SESSION_MINUTES) so it means the
# same thing at 09:45 as at 15:15.
SESSION_MINUTES = 375

# --------------------------------------------------------------------------
# Volatility inputs
#
# The VIX-implied expected move: price * (vix/100) / sqrt(TRADING_DAYS) * K.
# The SAME formula and K daily-market-sentiment uses, so "volatile-expansion"
# means one thing across both frames.
# --------------------------------------------------------------------------
EXPECTED_MOVE_K = float(os.environ.get("EXPECTED_MOVE_K", "1.0"))
TRADING_DAYS_PER_YEAR = 252

# --------------------------------------------------------------------------
# buildup
#
# The move below which the buildup label treats a change as no change, in
# percent. 0.0 = pure sign: any tick either way counts. Moved here with buildup
# from intraday-market-sentiment; still a tunable to be set from measurement
# later, not a constant.
# --------------------------------------------------------------------------
BUILDUP_EPSILON_PCT = float(os.environ.get("BUILDUP_EPSILON_PCT", "0.0"))

# --------------------------------------------------------------------------
# The pattern-detector chain
#
# Once the judgement row is written this function invokes pattern-detector with
# it. UNSET MEANS OFF: with no name configured nothing is dispatched and a log
# line says so, so this ships with no behavioural change and the chain is
# switched on by setting one variable once pattern-detector exists. Setting it
# needs one IAM change - this function's execution role must allow
# lambda:InvokeFunction on the detector's ARN, and NOT on a wildcard.
# --------------------------------------------------------------------------
PATTERN_DETECTOR_FUNCTION_NAME = os.environ.get("PATTERN_DETECTOR_FUNCTION_NAME", "")
PATTERN_DETECTOR_INVOCATION_TYPE = os.environ.get(
    "PATTERN_DETECTOR_INVOCATION_TYPE", "Event"
)
