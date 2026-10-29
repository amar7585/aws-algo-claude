"""
pattern-detector tunables and the strategy-manager gate.

EVERY THRESHOLD HERE IS PROVISIONAL. The two-clock turn rule was drawn from 5
hand-observed turns over 2 sessions (the observations findings); replayed, the
coded volume rule flags 2 of them (see VOLUME_Z_MIN). The cutoffs below are
starting points to be calibrated once sessions accumulate, the same way the
classifier's option thresholds are deferred. They are env-overridable so a value
can be set from measurement without a code change.
"""

import os

# The future the detector reads from candle_5min. The SAME constant the loader
# and sentiment use, so the (security_id, instrument_type) identity matches what
# was stored - candle_5min is never queried on security_id alone (hard rule #4).
FUTURES_INSTRUMENT_TYPE = os.environ.get("FUTURES_INSTRUMENT_TYPE", "FUTIDX")

# The candle grain the detector reads. A bar is CLOSED iff
# candle_ts + BAR_SECONDS <= the snapshot's captured_at - a fact about
# candle_5min, not a tunable, so it is not env-overridable.
BAR_SECONDS = 300

# ---- same-tick clock: the abnormal-volume reversal candle on the future -----
# A volume SPIKE: z >= VOLUME_Z_MIN against the preceding bars. Spikes only.
# This was two-sided (|z|) on the premise that the 2026-09-17 turns had volume
# COLLAPSE into them. Measured 2026-10-23..28 that premise does not hold: the
# coded rule flags none of the 09-17 turns, and the low side cannot reach -2 on
# most bars at all - a zero-volume bar only reaches -mean/sd, about -1.6 on the
# future. Across 2,175 futures bars (09-15..10-28) the collapse side flagged 0
# real bars against 35 spikes; its only live output was a forming-bar artifact.
VOLUME_LOOKBACK = int(os.environ.get("VOLUME_LOOKBACK", "20"))    # bars for the z baseline
VOLUME_Z_MIN = float(os.environ.get("VOLUME_Z_MIN", "2.0"))      # z that counts as a spike

# The reversal candle is looked for among the future's CLOSED 5-min bars that
# started within this window before the snapshot - the turn is up to one
# confirmation tick (~15 min) before the snapshot that confirms it.
CANDIDATE_WINDOW_SECONDS = int(os.environ.get("CANDIDATE_WINDOW_SECONDS", "1800"))

# How many of the future's recent closed 5-min bars to read (z baseline + window).
FUT_BARS_LOOKBACK = int(os.environ.get("FUT_BARS_LOOKBACK", "40"))

# ---- +1-tick clock: option-OI confirmation ---------------------------------
# The |option OI change| (percent) that confirms a turn when buildup does not.
# From the observations - PE OI -10.1% and -4.2% both confirmed lows.
OI_CONFIRM_PCT = float(os.environ.get("OI_CONFIRM_PCT", "3.0"))

# ---- level proximity (ANNOTATED, not enforced) ------------------------------
# How near the max-OI / max-pain wall counts as "at the level", percent of spot.
# A turn far from a wall is logged with its distance, not dropped.
LEVEL_EPS_PCT = float(os.environ.get("LEVEL_EPS_PCT", "0.15"))

# ---- continuation / breakout branch -----------------------------------------
# A SECOND vocabulary beside the reversal turn: a KEY-LEVEL BREAKOUT that
# continues (with or without a retest). PROVISIONAL AND UNDER-MEASURED - n=1
# (the 2026-09-21 orb_breakout_retest). The STRUCTURE is committed; every number
# below is a starting point, env-overridable, to calibrate as breakout trades
# accumulate - the same discipline as the reversal branch shipped on 5 turns.
#
# Unlike a reversal, a continuation is defined BY a level, so the level is a
# GATE here (no key level broken -> no candidate), not the annotation it is for
# reversals. The named levels are the 15-min opening range (orb_high/orb_low, on
# the fno row) and the previous day's extremes (pd_high/pd_low, on the daily
# read - which can be absent, and then that pair is simply unavailable).
#
# A breakout that continues also confirms SAME-TICK, not one tick later: the
# 2026-09-21 break read SHORT_COVERING with PCR rising 1.12->1.20 on the break
# snapshot. So the +1-tick lag is a REVERSAL phenomenon and does not apply here.
CONT_VOLUME_Z_MIN = float(os.environ.get("CONT_VOLUME_Z_MIN", "2.0"))  # spike z on the break bar (spikes only, like VOLUME_Z_MIN)

# The retest entry is a DIP back through the broken level (a close on its far
# side) followed by a RECLAIM (a close back across it), timed to the reclaim
# bar's close - not to any distance from the level, so there is no proximity
# tunable here. A pure continuation that never dips back has no reclaim and no
# retest entry (the no-retest case, whose entry is deferred).

# ---- Telegram alerting ------------------------------------------------------
# The detector pushes an alert to Telegram on the actionable events - a
# confirmed reversal turn, a fresh key-level break, and a retest-hold entry -
# so a setup that "can produce a trade signal" reaches the phone, not only the
# log. The same parameter the daily brief and error-notifier use.
TELEGRAM_PARAMETER_NAME = os.environ.get("TELEGRAM_PARAMETER_NAME", "/algo/telegram/brief")
HTTP_TIMEOUT_SECONDS = float(os.environ.get("HTTP_TIMEOUT_SECONDS", "8"))

# --------------------------------------------------------------------------
# The strategy-manager gate
#
# When a turn is confirmed this function invokes strategy-manager - so from day
# one the manager runs ONLY on a detected turn, not every snapshot. UNSET MEANS
# OFF: with no name configured nothing is dispatched and a log line says so.
# Setting it needs one IAM change - this function's execution role must allow
# lambda:InvokeFunction on the manager's ARN, and NOT on a wildcard.
# --------------------------------------------------------------------------
STRATEGY_MANAGER_FUNCTION_NAME = os.environ.get("STRATEGY_MANAGER_FUNCTION_NAME", "")
STRATEGY_MANAGER_INVOCATION_TYPE = os.environ.get(
    "STRATEGY_MANAGER_INVOCATION_TYPE", "Event"
)
