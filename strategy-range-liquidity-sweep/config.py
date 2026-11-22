"""
Configuration for strategy-range-liquidity-sweep.

Every threshold the playbook states, in one place, with the reason it has the
value it has. The playbook is explicit that these are starting values rather
than laws - so each one is an environment override, and the ones that are NOT
from the playbook are marked as such.

IT READS, AND WRITES ONE RECORD. The snapshot and daily rows arrive in the
invocation payload from strategy-manager, which is a pure router; on every-tick
runs (since 2026-11-14) so do today's closed candles, and the 15-min rows and
the daily series it reads from Neon itself. What it finds goes to its log and
to Telegram, and - when REGISTER_ENABLED is on - its alert register goes to
algo.playbook_alerts (register.py), a record nothing reads back. No signal
table, no order.

There is no Dhan client here and no token. The only thing the manager ever
called Dhan for was a live price, and this playbook never read it: a sweep is
confirmed by a CLOSED bar reclaiming a level, so an unconfirmed live tick is
exactly what the setup must not act on.
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
EXPECTED_CONTEXT_VERSION = int(os.environ.get("EXPECTED_CONTEXT_VERSION", "3"))

# --------------------------------------------------------------------------
# IST
#
# STILL LOCAL, BUT NO LONGER BECAUSE IT HAS TO BE. The original reason was that
# this function carried no layers at all. It now carries neon-access, which
# exports the same constant, so the two exist side by side and agree by
# definition - both are UTC+05:30, which is fixed and has no daylight rule.
#
# Kept local so that levels.py, sweep.py and trade.py stay pure over plain
# dicts and can be exercised with no layer on the path; the handler, which
# already needs the layer for connect(), uses the layer's helpers.
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
# Since 2026-11-14 every 5-min tick arrives here whatever the regime, so this
# fine gate - with the cell in session.py - is the only gate. It exists because
# a strategy that trusts an upstream gate it cannot see fires on a bad day the
# moment that gate changes.
# --------------------------------------------------------------------------
# NO BIAS (Amar 2026-11-14): "sweep is not part of any bias - it can happen
# within levels, not based on bias; it is mostly related to volatility." The
# playbook's neutral range / bullish sideways / bearish sideways is regime
# `sideways` with any bias, so only the regime is checked. Measured before the
# change on 41 sessions as live: taking the bias half out of the entry cell
# added 5 entries and removed none (new rules +1.04R -> +1.62R, full losses
# 1 -> 1).
#
# WITHIN LEVELS, NOT HIGH VOLATILITY (Amar 2026-11-15): "if price is within
# levels then why it matters - depends on high volatility." So the regime is
# checked for VOLATILITY only: `sideways` and `trending` both pass,
# `volatile-expansion` (the classifier's high-volatility read: the last 30 min
# >= 2x normal, or the VIX move) does not. Trending no longer blocks, and the
# day trend is no longer part of the gate (session.py checks price within the
# levels instead). Replayed before the change on 41 sessions as live: +4
# entries (10-01 short, 10-30 long, each under both rule sets - all reached T2;
# 10-30 is Amar's logged trade), none removed; new rules +1.62R -> +4.40R, full
# losses 1 -> 1. Held as an allow-list so a future regime value cannot quietly
# become eligible.
#
# THE VALUES ARE LOWER CASE NOW, and that is the taxonomy change rather than a
# style choice: the old pair was RANGE/TREND with bias NEUTRAL, and the shared
# layer emits sideways/trending/volatile-expansion with bias range-bound.
# Comparing against the old strings would fail every check silently-ish - the
# gate would stand down on every session and look merely cautious.
ALLOWED_REGIMES = frozenset(
    os.environ.get("ALLOWED_REGIMES", "sideways,trending").split(",")
)

# --------------------------------------------------------------------------
# The data this playbook fetches for itself
#
# strategy-manager is a pure router: it hands over the two sentiment rows and
# nothing else. A playbook knows which bars it needs, so it reads them - the
# manager used to fetch a fixed window on every playbook's behalf and guess at
# the size.
#
# IT READS NEON, NOT DHAN. The only thing the manager ever called Dhan for was
# a live price, and this playbook never read it: a sweep is confirmed by a
# CLOSED bar reclaiming a level, so an unconfirmed live tick is precisely the
# thing the setup must not act on. Dropping that call removed this function's
# need for a Dhan token, an SSM read and a rate limit budget entirely.
#
# A STALE CANDLE COSTS ONE SCAN, NOT A WRONG ROW. This function persists
# nothing, so reading intraday-data-loader's tables is safe here in a way it
# is not in intraday-market-sentiment, where a stale input would be written
# into a snapshot row that is never revisited.
# --------------------------------------------------------------------------
CANDLE_INTERVAL_MINUTES = int(os.environ.get("CANDLE_INTERVAL_MINUTES", "5"))
CANDLE_INTERVAL_SECONDS = CANDLE_INTERVAL_MINUTES * 60

# One session is 75 five-minute bars. 120 covers a full session with headroom
# and is trimmed to the session day before anything is scanned.
HISTORY_5MIN_BARS = int(os.environ.get("HISTORY_5MIN_BARS", "120"))

# Wilder ATR14 over daily bars needs 15 closes for its first value; 40 gives
# the average time to settle.
HISTORY_DAILY_BARS = int(os.environ.get("HISTORY_DAILY_BARS", "40"))
ATR_PERIOD = int(os.environ.get("ATR_PERIOD", "14"))

# India VIX must not be moving. The playbook's number: within +-5%.
MAX_VIX_CHANGE_PCT = float(os.environ.get("MAX_VIX_CHANGE_PCT", "5.0"))

# How much of the day's expected range is already spent. The playbook:
# "adr (today's range so far) is below 0.9x atr14".
#
# RE-SCALED, AND THE PLAYBOOK ASKS FOR THIS EXPLICITLY. 0.9 was calibrated
# against a 10-day mean of high-low, which excludes overnight gaps. The
# handler computes Wilder TRUE ATR14 over candle_daily, which includes them
# and is therefore a LARGER number for the same market - so the same 0.9 would be a
# looser gate than intended. The playbook states the equivalent as "roughly
# 0.78" and instructs confirming which definition atr14 holds before trusting
# the number. It holds true ATR - see handler.lambda_handler; hence 0.78.
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
# T1 - booking part of the trade at the opening-range midpoint (Amar 2026-11-22,
# "2 of 3")
#
# Enter 3 lots; when the range midpoint trades, book 2 of them there; the last
# lot runs to T2 on the same stop (manage.follow). A decision, NOT a tunable (no
# env override). The split was replayed before it was chosen (110 sessions,
# deployed code TRtj+AW8, stop never moved) - the total falls in a straight line
# as the booked share grows, and the win count rises:
#     booked at the midpoint   gate as live (6 trades)   gate off (24 trades)
#     none (all to T2)         +6.85R, 5 won             +10.48R, 10 won
#     1 of 4                   +6.19R, 6 won             +9.61R, 11 won
#     1 of 3                   +5.98R, 6 won             +9.32R, 12 won
#     1 of 2                   +5.54R, 6 won             +8.73R, 13 won
#     2 of 3 (this)            +5.10R, 6 won             +8.15R, 14 won
#     all                      +4.23R, 6 won             +6.98R, 14 won
# Amar chose 2 of 3 for the win rate. The function sizes nothing: the numbers
# are only said in its messages.
# --------------------------------------------------------------------------
LOTS = 3
T1_LOTS_BOOKED = 2

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
# The closing auction - a fact about the market, NOT a tunable (no env
# override). Since 2026-08-03 NSE's closing auction stops continuous trading
# in the NIFTY stocks at 15:15, so the index's 15:15 and 15:20 candles are
# flat lines (median range 0.00 on 76 of 76 sessions to 2026-11-20) and the
# 15:25 candle is one jump to the official close (median 24.9 pts, max 201).
# Nobody trades at that jump, and the future mostly does not follow it
# (correlation 0.57 with the index's 15:10->15:25 move, against 0.75 before
# 08-03). So the 15:10 candle - its close is the 15:15 price - is the last
# candle a trade is judged on: one still in after it is closed at its close,
# out at 15:15 by the clock (Amar 2026-11-20). Replayed on 110 sessions it
# changes 2 of 24 trades; see manage.follow and the README.
# --------------------------------------------------------------------------
LAST_LIVE_INDEX_BAR = datetime.time(15, 10)

# --------------------------------------------------------------------------
# Telegram
#
# A qualifying candidate is a real trade signal, and the signal is pushed to
# Telegram so it reaches the phone the moment it is generated - "definitely send
# on signal generation". It fires ONLY on a LIVE candidate whose trigger bar is
# FRESH (closed within the last snapshot interval), so a signal that persists
# across several 15-minute runs is announced once, not on every re-derivation.
# Rejections and stand-downs stay in the log; the chat gets signals only.
#
# The same parameter the daily brief uses. This function already reads
# /algo/neon/connection, so its role has an SSM-read policy - it needs that
# policy widened to this one extra parameter plus kms:Decrypt, its own grant.
# --------------------------------------------------------------------------
TELEGRAM_PARAMETER_NAME = os.environ.get("TELEGRAM_PARAMETER_NAME", "/algo/telegram/brief")
HTTP_TIMEOUT_SECONDS = float(os.environ.get("HTTP_TIMEOUT_SECONDS", "8"))

# A push of this many consecutive 5-minute closes beyond a level is judged
# structurally at its close back inside (sweep.push_sweeps): a sweep, or a break.
ACCEPTANCE_CLOSES = int(os.environ.get("ACCEPTANCE_CLOSES", "2"))

# --------------------------------------------------------------------------
# Every-tick runs and the trade after entry (2026-11-14)
#
# Since 2026-11-14 pattern-recorder hands EVERY 5-min tick to strategy-manager,
# and the manager sends each tick here whatever the regime, so a trade that is
# open keeps being watched even after the day's context changes. New entries
# (and re-entries) are taken only when price is within the levels and the
# regime is not volatile-expansion - no bias, no day trend (Amar 2026-11-14 /
# 11-15; see ALLOWED_REGIMES above and session.context_at).
#
# THE RULES: first-candle detection plus a 2+ close push judged structurally at
# its close back inside, with one more look on the next candle (2026-11-16,
# sweep.push_sweeps), one live idea per side, and the exit / watch / re-entry
# rule (manage.py). The older rule set that ran beside them (2 closes beyond =
# range broke, 2 attempts per side) and the message tags naming which set
# produced each message were removed 2026-11-19 (Amar).
# --------------------------------------------------------------------------
# When a 15-min row counts as in force for a candle: written (created_at) by the
# candle's close plus this - pattern-recorder runs about 100 s after the close.
ROW_VISIBLE_LAG_SECONDS = int(os.environ.get("ROW_VISIBLE_LAG_SECONDS", "100"))

# The source tag of an every-tick payload (pattern-recorder sets it).
TICK_SOURCE = "pattern-recorder-tick"

# Telegram stays on, as today (unset = on). "off" stops every send (logged as
# WOULD SEND) without a redeploy - a kill switch, not a shadow mode.
TELEGRAM_ENABLED = os.environ.get("TELEGRAM_ENABLED", "on").strip().lower() not in ("0", "off", "false", "no")

# --------------------------------------------------------------------------
# The alert register (Amar 2026-11-14, "re-derive and record") - register.py
#
# Every run rebuilds today's alerts - each level touched, its retests, and
# whether it was entered, blocked by the gate, missed, invalid, ended or covered
# - and writes them to algo.playbook_alerts AFTER the messages are sent, on the
# connection the reads already opened. Nothing is read back: the next run
# rewrites the day. A failed write raises (Lambda's automatic retry may then
# resend that run's messages - preferred over turning retries off, which would
# lose a signal for good).
#
# UNSET = OFF, so the code can be deployed before the table is relied on, and
# turned off again without a redeploy. "on" to write.
# --------------------------------------------------------------------------
REGISTER_ENABLED = os.environ.get("REGISTER_ENABLED", "").strip().lower() in ("1", "on", "true", "yes")
REGISTER_TABLE = "algo.playbook_alerts"
# This playbook's name in the register's `playbook` column - the function name.
REGISTER_PLAYBOOK = "strategy-range-liquidity-sweep"
