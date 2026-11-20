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
