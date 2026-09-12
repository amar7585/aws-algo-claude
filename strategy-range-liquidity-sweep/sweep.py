"""
Sweep detection on 5-minute bars, and what happened after each one.

THE TRADE IS THE FAILURE, NOT THE BREAK. Four things have to line up, in
order, and every one of them is a separate check below because the pattern
without any one of them is a different pattern:

    penetration   a wick beyond the level, inside the sweep band
    rejection     a close back inside, on that bar or the next
    no extension  the following bar prints no new extreme beyond the wick
    reclaim       price closed back on the range side of the level

PERCENTAGES ARE TAKEN AGAINST THE LEVEL, not against spot or the bar close.
That is how the playbook's own worked examples compute: Example 1's 8.2 points
beyond 24,158.25 is quoted as 0.034%, which is 8.2/24158.25. Using spot
instead would shift every threshold by however far price had travelled from
the level, which is precisely the quantity being measured.

WHY THERE IS NO STORED STATE FOR RE-ENTRIES. The playbook caps attempts at one
re-entry per side per session and wants the running cost of an idea reported.
That looks like it needs memory across invocations, and it does not: a
candidate is a deterministic function of the bars, so re-deriving the day's
whole sweep sequence from the session's candles on every run reproduces the
attempt count exactly. Nothing is persisted, and a re-run of this function
reaches the same answer instead of double-counting an attempt.
"""

import logging

from config import (
    ACCEPTANCE_CLOSES,
    MAX_PENETRATION_PCT,
    MIN_PENETRATION_PCT,
    NO_FOLLOW_THROUGH_BARS,
    NO_FOLLOW_THROUGH_RISK_FRACTION,
)
from levels import HIGH, LOW, ist_datetime

logger = logging.getLogger()

# Why a candidate was thrown away. The playbook requires these be reported
# rather than dropped - the rejections are how the thresholds get calibrated.
TOO_SHALLOW = "too shallow"
BREAKOUT = "breakout"
NO_REJECTION = "no close back inside"
EXTENDED = "next bar printed a new extreme"
PENDING = "awaiting the confirmation bar"

# What became of an attempt, once the bars after it are known.
WORKED = "worked"
CASE_A = "A"
CASE_B = "B"
CASE_C = "C"
OPEN = "open"


def _beyond(side, price, level):
    """Is `price` beyond `level` on the sweeping side?"""
    return price > level if side == HIGH else price < level


def _inside(side, price, level):
    """Is `price` back on the range side of `level`?"""
    return price <= level if side == HIGH else price >= level


def _extreme(side, bar):
    return bar["high"] if side == HIGH else bar["low"]


def _more_extreme(side, a, b):
    """The further of two prices in the sweeping direction."""
    return max(a, b) if side == HIGH else min(a, b)


def detect(bars, pools):
    """
    Every sweep attempt in `bars`, in time order, candidates and rejects alike.

    `bars` are the session's CLOSED 5-minute bars, oldest first. Each returned
    event carries `reject_reason` when it did not qualify, and None when it
    did - the caller reports both, because a near miss with its reason is the
    output the playbook asks for on a day with no trade.
    """
    events = []
    for pool in pools:
        side, level = pool["side"], pool["price"]
        for index, bar in enumerate(bars):
            wick = _extreme(side, bar)
            if not _beyond(side, wick, level):
                continue

            # Only the first penetration of a pool starts an attempt. Later
            # bars that also poke through belong to the same excursion, and
            # counting each of them would turn one sweep into five.
            if index and _beyond(side, _extreme(side, bars[index - 1]), level):
                continue

            events.append(_evaluate(bars, index, pool))

    events.sort(key=lambda event: (event["sweep_ts"], event["pool"]["priority"]))
    return events


