"""
Configuration for strategy-range-liquidity-sweep.

Every threshold the playbook states, in one place, with the reason it has the
value it has. The playbook is explicit that these are starting values rather
than laws - so each one is an environment override, and the ones that are NOT
from the playbook are marked as such.

NO LAYERS AND NO DATABASE. This function reads nothing and writes nothing: the
whole context arrives in the invocation payload from strategy-manager,
and what it finds goes to its own log. That is why IST lives here rather than
coming from the neon-access layer - importing that layer would pull in pg8000
for a function that never opens a connection, the same reason error-notifier
does not use it either.
"""

import datetime
import os

# --------------------------------------------------------------------------
# The payload contract
#
# The version of strategy-manager's context this function is written
# against. It ASSERTS this rather than coping with a mismatch: a context that
# has moved on would have this function reading a key that is no longer there,
# getting None, and gating on it. A gate that silently passes because its
# input vanished is the worst failure this playbook can have.
# --------------------------------------------------------------------------
EXPECTED_CONTEXT_VERSION = int(os.environ.get("EXPECTED_CONTEXT_VERSION", "1"))

# --------------------------------------------------------------------------
# IST
#
# Local, not from the neon-access layer - see the module docstring.
# --------------------------------------------------------------------------
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

SESSION_START = datetime.time(9, 15)
SESSION_END = datetime.time(15, 30)

# The opening range: bars stamped 09:15, 09:20, 09:25.
#
# THREE 5-MINUTE BARS, NEVER DHAN'S 15-MINUTE BUCKET. The playbook says this
# outright and it is not a preference: Dhan's intraday buckets are aligned
# from the session open, so its native 15-minute series and the chart the
# levels were marked on do not agree. The same definition
# intraday-market-sentiment uses for orb_high/orb_low.
ORB_END = datetime.time(9, 30)

# The first hour, for the third pool. Where it coincides with the opening
# range the two form a stacked pool, which the playbook calls the
# highest-quality kind on a range day.
FIRST_HOUR_END = datetime.time(10, 15)

# --------------------------------------------------------------------------
# The regime gate
#
# The coarse gate already happened: strategy-manager only invokes this
# function when the 15-minute regime is RANGE. This is the fine gate, and it
# exists because the manager's registry cannot know what this playbook
# needs - and because a strategy that trusts an upstream gate it cannot see
# fires on a bad day the moment that gate changes.
# --------------------------------------------------------------------------
# The playbook allows neutral range, bullish sideways and bearish sideways.
# The classification expresses exactly those three as regime RANGE with bias
# NEUTRAL, BULLISH or BEARISH, so every bias is allowed here and the regime
# does the work. Held as a set rather than assumed so a future regime value
# cannot quietly become eligible.
ALLOWED_REGIMES = frozenset(
    os.environ.get("ALLOWED_REGIMES", "RANGE").split(",")
)
ALLOWED_BIASES = frozenset(
    os.environ.get("ALLOWED_BIASES", "BULLISH,BEARISH,NEUTRAL").split(",")
)

# India VIX must not be moving. The playbook's number: within +-5%.
MAX_VIX_CHANGE_PCT = float(os.environ.get("MAX_VIX_CHANGE_PCT", "5.0"))

# How much of the day's expected range is already spent. The playbook:
# "adr (today's range so far) is below 0.9x atr14".
#
# RE-SCALED, AND THE PLAYBOOK ASKS FOR THIS EXPLICITLY. 0.9 was calibrated
# against a 10-day mean of high-low, which excludes overnight gaps. The
# manager computes Wilder TRUE ATR14, which includes them and is
# therefore a LARGER number for the same market - so the same 0.9 would be a
# looser gate than intended. The playbook states the equivalent as "roughly
# 0.78" and instructs confirming which definition atr14 holds before trusting
# the number. It holds true ATR; hence 0.78.
MAX_RANGE_USED_VS_ATR = float(os.environ.get("MAX_RANGE_USED_VS_ATR", "0.78"))

# The opening range must be wide enough to pay a 1:2. The playbook: at least
# 0.15% of price, about 36 points on Nifty at 24,000.
MIN_ORB_RANGE_PCT = float(os.environ.get("MIN_ORB_RANGE_PCT", "0.15"))

