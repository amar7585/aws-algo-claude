"""
The ~15:45 EVENING BRIEF (Amar, 2026-11-23: step 2 of the phone flow) - every line pure.

    build(inputs) -> (text, missing)

No Dhan, no database: the handler reads, this composes - so the whole brief can be
rebuilt offline for every stored session. It READS ONLY: the evening run never
touches the daily read, candle_daily or the width profile (decision E2), and the
brief itself is not stored (E3: every number is re-derivable from the tables).

THE SECTIONS, in order:
  CLOSE         the official close (the 15:25 auction candle) against PDC, the
                15:15 price (the 15:10 candle - the last continuously traded
                price), the day range against ADR, the open, the 15-min range and
                where the close sits against it
  VIX           the last closed 5-min VIX candle against the previous daily close.
                The official daily close is not available at 15:45: Dhan's daily
                endpoint lags a session (dhan.py)
  LEVELS        the "open" daily highs / lows (as of yesterday's close) that today
                traded through; a daily FVG formed today; FVGs filled today
  OPTION CHAIN  the morning (first stored 5-min snapshot) -> the close (the
                evening chain read; the last stored snapshot when that read
                fails): ATM, call wall with its OI, put wall with its OI, max pain,
                PCR, on the current weekly expiry
  THE DAY'S READ  day_trend through the day's 15-min rows
  ALERTS / TRADES the alerts sent; the day's paper positions (position_master,
                since P2a: the sweep) with lot-points; trend trades are recorded
                from P2b
  NEXT SESSION  PDH / PDL / PDC for tomorrow, and the next weekly expiry
  SYSTEM        what was stored today: 5-min candles, 15-min rows, 5-min snapshots
ASCII only, like every message this repo sends.

THE INPUTS (a dict; any value may be an Exception - its section then reads
"missing - <reason>"):
  today, today_start     datetime.date and its IST midnight epoch
  ist_date, ist_hm       epoch -> date / "HH:MM"
  daily, vix_daily       NIFTY / INDIA VIX daily candles, COMPLETED sessions only, oldest first
  bars                   today's stored 5-min index candles {ts, open, high, low, close}, oldest first
  vix_now                {"value", "clock"} - the latest closed VIX 5-min candle
  sentiments             today's 15-min rows {snapshot_ts, day_trend}, oldest first
  snapshots              today's fno_5m_snapshot rows, oldest first
  fno15_rows             today's intraday_fno_data row count
  chain                  handler.fetch_chain(...) at the evening run, or an Exception
  alerts                 today's pattern_events rows that were sent to Telegram
  positions              today's algo.position_master rows
  adr_sessions           int (config)
"""

from brief import Missing, _need, adr, day_label, n0, n2, oi_m, open_levels, unfilled_fvgs

AUCTION_CANDLE = "15:25"   # the closing auction lands in this candle (NSE, since 2026-08-03)
LAST_LIVE_CANDLE = "15:10"  # its close is the 15:15 price - the last continuously traded one
SHORT = {"strategy-range-liquidity-sweep": "sweep", "strategy-trend-pullback": "trend"}


def _day(x):
    """(bars, high, low, official close, 15:15 price, final) - final = the auction candle is stored."""
    bars = _need(x, "bars")
    if not bars:
        raise Missing("no 5-min candle stored today - did the market open?")
    hm = x["ist_hm"]
    final = any(hm(b["ts"]) == AUCTION_CANDLE for b in bars)
    live = [b for b in bars if hm(b["ts"]) <= LAST_LIVE_CANDLE]
    return (bars, max(b["high"] for b in bars), min(b["low"] for b in bars), bars[-1]["close"],
            live[-1]["close"] if live else None, final)


def section_close(x):
    daily = _need(x, "daily")
    bars, dh, dl, close, p1515, final = _day(x)
    pdc = daily[-1]["close"]
    chg = close - pdc
    rng = dh - dl
    where = ""
    if rng:
        pos = (close - dl) / rng
        where = " (top tenth of the day range)" if pos >= 0.9 else " (bottom tenth of the day range)" if pos <= 0.1 else ""
    hm = x["ist_hm"]
    head = (f"CLOSE {n2(close)}{where} vs PDC {n2(pdc)}: {chg:+,.2f} pts / {chg / pdc * 100:+.2f}%" if final else
            f"CLOSE not final - the {AUCTION_CANDLE} candle is not stored yet; last stored {hm(bars[-1]['ts'])} "
            f"candle closed {n2(close)} ({chg:+,.2f} vs PDC {n2(pdc)})")
    lines = [head]
    if p1515 is not None:
        lines.append(f"  15:15 price {n2(p1515)} (last traded candle - the closing auction sets the rest)")
    try:
        vs_adr = f" = {rng / adr(daily, x['adr_sessions']) * 100:.0f}% of ADR{x['adr_sessions']}"
    except Missing:
        vs_adr = ""
    orb = [b for b in bars if hm(b["ts"]) < "09:30"]
    orb_txt = ""
    if len(orb) == 3:
        oh, ol = max(b["high"] for b in orb), min(b["low"] for b in orb)
        side = "above" if close > oh else "below" if close < ol else "inside"
        orb_txt = f" | 15-min range {n2(ol)} - {n2(oh)}, closed {side} it"
    lines.append(f"  day range {n2(dl)} - {n2(dh)} ({rng:,.0f} pts{vs_adr}) | open {n2(bars[0]['open'])}{orb_txt}")
    if not final:
        x.setdefault("_late", []).append(("CLOSE", f"the {AUCTION_CANDLE} auction candle is not stored - "
                                                   f"the close is not final"))
    return lines


