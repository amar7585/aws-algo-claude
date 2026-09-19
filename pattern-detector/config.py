"""
pattern-detector tunables and the strategy-manager gate.

EVERY THRESHOLD HERE IS PROVISIONAL. The two-clock turn rule is measured on 5
turns over 2 sessions (the observations findings); the cutoffs below are
starting points to be calibrated once sessions accumulate, the same way the
classifier's option thresholds are deferred. They are env-overridable so a value
can be set from measurement without a code change.
"""

import os

# The future the detector reads from candle_5min. The SAME constant the loader
# and sentiment use, so the (security_id, instrument_type) identity matches what
# was stored - candle_5min is never queried on security_id alone (hard rule #4).
FUTURES_INSTRUMENT_TYPE = os.environ.get("FUTURES_INSTRUMENT_TYPE", "FUTIDX")

# ---- same-tick clock: the abnormal-volume reversal candle on the future -----
# Two-sided z-score, because the anomaly's DIRECTION varies - 2026-09-17 turns
# had volume COLLAPSE into them, 2026-09-18 had SPIKES. What is abnormal is the
# magnitude, not the sign.
VOLUME_LOOKBACK = int(os.environ.get("VOLUME_LOOKBACK", "20"))    # bars for the z baseline
VOLUME_Z_MIN = float(os.environ.get("VOLUME_Z_MIN", "2.0"))      # |z| that counts as abnormal

# The reversal candle is looked for among the future's 5-min bars that closed
# within this window before the snapshot - the turn is up to one confirmation
# tick (~15 min) before the snapshot that confirms it.
CANDIDATE_WINDOW_SECONDS = int(os.environ.get("CANDIDATE_WINDOW_SECONDS", "1800"))

# How many of the future's recent 5-min bars to read (z baseline + window).
FUT_BARS_LOOKBACK = int(os.environ.get("FUT_BARS_LOOKBACK", "40"))

# ---- +1-tick clock: option-OI confirmation ---------------------------------
# The |option OI change| (percent) that confirms a turn when buildup does not.
# From the observations - PE OI -10.1% and -4.2% both confirmed lows.
OI_CONFIRM_PCT = float(os.environ.get("OI_CONFIRM_PCT", "3.0"))

# ---- level proximity (ANNOTATED, not enforced) ------------------------------
# How near the max-OI / max-pain wall counts as "at the level", percent of spot.
# A turn far from a wall is logged with its distance, not dropped.
LEVEL_EPS_PCT = float(os.environ.get("LEVEL_EPS_PCT", "0.15"))

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
