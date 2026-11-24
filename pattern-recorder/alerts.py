"""
pattern-recorder - which v2 events are ALERTS. Pure.

An alert is handed to the manager (the playbooks); it is not sent to Telegram.
The alert's Telegram text was removed 2026-11-23 with the raw alerts (Amar:
"remove completely those messages from code ... stick with the new flow").

THE RULE (plan v3, Amar 2026-11-07/08/09), on the classifier's day_trend:
  up    -> every UP pattern
  down  -> every DOWN pattern
  none  -> rejections (R1) and sweeps (R2), both ways - the no-trend branch,
           KEPT (Amar, 2026-11-09)
and only at the KEY levels: PDH / PDL / ORH / ORL (Amar, 2026-11-08) - the
event's own level, or one within NEAR of it (other_levels). Before the day's
first classifier row (09:45) the trend is read as none, as in the replay.

C1 (Amar, 2026-11-23, "Approve as recommended"): on a TREND day the level set
is the key levels PLUS the call wall and the put wall (TREND_LEVELS) - the
trend's holds on 2026-11-23 were all at levels the key set did not hold. The
no-trend branch is unchanged: R1 / R2 at the key levels only. Replayed on 88
sessions (replay/out/miss-2026-11-23/): +2,213 point-lots over the key set,
profit factor 1.87 -> 2.22, no larger drawdown; evidence = 35 option sessions.
RULES_VERSION stays v2.2: detection is unchanged, only which events alert.

Volume and OI are never a gate: the replays found no gate that kept the logged
trades (v2_confirmation.md). They are stored on the event row.

Replayed over 90 sessions (replay/out/v21_replay.md): 7.5 alerts a day at the
KEY levels (2.0 from the trend branch, 5.5 from the no-trend one); 3 of the 9
hand-logged trades caught.

Which run decides: an event is judged ONCE, on the run that first sees it
(fresh), against the latest classifier row that run can read. It is never
re-judged later when the trend changes.
"""

import structure as st

KEY_LEVELS = frozenset(st.INDEX_TWINS)
TREND_LEVELS = KEY_LEVELS | frozenset((st.CALL_WALL, st.PUT_WALL))   # C1, 2026-11-23: the trend branch only
TREND_STATES = ("up", "down", "none")


def at_levels(event, levels):
    """The event's own level, or one within NEAR of it (other_levels), is in `levels`."""
    return event["level"] in levels or any(x in levels for x in event["other_levels"])


def at_key(event):
    return at_levels(event, KEY_LEVELS)


def passes(event, day_trend):
    """Is this event an alert under day_trend ('up' | 'down' | 'none' | None)?"""
    if day_trend in ("up", "down"):
        return event["direction"] == day_trend and at_levels(event, TREND_LEVELS)
    return at_key(event) and event["family"] in ("R1", "R2")
