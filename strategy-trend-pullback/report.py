"""The candidate as a Telegram block. ASCII only. Index points. Not an order."""

from config import FINAL_CLOSE_LABEL
from pullback import clock

NAMES = {"pdh": "PDH", "pdl": "PDL", "orh": "ORH", "orl": "ORL", "call_wall": "CALL WALL", "put_wall": "PUT WALL"}


def _name(level):
    return NAMES.get(level) or str(level).replace("_", " ").upper()


def _hold(c, d):
    """The hold line's level: 'ORL 25668.85' / 'PUT WALL 25500.00' / 'SWING LOW, near the PUT WALL' (C1, 2026-11-23)."""
    if c["key_price"] is not None:
        return f"{_name(c['key_level'])} {_p(c['key_price'])}"
    return f"{_name(d.get('level'))}, near the {_name(c['key_level'])}"


def _p(x):
    return "-" if x is None else f"{x:.2f}"


def render(candidate, detection, snapshot, stamp):
    c, d = candidate, detection
    up = c["side"] == "LONG"
    word = "below" if up else "above"
    lv = c["levels"]
    why = (f"{c['off_line_name']} - back inside the 15-min range {_p(lv['orl'])} - {_p(lv['orh'])} "
           f"= trend off")
    sub = f"/{d['subtype']}" if d.get("subtype") else ""
    lines = [
        f"TREND PULLBACK {c['side']} - {stamp}",
        f"Day trend {d['day_trend']} | hold at {_hold(c, d)} "
        f"({d['family']}{sub}, candle {clock(c['trigger_ts'])})",
        f"Entry  {_p(c['entry'])}  (index close of the {clock(c['trigger_ts'])} candle)",
        f"Stop   exit on a 5-min close {word} {_p(c['off_line'])} ({why})",
        f"       nominal {_p(c['stop_nominal'])}, risk {c['risk']:.0f} pts",
    ]
    # P2b (Amar 2026-11-22, "Previous-candle trail"; 3 lots, 2 at T1): T1 = entry + 2R, then stop to entry at
    # +1R, then the trail on the previous candle - replacing the session-extreme T1 and break-even.
    sg = 1 if up else -1
    t1 = c["entry"] + sg * 2.0 * c["risk"]
    lines.append(f"T1     {_p(t1)} (entry + 2R) -> sell 2 of 3 lots")
    lines.append(f"       the last lot: stop to entry at +1R ({_p(c['entry'] + sg * c['risk'])}), then trails on the "
                 f"previous 5-min candle's {'low' if up else 'high'}; out at the {FINAL_CLOSE_LABEL} close")
    vz = d.get("vol_z")
    rv = d.get("rvol_tod")
    lines.append(f"v2: vol z {'-' if vz is None else f'{float(vz):.2f}'}, rvol {'-' if rv is None else f'{float(rv):.2f}'}x"
                 f" | 15m {snapshot.get('regime') or '-'}")
    lines.append("Index points. Not an order.")
    return "\n".join(lines)
