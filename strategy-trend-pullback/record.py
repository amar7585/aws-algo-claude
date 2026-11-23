"""
Trend positions through order-manager (P2b, Amar 2026-11-22 / 11-23). PURE.

ENTRY (an alert, entry_exit): the candidate becomes an ENTRY - 3 lots at the trigger candle's close (the fill at the
signal), stop_loss = the stop line (ORH / ORL), risk_pts = R, t1_target = entry + 2R - carrying the candidate text.
Outside entry_exit the same candidate is sent as REJECTED (its text headed REJECTED with the reason).

EXIT MODE (a tick, the risk manager passes this playbook's open positions in `run`, so it reads no database): for
each position the decided rule (exits.py) is re-derived from the stored entry, stop line and R over today's closed
candles, and only what order-manager does not have yet is sent - read, then catch up, like the sweep:
  * a step on a candle after the row's last applied one;
  * a step ON that candle only if its effect is not in the row (T1 booked there; a stop not yet at that level);
  * a closed position takes nothing more.
force_exit: after those steps, what is still OPEN closes - TIME EXIT at the 15:10 candle's close, DAILY LOSS EXIT
at the newest close.

Messages (the playbook's own text): T1 - 2 of 3 sold; STOP TO ENTRY (the first protected move); STOPPED (the range
close); TRAILED OUT / BREAK-EVEN (the protected stop); CLOSED AT 15:15; DAILY LOSS EXIT. Trail moves after the first
are stored (tsl_stop_loss) and NOT messaged (Amar 2026-11-23).
"""

import exits

LAST_LIVE_MINUTE = 15 * 60 + 10


def _clock(ts):
    m = ((int(ts) + 19800) % 86400) // 60
    return f"{m // 60:02d}:{m % 60:02d}"


def _stamp(ts):
    import datetime
    d = datetime.datetime.fromtimestamp(int(ts) + 19800, datetime.timezone.utc)
    return d.strftime("%Y-%m-%d %H:%M")


def entry_signal(candidate, detection, instrument, session_start, playbook, text, mode, why):
    c = candidate
    up = c["side"] == "LONG"
    sg = 1 if up else -1
    R = float(c["risk"])
    t1 = c["entry"] + sg * exits.T1_RATIO * R
    sig = {"playbook": playbook,
           "signal_key": f"{detection['trigger_ts']}|{detection['family']}|{detection.get('subtype') or 'none'}|{detection['direction']}",
           "instrument": {k: str(instrument[k]) for k in ("security_id", "instrument_type", "exchange_segment", "trading_symbol")},
           "session_start": int(session_start), "entry_ts": int(c["trigger_ts"]), "candle_ts": int(c["trigger_ts"]),
           "direction": "long" if up else "short", "price": round(float(c["entry"]), 2), "lots": exits.LOTS,
           "stop_loss": round(float(c["off_line"]), 2), "t1_target": round(t1, 2), "risk_pts": round(R, 2), "message": text}
    if mode != "entry_exit":
        sig.update(kind="REJECTED", refused_reason=why or mode, message=f"TREND PULLBACK REJECTED - {why or mode}\n{text}")
    else:
        sig["kind"] = "ENTRY"
    return sig


