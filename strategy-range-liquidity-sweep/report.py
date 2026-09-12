"""
The output, in the shapes the playbook specifies.

THE LOG IS THIS FUNCTION'S ONLY OUTPUT. It writes no table and emits no
signal, so what it renders here is the entire record of what it saw - which is
why the rejected candidates are rendered too. The playbook is explicit:
"Report every candidate found, including the ones that failed the sweep band
or the RR filter, with the reason. The rejections are how the thresholds get
calibrated over time."

ASCII only, to match the rest of this repo - the playbook's prose uses em
dashes and a unicode minus, and those are rendered as "-" here rather than
carried into log output.
"""

from config import MIN_RISK_REWARD


def _pool_label(event, stacked):
    names = [event["pool"]["name"]] + [pool["name"] for pool in stacked]
    if len(names) == 1:
        return f"{names[0]} @ {event['level']:.2f}"
    prices = [event["pool"]["price"]] + [pool["price"] for pool in stacked]
    joined = " + ".join(names)
    return f"{joined} @ " + ", ".join(f"{price:.2f}" for price in prices)


def candidate_block(event, candidate, stacked, invalidation):
    """The SWEEP CANDIDATE block for one qualifying setup."""
    rejection = (
        "closed back inside on the next candle"
        if event.get("rejection_on_next")
        else "closed back inside on the sweep candle"
    )
    lines = [
        f"SWEEP CANDIDATE - {candidate['direction']}",
        f"Time            {event['sweep_time']}",
        f"Pool swept      {_pool_label(event, stacked)}",
        f"Penetration     {event['penetration']:.2f} "
        f"({event['penetration_pct']:.3f}%) - inside band",
        f"Rejection       {rejection}",
        "Confirmation    no new extreme on following candle: yes",
        "",
        f"Entry  {candidate['entry']:.2f}  ({candidate['entry_kind']})",
        f"Stop   {candidate['stop']:.2f}  (risk {candidate['risk']:.2f}, "
        f"buffer {candidate['stop_buffer']:.2f})",
    ]
    if candidate["t1"] is not None:
        lines.append(f"T1     {candidate['t1']:.2f}  ({candidate['t1_name']})")
    else:
        lines.append("T1     none between entry and the opposite side")
    lines.append(
        f"T2     {candidate['t2']:.2f}  (reward {candidate['reward']:.2f}, "
        f"RR 1:{candidate['risk_reward']:.2f}) - {candidate['t2_name']}"
    )
    if candidate["t3"] is not None:
        lines.append(
            f"T3     {candidate['t3']:.2f}  ({candidate['t3_name']}, "
            f"stacked-pool sweep only)"
        )
    lines += [
        "",
        f"Grade: {candidate['grade']} - {candidate['grade_reason']}",
        f"Invalidated by: {invalidation}",
    ]
    return "\n".join(lines)


def re_entry_block(event, candidate, stacked, first, outcome, cumulative_risk):
    """
    The RE-ENTRY block, including the running cost of the idea.

    That last line is in the playbook for a stated reason: it is the number
    easiest to lose track of mid-session, and it belongs in front of the user
    rather than in their head. It is not a sizing instruction.
    """
    passes = (
        "passes"
        if candidate and candidate["risk_reward"] >= MIN_RISK_REWARD
        else f"fails the {MIN_RISK_REWARD} filter"
    )
    lines = [
        f"RE-ENTRY - {first['direction']}, attempt 2 of 2 on this side",
        f"First attempt   entry {first['entry']:.2f}, "
        f"stopped {first['stop']:.2f}  (-{first['risk']:.2f} pts)",
        f"Failure type    Case {outcome['outcome']} - "
        f"{'extended sweep, no acceptance' if outcome['outcome'] == 'B' else outcome['outcome']}",
        f"                (max closes beyond level: "
        f"{outcome.get('acceptance_closes', 0)}, penetration "
        f"{event['penetration_pct']:.3f}% - inside band)",
        "",
    ]
    if candidate:
        lines += [
            f"New entry {candidate['entry']:.2f}   "
            f"New stop {candidate['stop']:.2f}  (risk {candidate['risk']:.2f} pts)",
            f"Target unchanged {candidate['t2']:.2f}   "
            f"New RR 1:{candidate['risk_reward']:.2f}   {passes}",
        ]
    else:
        lines.append(f"No re-entry - {passes}")
    lines.append(
        f"Cumulative risk on this idea today: {cumulative_risk:.2f} pts against "
        f"a {first['reward']:.2f} pt target"
    )
    return "\n".join(lines)


def rejected_block(rejections):
    """
    Every candidate that did not qualify, with what it was short of.

    The playbook asks for the nearest miss by name on a day with no setup, and
    for the thresholds not to be quietly bent when a real setup falls just
    outside one. Rendering the number alongside the threshold it missed is how
    that stays visible.
    """
    if not rejections:
        return "No sweep attempts found against any pool."
    lines = [f"REJECTED - {len(rejections)} attempt(s) did not qualify:"]
    for event, reason in rejections:
        lines.append(
            f"  {event['sweep_time']}  {event['pool']['name']} @ "
            f"{event['level']:.2f}  penetration {event['penetration']:.2f} "
            f"({event['penetration_pct']:.3f}%)  ->  {reason}"
        )
    return "\n".join(lines)


def stand_down_block(failures, details):
    """The gate refused. Name every check that failed, and nothing else."""
    lines = ["NO SCAN - the regime gate did not pass:"]
    lines += [f"  - {failure}" for failure in failures]
    lines.append(
        "  window {window}, regime {regime}/{bias}, adr {adr}, atr14 {atr14}, "
        "opening range {orb_range} ({orb_range_pct}%)".format(
            window=details.get("window"),
            regime=details.get("regime"),
            bias=details.get("bias"),
            adr=details.get("adr"),
            atr14=details.get("atr14"),
            orb_range=details.get("orb_range"),
            orb_range_pct=details.get("orb_range_pct"),
        )
    )
    return "\n".join(lines)


def invalidation(event):
    """The specific condition that kills this candidate, not a generic list."""
    side = "above" if event["side"] == "high" else "below"
    return (
        f"two consecutive 5-minute closes {side} {event['level']:.2f} "
        f"(acceptance - the range is breaking), a close {side} it on expanding "
        f"volume with follow-through, india vix moving more than 5%, or the "
        f"15:00 cutoff"
    )