def section_vix(x):
    v = _need(x, "vix_now")
    vd = _need(x, "vix_daily")
    if not vd:
        raise Missing("no VIX daily close stored")
    prev = vd[-1]["close"]
    return [f"VIX {v['value']:.2f} at {v['clock']} ({v['value'] - prev:+.2f} / {(v['value'] / prev - 1) * 100:+.2f}% "
            f"vs prev close {prev:.2f}) [last 5-min candle; the official close prints overnight]"]


def section_levels(x):
    daily = _need(x, "daily")
    bars, dh, dl, close, _, _ = _day(x)
    ist_date = x["ist_date"]
    highs, lows = open_levels(daily)
    taken = ([f"high {day_label(ist_date(b['ts']))} ({n2(b['high'])})" for b in sorted(highs, key=lambda b: b["high"])
              if b["high"] <= dh] +
             [f"low {day_label(ist_date(b['ts']))} ({n2(b['low'])})" for b in sorted(lows, key=lambda b: -b["low"])
              if b["low"] >= dl])
    lines = ["LEVELS FILLED TODAY: " + ("; ".join(taken) if taken else "none")]
    prev2 = daily[-2] if len(daily) >= 2 else None
    new = None
    if prev2 is not None:
        if dl > prev2["high"]:
            new = f"new bullish daily FVG {n2(prev2['high'])}-{n2(dl)}"
        elif dh < prev2["low"]:
            new = f"new bearish daily FVG {n2(dh)}-{n2(prev2['low'])}"
    filled = [g for g in unfilled_fvgs(daily)
              if ((dl <= g["lo"]) if g["kind"] == "bullish" else (dh >= g["hi"]))]
    part = new or "no new daily FVG formed"
    if filled:
        part += "; filled today: " + "; ".join(
            f"{g['kind']} {n2(g['lo'])}-{n2(g['hi'])} ({ist_date(g['ts']):%d %b})" for g in filled)
    lines.append(f"  daily FVG: {part}")
    return lines


def section_chain(x):
    snaps = _need(x, "snapshots")
    if not snaps:
        raise Missing("no 5-min option snapshot stored today")
    first = snaps[0]
    c = x.get("chain")
    if isinstance(c, dict):
        s = c["summary"]
        end = {"atm": s["atm_strike"], "call": s["max_oi_call"], "put": s["max_oi_put"], "pain": s["max_pain"],
               "pcr": s["pcr_oi"]}
        expiry, src = c["expiry"], ""
        oi = f" (OI {oi_m(c.get('call_oi'))})", f" (OI {oi_m(c.get('put_oi'))})"
    else:
        last = snaps[-1]
        end = {"atm": last["near_atm_strike"], "call": last["near_max_oi_call"], "put": last["near_max_oi_put"],
               "pain": last["near_max_pain"], "pcr": last["near_pcr_oi"]}
        expiry = x["ist_date"](last["near_expiry_ts"]) if last.get("near_expiry_ts") else None
        src = f" [the evening chain read failed ({c}) - the last stored snapshot {x['ist_hm'](last['snapshot_ts'])}]"
        oi = "", ""
        x.setdefault("_late", []).append(("OPTION CHAIN", f"the evening chain read failed ({c}) - "
                                                          f"no OI at the walls, no next expiry"))
    head = "OPTION CHAIN" + (f" (current expiry {day_label(expiry)}" + (", EXPIRY TODAY" if expiry == x["today"] else "")
                             + ")" if expiry else "")
    return [f"{head}: ATM {n0(first['near_atm_strike'])} -> {n0(end['atm'])} "
            f"(from {x['ist_hm'](first['snapshot_ts'])}){src}",
            f"  call wall {n0(first['near_max_oi_call'])} -> {n0(end['call'])}{oi[0]} | put wall "
            f"{n0(first['near_max_oi_put'])} -> {n0(end['put'])}{oi[1]} | max pain {n0(first['near_max_pain'])} -> "
            f"{n0(end['pain'])} | PCR {first['near_pcr_oi']:.2f} -> {end['pcr']:.2f}"]