def _texts(row, s, side):
    word = "LONG" if side == "LONG" else "SHORT"
    e = float(row["open_avg"])
    st = _stamp(s["candle_ts"] + 300)
    k = s["kind"]
    if k == "T1":
        return (f"TREND T1 {word} - {st}\nT1 {s['price']:.2f} (entry + 2R) traded - 2 of 3 lots sold (entry {e:.2f})\n"
                f"the last lot: stop to entry at +1R, then trails on the previous 5-min candle\nfrom: trend playbook")
    if k == "STOP MOVE":
        if not s.get("first"):
            return None
        return (f"TREND STOP TO ENTRY {word} - {st}\nprice reached +1R - the stop is now {s['stop']:.2f} "
                f"(entry {e:.2f}); from the next candle it trails on the previous candle's "
                f"{'low' if side == 'LONG' else 'high'}, never back past entry\nfrom: trend playbook")
    if k == "STOP":
        return (f"TREND STOPPED {word} - {st}\na 5-min close back inside the 15-min range ({float(row['stop_loss']):.2f}) - "
                f"out at {s['price']:.2f} (entry {e:.2f})\nfrom: trend playbook")
    if k == "TRAIL":
        what = "BREAK-EVEN" if s.get("break_even") else "TRAILED OUT"
        return f"TREND {what} {word} - {st}\nthe protected stop traded - out at {s['price']:.2f} (entry {e:.2f})\nfrom: trend playbook"
    raise RuntimeError(f"no text for trend step {k}")


def _signal(row, s, text):
    side = row["position_type"]
    sig = {"kind": s["kind"], "playbook": row["playbook"], "signal_key": row["signal_key"],
           "instrument": {k: str(row[k]) for k in ("security_id", "instrument_type", "exchange_segment", "trading_symbol")},
           "session_start": int(row["session_start"]), "entry_ts": int(row["entry_ts"]),
           "direction": "long" if side == "LONG" else "short", "candle_ts": int(s["candle_ts"]), "message": text}
    if s["kind"] == "STOP MOVE":
        sig["stop_loss"] = s["stop"]
    else:
        sig["price"] = s["price"]
    if s["kind"] == "T1":
        sig["lots"] = s["lots"]
    return sig


def _applied(row, s):
    k, ts = s["kind"], int(s["candle_ts"])
    if k == "T1":
        return row.get("t1_booked_ts") is not None and int(row["t1_booked_ts"]) == ts
    if k == "STOP MOVE":
        cur = row.get("tsl_stop_loss")
        if cur is None:
            return False
        return float(cur) >= s["stop"] if row["position_type"] == "LONG" else float(cur) <= s["stop"]
    return row["status"] == "CLOSED"


def pending(row, bars, newest_ts):
    """The signals for one stored position that order-manager does not have yet, oldest first."""
    if row["status"] != "OPEN":
        return []
    path = exits.steps(bars, int(row["entry_ts"]), row["position_type"], float(row["open_avg"]), float(row["stop_loss"]),
                       float(row["risk_pts"]), float(row["t1_target"]) if row.get("t1_target") is not None else None)
    last = int(row["last_update"])
    out = []
    for s in path:
        ts = int(s["candle_ts"])
        if ts < last or (ts == last and _applied(row, s)):
            continue
        text = _texts(row, s, row["position_type"])
        if text and ts < int(newest_ts) and s["kind"] != "STOP MOVE":
            text += f"\n(late: this step was on the {_clock(ts)} candle)"
        out.append(_signal(row, s, text))
    return out


def forced_close(row, bars, force, why, status):
    """The close of a position still OPEN after this run's steps (force_exit)."""
    if status != "OPEN":
        return None
    if force == "TIME EXIT":
        bar = next((b for b in bars if ((int(b["ts"]) + 19800) % 86400) // 60 >= LAST_LIVE_MINUTE), bars[-1])
        word = "CLOSED AT 15:15"
        why_text = "the closing auction freezes the index at 15:15 - out"
    else:
        bar = bars[-1]
        word = "DAILY LOSS EXIT"
        why_text = f"the risk manager's daily loss limit: {why or force} - out"
    side = row["position_type"]
    px = round(float(bar["close"]), 2)
    text = (f"TREND {word} {'LONG' if side == 'LONG' else 'SHORT'} - {_stamp(int(bar['ts']) + 300)}\n{why_text} at {px:.2f} "
            f"(entry {float(row['open_avg']):.2f})\nfrom: trend playbook")
    return _signal(row, {"kind": force, "candle_ts": int(bar["ts"]), "price": px}, text)
