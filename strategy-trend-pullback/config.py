"""
strategy-trend-pullback tunables. Every number is an environment override,
because these are starting values from one traded session (2026-11-06) and one
replay (pattern-recorder/replay/out/trend_playbook_replay.md), not laws.
"""

import os

# The context strategy-manager hands over. v2 is the manager's current shape;
# this playbook additionally needs `detection` (v2's alert) and `index_bars`.
EXPECTED_CONTEXT_VERSION = int(os.environ.get("EXPECTED_CONTEXT_VERSION", "2"))

# ---- the setup (Amar, 2026-11-09): breakout and hold only ----------------------
# C2 = a retest / hold of a key level already broken this session in the trend's
# direction (touch_hold, dip_reclaim, wick_reclaim); R1 = a hold candle at the
# level. NOT C1 (the break itself - weak in the replay) and NOT R2 (sweeps belong
# to the sweep playbook on no-trend days).
HOLD_FAMILIES = tuple(x for x in os.environ.get("HOLD_FAMILIES", "C2,R1").split(",") if x)
KEY_LEVELS = ("pdh", "pdl", "orh", "orl")

# Signal window, on the trigger candle's CLOSE (IST minutes). 09:45-14:30, the
# window of Amar's short-into-liquidity playbook.
WINDOW_FROM_MINUTE = int(os.environ.get("WINDOW_FROM_MINUTE", str(9 * 60 + 45)))
WINDOW_TO_MINUTE = int(os.environ.get("WINDOW_TO_MINUTE", str(14 * 60 + 30)))

# ---- the stop (S3, Amar 2026-11-09) ----------------------------------------------
# Exit on a 5-min CLOSE beyond the day-trend's own off line: below min(PDH, ORH)
# for a long, above max(PDL, ORL) for a short. The NOMINAL stop (for the risk
# figure) is that line minus a buffer - the sweep playbook's buffer rule:
# max(STOP_BUFFER_PCT of price, STOP_BUFFER_RANGE_FACTOR x the mean range of the
# last STOP_BUFFER_LOOKBACK_BARS candles).
STOP_BUFFER_PCT = float(os.environ.get("STOP_BUFFER_PCT", "0.03"))
STOP_BUFFER_RANGE_FACTOR = float(os.environ.get("STOP_BUFFER_RANGE_FACTOR", "0.5"))
STOP_BUFFER_LOOKBACK_BARS = int(os.environ.get("STOP_BUFFER_LOOKBACK_BARS", "6"))

# ---- targets -----------------------------------------------------------------------
# T1 = the session extreme before the trigger candle, then stop to break-even on
# the remainder; T2 = the remainder held to this candle's close (the index
# freezes for the closing auction from 15:15).
FINAL_CLOSE_LABEL = os.environ.get("FINAL_CLOSE_LABEL", "15:10")

# ---- delivery --------------------------------------------------------------------
# UNSET = SHADOW: the candidate is logged as "WOULD SEND" and nothing is sent.
TELEGRAM_ENABLED = os.environ.get("TELEGRAM_ENABLED", "").strip().lower() in ("1", "on", "true", "yes")
TELEGRAM_PARAMETER_NAME = os.environ.get("TELEGRAM_PARAMETER_NAME", "/algo/telegram/brief")
HTTP_TIMEOUT_SECONDS = float(os.environ.get("HTTP_TIMEOUT_SECONDS", "8"))