def _evaluate(bars, index, pool):
    side, level = pool["side"], pool["price"]
    bar = bars[index]
    sweep_extreme = _extreme(side, bar)

    event = {
        "pool": pool,
        "sweep_index": index,
        "sweep_ts": bar["ts"],
        "sweep_time": ist_datetime(bar["ts"]).strftime("%H:%M"),
        "sweep_extreme": sweep_extreme,
        "level": level,
        "side": side,
        "penetration": round(abs(sweep_extreme - level), 2),
        "penetration_pct": round(abs(sweep_extreme - level) / level * 100, 4),
        "reject_reason": None,
        "rejection_bar": None,
        "entry_price": None,
        "confirmed": False,
    }

    # ---- the band -------------------------------------------------------
    if event["penetration_pct"] < MIN_PENETRATION_PCT:
        event["reject_reason"] = TOO_SHALLOW
        return event
    if event["penetration_pct"] > MAX_PENETRATION_PCT:
        event["reject_reason"] = BREAKOUT
        return event

    # ---- rejection: this bar closes back inside, or the next one does ----
    reject_index = None
    if _inside(side, bar["close"], level):
        reject_index = index
    elif index + 1 < len(bars) and _inside(side, bars[index + 1]["close"], level):
        reject_index = index + 1
        # The next bar may have pushed the wick further before closing back
        # inside. The sweep extreme is the furthest point of the excursion,
        # not of its first bar - the stop goes beyond THAT.
        sweep_extreme = _more_extreme(
            side, sweep_extreme, _extreme(side, bars[index + 1])
        )
        event["sweep_extreme"] = sweep_extreme
        event["penetration"] = round(abs(sweep_extreme - level), 2)
        event["penetration_pct"] = round(
            abs(sweep_extreme - level) / level * 100, 4
        )
        if event["penetration_pct"] > MAX_PENETRATION_PCT:
            event["reject_reason"] = BREAKOUT
            return event

    if reject_index is None:
        # Either it has not closed back inside yet, or it closed beyond and
        # that is acceptance rather than a sweep. The difference matters, so
        # the two get different reasons.
        event["reject_reason"] = (
            PENDING if index + 1 >= len(bars) else NO_REJECTION
        )
        return event

    event["rejection_bar"] = reject_index
    event["rejection_on_next"] = reject_index != index
    # The reclaim close IS the entry price for a close entry. Example 1 takes
    # its 24,147.25 entry from exactly this bar.
    event["entry_price"] = bars[reject_index]["close"]
    event["reclaim_ts"] = bars[reject_index]["ts"]
    event["reclaim_time"] = ist_datetime(bars[reject_index]["ts"]).strftime("%H:%M")

    # ---- no extension: the bar after the rejection -----------------------
    confirm_index = reject_index + 1
    if confirm_index >= len(bars):
        event["reject_reason"] = PENDING
        return event
    if _beyond(side, _extreme(side, bars[confirm_index]), sweep_extreme):
        event["reject_reason"] = EXTENDED
        return event

    event["confirmed"] = True
    event["confirm_index"] = confirm_index
    event["confirm_ts"] = bars[confirm_index]["ts"]
    return event


def acceptance_run(bars, level, side, from_index):
    """
    The longest run of consecutive closes BEYOND the level from `from_index`.

    This is the single measurement that separates a broken range from a deep
    sweep, and the playbook hangs its whole stop-out triage on it:
    ACCEPTANCE_CLOSES consecutive closes beyond is Case A - the level did not
    hold, the range is gone, and both sides are dead for the session. Anything
    less, with price back inside, is Case B - the pool was deeper than the
    first wick suggested and one re-entry is allowed.
    """
    longest = run = 0
    for bar in bars[from_index:]:
        if _beyond(side, bar["close"], level):
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    return longest


def acceptance_at(bars, level, side, closes=None):
    """
    The bar index at which the Nth consecutive close beyond `level` completed.

    None when it never happens. This is the moment the RANGE ITSELF broke, and
    the playbook is unusually firm about the consequence: once a level has been
    accepted through, that side is finished for the session AND so is the other
    one, because "if the opening-range high has been accepted through, a later
    sweep of the range low is not a range trade - the range no longer exists".

    Computed independently of whether an attempt was ever taken, because the
    range can break without this function having produced a candidate, and the
    stand-down applies either way.
    """
    closes = ACCEPTANCE_CLOSES if closes is None else closes
    run = 0
    for index, bar in enumerate(bars):
        if _beyond(side, bar["close"], level):
            run += 1
            if run >= closes:
                return index
        else:
            run = 0
    return None


def outcome(bars, event, trade):
    """
    What became of an attempt: WORKED, CASE_A, CASE_B, CASE_C or OPEN.

    Walked forward bar by bar from the confirmation, because the order of
    events is the whole question - a target reached before the stop is a win
    and the same two prices in the other order is a loss, and a summary of the
    session cannot tell them apart.
    """
    side = event["side"]
    start = event.get("confirm_index", event["sweep_index"]) + 1
    forward = bars[start:]
    if not forward:
        return {"outcome": OPEN, "bars_after": 0}

    stop, target, entry = trade["stop"], trade["t2"], trade["entry"]
    short = side == HIGH  # a swept high is faded short

    for offset, bar in enumerate(forward, start=1):
        hit_stop = bar["high"] >= stop if short else bar["low"] <= stop
        hit_target = bar["low"] <= target if short else bar["high"] >= target
        # Both in one bar is unresolvable at this resolution. The stop is
        # assumed first: it is the conservative read, and calling it a win
        # would flatter the record of a setup whose whole value is its
        # risk-reward.
        if hit_stop:
            longest = acceptance_run(
                bars, event["level"], side, event["sweep_index"]
            )
            case = CASE_A if longest >= ACCEPTANCE_CLOSES else CASE_B
            return {
                "outcome": case,
                "bars_after": offset,
                "acceptance_closes": longest,
                "stopped_at": stop,
            }
        if hit_target:
            return {"outcome": WORKED, "bars_after": offset, "reached": target}

    # Neither. Case C is the playbook's "expired rather than failed": roughly
    # NO_FOLLOW_THROUGH_BARS later, price is back around the entry and the
    # range is intact.
    if len(forward) >= NO_FOLLOW_THROUGH_BARS:
        risk = abs(entry - stop)
        drift = abs(forward[-1]["close"] - entry)
        if risk and drift <= risk * NO_FOLLOW_THROUGH_RISK_FRACTION:
            return {"outcome": CASE_C, "bars_after": len(forward)}
    return {"outcome": OPEN, "bars_after": len(forward)}
