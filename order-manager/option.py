"""
The paper OPTION that follows each index paper position - PURE (Amar 2026-11-28: "Paper on the option").

    what        bought only (selling later): a CE for a long, a PE for a short, 1 strike in the money ("1 ITM"):
                ATM = the nearest 50 to the index price of the opening step (Python's round, as R1 measured it);
                strike = ATM - 50 for a CE, ATM + 50 for a PE
    expiry      the nearest expiry in Dhan's list; on that expiry's own day, the next one (as measured: the monthly
                and keep-the-expiring variants made no difference)
    when        on every index step that changes quantity, and only when that step is APPLIED:
                  ENTRY / RE-ENTRY   buy  lots x lot_size units (a re-entry buys a NEW contract at that moment)
                  T1                 sell lots x lot_size units
                  any close          sell everything still open (a close while already out sells nothing)
                Exits are decided on the INDEX by the playbooks; the option only follows them.
    price       Dhan's live price when the step is applied (O2): the chain's last_price on a buy, a quote on a sell
    rupees      premium x units. realized_rupees = received - cost of the units sold (average cost of the units
                open); charges are ESTIMATED per order (rates unverified) and kept apart in charges_est
The arithmetic is recomputed from the fills on every step, so the totals cannot drift from the fills.
"""

import datetime
import decimal

from config import OPTION_CHARGES

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
OPENING = ("ENTRY", "RE-ENTRY")
CLOSING = ("EXIT", "STOP", "TARGET", "TRAIL", "TIME EXIT", "DAILY LOSS EXIT", "KILL SWITCH EXIT")
BUY, SELL = "BUY", "SELL"
Q = decimal.Decimal("0.01")


def D(x):
    return decimal.Decimal(str(x))


def option_type(direction):
    return "CE" if direction == "long" else "PE"


def strike_for(index_price, direction, depth, step):
    """1 ITM: a CE below ATM for a long, a PE above it for a short. ATM uses Python's round, as R1 measured it."""
    atm = round(float(index_price) / step) * step
    return atm - step * depth if direction == "long" else atm + step * depth


def pick_expiry(expiries, session_date):
    """The nearest expiry; on its own expiry day, the next one. `expiries` are ISO dates; returns a date."""
    days = sorted({datetime.date.fromisoformat(str(e)[:10]) for e in expiries if datetime.date.fromisoformat(str(e)[:10]) >= session_date})
    if not days:
        raise RuntimeError(f"no option expiry on or after {session_date} in Dhan's list {expiries}")
    if days[0] == session_date:
        if len(days) < 2:
            raise RuntimeError(f"{session_date} is an expiry day and Dhan's list has no later expiry: {expiries}")
        return days[1]
    return days[0]


def find_leg(chain, strike, typ):
    """(security_id, last_price) of the strike's CE / PE in the chain's `oc`. Missing or unpriced RAISES."""
    for key, legs in chain["oc"].items():
        if abs(float(key) - float(strike)) < 1e-6:
            leg = (legs or {}).get(typ.lower())
            if not leg or leg.get("security_id") is None:
                raise RuntimeError(f"the chain has strike {strike} but no {typ} leg with a security_id")
            px = leg.get("last_price")
            if px is None or float(px) <= 0:
                raise RuntimeError(f"{strike} {typ} has no live price in the chain ({px!r})")
            return str(leg["security_id"]), float(px)
    raise RuntimeError(f"strike {strike} is not in the option chain ({len(chain['oc'])} strikes)")


def symbol(underlying, expiry, strike, typ):
    return f"{underlying} {expiry.strftime('%d%b').upper()} {int(strike) if float(strike).is_integer() else strike} {typ}"


def expiry_ts(expiry):
    return int(datetime.datetime(expiry.year, expiry.month, expiry.day, tzinfo=IST).timestamp())


def action(sig, opt_row, lot_size):
    """(side, units) the option takes on this APPLIED index step, or None."""
    kind = sig["kind"]
    if kind in OPENING:
        return BUY, int(sig["lots"]) * int(lot_size)
    if opt_row is None:
        return None
    if kind == "T1":
        units = int(sig["lots"]) * int(lot_size)
        if units > int(opt_row["open_units"]):
            raise RuntimeError(f"T1 sells {units} option units but only {opt_row['open_units']} are open")
        return SELL, units
    if kind in CLOSING and int(opt_row["open_units"]) > 0:
        return SELL, int(opt_row["open_units"])
    return None


def charges(price, units, side):
    c = {k: D(v) for k, v in OPTION_CHARGES.items()}
    v = D(price) * units
    exch, sebi = c["exchange"] * v, c["sebi"] * v
    total = c["brokerage"] + exch + sebi + c["gst"] * (c["brokerage"] + exch + sebi)
    total += c["stamp_buy"] * v if side == BUY else c["stt_sell"] * v
    return total.quantize(Q)


def totals(fills):
    """Recompute the row's numbers from its fills (average cost of the units open)."""
    buy_q = sell_q = 0
    buy_v = sell_v = realized = cost_open = ch = D(0)
    open_u = 0
    for f in fills:
        px, u = D(f["price"]), int(f["units"])
        ch += D(f["charges"])
        if f["side"] == BUY:
            buy_q += u; buy_v += px * u; open_u += u; cost_open += px * u
        else:
            if u > open_u:
                raise RuntimeError(f"a sale of {u} option units with only {open_u} open")
            avg = cost_open / open_u
            realized += (px - avg) * u
            cost_open -= avg * u
            sell_q += u; sell_v += px * u; open_u -= u
    return {"buy_qty": buy_q, "buy_value": buy_v.quantize(Q), "sell_qty": sell_q, "sell_value": sell_v.quantize(Q),
            "open_units": open_u, "realized_rupees": realized.quantize(Q), "charges_est": ch.quantize(Q)}


def sale_rupees(fills, units, price):
    """This sale's gross rupees at the average cost of the units open before it."""
    open_u, cost = 0, D(0)
    for f in fills:
        if f["side"] == BUY:
            open_u += int(f["units"]); cost += D(f["price"]) * int(f["units"])
        else:
            avg = cost / open_u
            cost -= avg * int(f["units"]); open_u -= int(f["units"])
    return ((D(price) - cost / open_u) * units).quantize(Q)


def line(side, sym, units, price, sale=None):
    text = f"option: {side} {sym} x{units} @ {float(price):.2f}"
    if sale is not None:
        text += f" | this sale {'+' if sale >= 0 else '-'}₹{abs(float(sale)):,.0f}"
    return text
