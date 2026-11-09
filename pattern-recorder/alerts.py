"""
pattern-recorder - which v2 events are ALERTS, and the alert's text. Pure.

THE RULE (plan v3, Amar 2026-11-07/08/09), on the classifier's day_trend:
  up    -> every UP pattern
  down  -> every DOWN pattern
  none  -> rejections (R1) and sweeps (R2), both ways - the no-trend branch,
           KEPT (Amar, 2026-11-09)
and only at the KEY levels: PDH / PDL / ORH / ORL (Amar, 2026-11-08) - the
event's own level, or one within NEAR of it (other_levels). Before the day's
first classifier row (09:45) the trend is read as none, as in the replay.

Volume and OI are SHOWN in the message, never a gate: the replays found no gate
that kept the logged trades (v2_confirmation.md).

Replayed over 90 sessions (replay/out/v21_replay.md): 7.5 alerts a day at the
KEY levels (2.0 from the trend branch, 5.5 from the no-trend one); 3 of the 9
hand-logged trades caught.

Which run decides: an event is judged ONCE, on the run that first sees it
(fresh), against the latest classifier row that run can read. It is never
re-judged later when the trend changes.
"""

import structure as st

KEY_LEVELS = frozenset(st.INDEX_TWINS)
TREND_STATES = ("up", "down", "none")

FAMILY_NAMES = {"R1": "REJECTION", "R2": "SWEEP", "C1": "BREAK", "C2": "RETEST"}


def at_key(event):
    return event["level"] in KEY_LEVELS or any(x in KEY_LEVELS for x in event["other_levels"])


def passes(event, day_trend):
    """Is this event an alert under day_trend ('up' | 'down' | 'none' | None)?"""
    if not at_key(event):
        return False
    if day_trend in ("up", "down"):
        return event["direction"] == day_trend
    return event["family"] in ("R1", "R2")


def _num(value, fmt):
    return "-" if value is None else format(float(value), fmt)


def message(event, stamp, clock, day_trend, bar_oi_change, fifteen):
    """
    The Telegram text. ASCII only, one alert per message.

    bar_oi_change  the future's OI change on the trigger bar (the poller's
                   fut_bar_oi_change for that bar), or None
    fifteen        the latest 15-min row (merged fno + sentiment) or None
    """
    name = FAMILY_NAMES[event["family"]]
    sub = f"/{event['subtype']}" if event["subtype"] else ""
    level = event["level"]
    if event["level_strike"] is not None and level in KEY_LEVELS:
        where = f"{level} {_num(event['level_strike'], '.2f')} (fut {_num(event['level_price'], '.1f')})"
    elif event["level_strike"] is not None:
        where = f"{level} strike {_num(event['level_strike'], '.0f')} (fut {_num(event['level_price'], '.1f')})"
    else:
        where = f"{level} fut {_num(event['level_price'], '.1f')}"
    near = [x for x in event["other_levels"] if x in KEY_LEVELS]
    if near:
        where += " near " + ",".join(near)
    lines = [
        f"V2 {name} {event['direction'].upper()} - {stamp}",
        f"{event['family']}{sub} at {where}, candle {clock}, close {_num(event['trigger_close'], '.1f')}",
        f"day trend {day_trend or 'none'} | candles {','.join(event['candles']) or '-'}"
        + (f" | {event['chart_pattern']}" if event["chart_pattern"] else ""),
        f"vol z {_num(event['vol_z'], '.2f')}, rvol {_num(event['rvol_tod'], '.2f')}x"
        f" | fut bar dOI {'-' if bar_oi_change is None else f'{int(bar_oi_change):+d}'}",
    ]
    if fifteen:
        lines.append(
            f"15m: buildup {fifteen.get('buildup') or '-'}, regime {fifteen.get('regime') or '-'}|"
            f"{fifteen.get('bias') or '-'}, PE OI {_num(fifteen.get('near_pe_oi_change_pct'), '+.1f')}%,"
            f" CE OI {_num(fifteen.get('near_ce_oi_change_pct'), '+.1f')}%")
    return "\n".join(lines)