# --------------------------------------------------------------------------
# The sweep band
#
# A wick beyond the level by this much is a sweep. Less is noise inside the
# level; more is a breakout wearing a sweep's shape.
#
# ---------------------------------------------------------------------------
# A KNOWN DISAGREEMENT INSIDE THE PLAYBOOK, LEFT VISIBLE RATHER THAN SPLIT.
#
# The rule says 0.04%-0.30%, and the prose explains the floor as "below 10 pts
# it is noise inside the level" - 0.04% of 24,000 is 9.6 points, so rule and
# prose agree.
#
# Worked Example 1 does not. Its sweep is 8.2 points beyond the level, which
# it states as 0.034%, and labels "inside the sweep band". At 0.034% it is
# below the 0.04% floor, and at 8.2 points it is below the 10-point floor the
# prose gives - so by both statements of the rule that example is too shallow.
#
# The explicit threshold wins here, because it is the rule stated twice and
# the example is a label on a number that contradicts it. The consequence is
# deliberate and worth knowing: run against Example 1's session this function
# reports that sweep as a REJECTED candidate with reason "too shallow"
# (0.034% against a 0.040% floor) rather than as a candidate. The playbook
# requires rejections be reported with their reason for exactly this purpose -
# calibrating the thresholds over time - so the disagreement surfaces in the
# output on its own instead of being resolved by guesswork here.
#
# Lower MIN_PENETRATION_PCT to 0.03 to make Example 1 qualify.
# ---------------------------------------------------------------------------
MIN_PENETRATION_PCT = float(os.environ.get("MIN_PENETRATION_PCT", "0.04"))
MAX_PENETRATION_PCT = float(os.environ.get("MAX_PENETRATION_PCT", "0.30"))

# --------------------------------------------------------------------------
# Pools
# --------------------------------------------------------------------------
# How close two pools must sit to count as stacked. NOT FROM THE PLAYBOOK,
# which names stacked pools repeatedly but never says how near is near.
#
# SET FROM THE PLAYBOOK'S OWN WORKED EXAMPLE rather than invented: Example 2
# stacks the 15-minute low at 24,106.00 with the previous day low at
# 24,091.50, 14.50 points apart, which is 0.060% of price. The threshold has
# to be above that for the example to be the stacked sweep the playbook calls
# it, so 0.08% clears it with margin without reaching for unrelated levels.
#
# It is the least-evidenced number here and the first that should be
# re-measured against real sessions.
STACKED_POOL_PCT = float(os.environ.get("STACKED_POOL_PCT", "0.08"))

# --------------------------------------------------------------------------
# The stop
#
# Beyond the sweep extreme plus a buffer, where the buffer is the LARGER of a
# fixed fraction of price and half the recent 5-minute range. The playbook is
# blunt about why: placing the stop at the sweep extreme with no buffer is the
# most common way this setup gets stopped and then works.
# --------------------------------------------------------------------------
STOP_BUFFER_PCT = float(os.environ.get("STOP_BUFFER_PCT", "0.03"))
STOP_BUFFER_ATR_FACTOR = float(os.environ.get("STOP_BUFFER_ATR_FACTOR", "0.5"))
STOP_BUFFER_LOOKBACK_BARS = int(os.environ.get("STOP_BUFFER_LOOKBACK_BARS", "6"))

# --------------------------------------------------------------------------
# Risk-reward
#
# Below this the candidate is rejected outright. The playbook: "Reject the
# candidate if (T2 - entry) / (entry - stop) < 1.5." It is also the brake on
# re-entries, where a wider stop against an unchanged target usually fails it -
# which the playbook describes as the arithmetic quietly saying the edge is
# gone.
# --------------------------------------------------------------------------
MIN_RISK_REWARD = float(os.environ.get("MIN_RISK_REWARD", "1.5"))

# --------------------------------------------------------------------------
# Time filters
#
# 09:15-09:45  the range is still forming, no scanning
# 09:45-14:30  prime window
# 14:30-15:00  stacked pool and a near target only
# after 15:00  no new entries
# --------------------------------------------------------------------------
SCAN_START = datetime.time(9, 45)
PRIME_END = datetime.time(14, 30)
LATE_END = datetime.time(15, 0)

# --------------------------------------------------------------------------
# Re-entry
#
# One re-entry per side per session, and the playbook is explicit that a
# second failure closes that side for the day "regardless of how the candles
# look. There is no third attempt that is not just wanting it to work."
# --------------------------------------------------------------------------
MAX_ATTEMPTS_PER_SIDE = int(os.environ.get("MAX_ATTEMPTS_PER_SIDE", "2"))

# Two consecutive 5-minute closes beyond the level is acceptance - Case A, the
# range broke, and that side is dead for the session along with the other one.
ACCEPTANCE_CLOSES = int(os.environ.get("ACCEPTANCE_CLOSES", "2"))

# Case C: filled, never reached T1, and roughly this many bars later price is
# back around entry with the range intact. The idea has expired rather than
# failed.
NO_FOLLOW_THROUGH_BARS = int(os.environ.get("NO_FOLLOW_THROUGH_BARS", "8"))

# How near "back around the entry" is, as a fraction of the trade's own risk.
# NOT FROM THE PLAYBOOK, which says "back around the entry" without a number.
# Expressed in units of risk rather than points so it scales with the setup.
NO_FOLLOW_THROUGH_RISK_FRACTION = float(
    os.environ.get("NO_FOLLOW_THROUGH_RISK_FRACTION", "0.5")
)