def section_read(x):
    rows = _need(x, "sentiments")
    if not rows:
        raise Missing("no 15-min row stored today")
    changes = []
    for r in rows:
        if not changes or changes[-1][1] != r.get("day_trend"):
            changes.append((x["ist_hm"](r["snapshot_ts"]), r.get("day_trend")))
    return ["THE DAY'S READ  trend " + " -> ".join(f"{t} {s}" for s, t in changes)
            + f" (last row {x['ist_hm'](rows[-1]['snapshot_ts'])})"]


def section_trades(x):
    alerts = _need(x, "alerts")
    positions = _need(x, "positions")
    routed = sum(1 for a in alerts if a.get("manager_sent_at"))
    parts, total = [], 0.0
    for p in sorted(positions, key=lambda p: int(p["entry_ts"])):
        lot = int(p["lot_size"])
        lp = float(p["realized_profit"]) / lot
        total += lp
        state = p.get("exit_reason") if p.get("status") == "CLOSED" else p.get("status")
        parts.append(f"{SHORT.get(p['playbook'], p['playbook'])} {x['ist_hm'](p['entry_ts'])} {p['position_type']} "
                     f"{state} {lp:+.1f} lot-pts" + (" (T1 booked)" if p.get("t1_booked_ts") else ""))
    return [f"ALERTS {len(alerts)} sent ({routed} to the playbooks)",
            "  TRADES (paper): " + ("; ".join(parts) if parts else "none recorded")
            + (f" | day {total:+.1f} lot-pts" if parts else ""),
            "  [trend-playbook trades are recorded from P2b]"]


def section_next(x):
    _, dh, dl, close, _, final = _day(x)
    lines = [f"NEXT SESSION  PDH {n2(dh)} | PDL {n2(dl)} | PDC {n2(close)}" + ("" if final else " (close not final)")]
    c = x.get("chain")
    if isinstance(c, dict) and c.get("expiries"):
        nxt = [d for d in c["expiries"] if d > x["today"]]
        if nxt:
            lines[0] += f" | next weekly expiry {day_label(nxt[0])}"
    return lines


def section_system(x):
    bars = x.get("bars") if isinstance(x.get("bars"), list) else []
    sents = x.get("sentiments") if isinstance(x.get("sentiments"), list) else []
    snaps = x.get("snapshots") if isinstance(x.get("snapshots"), list) else []
    f15 = x.get("fno15_rows")
    return [f"SYSTEM stored today: 5-min candles {len(bars)} | 15-min rows {len(sents)}"
            + (f" (+{f15} option rows)" if isinstance(f15, int) else "") + f" | 5-min option snapshots {len(snaps)}"]


SECTIONS = (("CLOSE", section_close), ("VIX", section_vix), ("LEVELS", section_levels), ("OPTION CHAIN", section_chain),
            ("THE DAY'S READ", section_read), ("ALERTS / TRADES", section_trades), ("NEXT SESSION", section_next),
            ("SYSTEM", section_system))
BLANK_AFTER = ("VIX", "LEVELS", "OPTION CHAIN", "ALERTS / TRADES")


def build(inputs):
    """(text, missing): every section in its own guard - a missing input becomes a
    "missing" line and an entry in `missing`, never an exception. A CLOSE that is
    not final (the auction candle not stored yet) is also listed in `missing`."""
    x = dict(inputs)
    today = x["today"]
    lines = [f"NIFTY EVENING BRIEF - {day_label(today)} {today:%Y}", ""]
    missing = []
    for name, section in SECTIONS:
        try:
            lines += section(x)
        except Missing as exc:
            lines.append(f"{name}: missing - {exc}")
            missing.append((name, str(exc)))
        except Exception as exc:  # a bug must not cost the brief - it is reported, not swallowed
            lines.append(f"{name}: missing - {type(exc).__name__}: {exc}")
            missing.append((name, f"{type(exc).__name__}: {exc}"))
        if name in BLANK_AFTER:
            lines.append("")
    missing += x.get("_late", [])
    lines += ["Index points. Not a trade recommendation."]
    text = "\n".join(lines)
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    ascii_text = text.encode("ascii", "replace").decode("ascii")
    if ascii_text != text:
        missing.append(("TEXT", "non-ASCII characters replaced"))
    return ascii_text, missing
