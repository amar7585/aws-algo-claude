"""The candidate as a Telegram block. ASCII only. Index points. Not an order."""

from config import FINAL_CLOSE_LABEL
from pullback import clock

NAMES = {"pdh": "PDH", "pdl": "PDL", "orh": "ORH", "orl": "ORL"}


def _p(x):
    return "-" if x is None else f"{x:.2f}"


def render(candidate, detection, snapshot, stamp):
    c, d = candidate, detection
    up = c["side"] == "LONG"
    word, beyond = ("below", "min") if up else ("above", "max")
    pair = "PDH / ORH" if up else "PDL / ORL"
    lv = c["levels"]
    pair_values = f"{_p(lv['pdh'])} / {_p(lv['orh'])}" if up else f"{_p(lv['pdl'])} / {_p(lv['orl'])}"
    sub = f"/{d['subtype']}" if d.get("subtype") else ""
    lines = [
        f"TREND PULLBACK {c['side']} - {stamp}",
        f"Day trend {d['day_trend']} | hold at {NAMES[c['key_level']]} {_p(c['key_price'])} "
        f"({d['family']}{sub}, candle {clock(c['trigger_ts'])})",
        f"Entry  {_p(c['entry'])}  (index close of the {clock(c['trigger_ts'])} candle)",
        f"Stop   exit on a 5-min close {word} {_p(c['off_line'])} ({beyond} of {pair} {pair_values})",
        f"       nominal {_p(c['stop_nominal'])}, risk {c['risk']:.0f} pts",
    ]
    if c["t1"] is None:
        lines.append("T1     none - entry is at the session extreme; hold to the stop or the "
                     f"{FINAL_CLOSE_LABEL} close")
    else:
        lines.append(f"T1     {_p(c['t1'])} session {'high' if up else 'low'} (+{c['t1_room']:.0f}, RR 1:{c['rr']:.1f})"
                     " -> then stop to break-even")
        lines.append(f"T2     hold the rest to the {FINAL_CLOSE_LABEL} close")
    vz = d.get("vol_z")
    rv = d.get("rvol_tod")
    lines.append(f"v2: vol z {'-' if vz is None else f'{float(vz):.2f}'}, rvol {'-' if rv is None else f'{float(rv):.2f}'}x"
                 f" | 15m {snapshot.get('regime') or '-'}|{snapshot.get('bias') or '-'}")
    lines.append("Index points. Not an order.")
    return "\n".join(lines)
