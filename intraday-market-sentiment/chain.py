"""
Turning one option chain into the numbers the snapshot row carries.

Nothing here talks to Dhan or to Postgres - it takes the `data` block
dhan.option_chain() returned and computes. That separation is what lets the
whole of this module be exercised against a saved payload with no token and no
database.

Two widths, and they are not interchangeable:

    aggregates (PCR, OI totals, max pain, the OI walls)   ATM +-20 strikes
    raw legs written to option_chain_snapshot             ATM +-2 strikes

The live chain carries 232 strikes. Computing PCR across all of them would let
strikes 5,000 points away - which trade a few hundred lots and never move -
outvote the strikes price is actually near.

MAX PAIN IS WINDOWED, AND THAT IS A REAL LIMITATION. It is computed over the
same ATM +-20 strikes as everything else, so it can only ever name a strike
inside that window. A true max pain sweeps the whole chain. On a quiet day the
two agree; on a day that has trended hard the windowed answer will sit at the
window's edge, and a reader should treat an answer AT the boundary as "at
least this far", not as the minimum. The alternative - sweeping 232 strikes -
was not chosen because the same far-OTM OI that distorts PCR distorts it too.
"""

import logging

logger = logging.getLogger()

CE, PE = "ce", "pe"


def _number(value):
    """A leg's numeric field, with Dhan's 0-means-absent mapped to None.

    ONLY for implied_volatility and the greeks. Measured 2026-09-12: an
    illiquid deep-ITM put quoting 490.6 returned implied_volatility 0 and
    delta/theta/gamma/vega all 0. Stored as 0 those poison any average or
    subtraction taken across legs - iv_skew in particular is a subtraction, so
    one absent leg turns it into a number that looks like a reading.

    A genuine 0 is indistinguishable from an absent one in this payload. For
    IV and the greeks a true zero is not a thing a live option has, so mapping
    both to None loses nothing. It would be wrong for last_price or oi, which
    is why those do not go through here.
    """
    if value is None:
        return None
    value = float(value)
    return None if value == 0 else value


def strikes_sorted(data):
    """
    The chain's strikes as (float, original-key) pairs, ascending.

    The key is kept because the payload is keyed by a SIX-DECIMAL STRING -
    "23950.000000" - and rebuilding that from a float is a formatting
    assumption waiting to break. Look it up, do not reconstruct it.
    """
    pairs = sorted(((float(k), k) for k in data["oc"]), key=lambda p: p[0])
    if len(pairs) < 2:
        raise RuntimeError(f"option chain has {len(pairs)} strike(s), need at least 2")
    return pairs


def strike_step(pairs):
    """
    The gap between adjacent strikes, DERIVED rather than hardcoded.

    NIFTY is 50 today and has not always been. The same reasoning as the
    futures month: read it from the data, so the code cannot disagree with the
    exchange.
    """
    return pairs[1][0] - pairs[0][0]


def atm_index(pairs, spot):
    """Index of the strike nearest `spot`. Ties go to the lower strike."""
    return min(range(len(pairs)), key=lambda i: (abs(pairs[i][0] - spot), i))


def window(pairs, centre, per_side):
    """
    `per_side` strikes either side of `centre`, clamped to the chain.

    Clamping is silent by design: near a chain's edge a narrower window is the
    honest answer, and refusing would take the whole snapshot down over a
    condition the exchange controls. It is logged when it bites.
    """
    low = max(0, centre - per_side)
    high = min(len(pairs), centre + per_side + 1)
    if high - low != 2 * per_side + 1:
        logger.info(
            "strike window clamped to %d of the %d requested",
            high - low, 2 * per_side + 1,
        )
    return pairs[low:high]


def _leg(data, key, side):
    return (data["oc"].get(key) or {}).get(side) or {}


def summarise(data, spot, per_side):
    """
    The aggregate block for one expiry.

    `spot` is the index price the ATM is chosen against. It is passed in
    rather than taken from data["last_price"] so that both expiries in a
    snapshot are centred on the SAME price - two chains fetched seconds apart
    can report slightly different last_price values, and an ATM that differs
    between them would make near_* and mth_* quietly incomparable.
    """
    pairs = strikes_sorted(data)
    centre = atm_index(pairs, spot)
    atm_value, atm_key = pairs[centre]
    scoped = window(pairs, centre, per_side)

    ce_oi = pe_oi = ce_volume = pe_volume = 0
    max_call = max_put = None
    max_call_oi = max_put_oi = -1
    for value, key in scoped:
        call, put = _leg(data, key, CE), _leg(data, key, PE)
        call_oi, put_oi = int(call.get("oi") or 0), int(put.get("oi") or 0)
        ce_oi += call_oi
        pe_oi += put_oi
        ce_volume += int(call.get("volume") or 0)
        pe_volume += int(put.get("volume") or 0)
        if call_oi > max_call_oi:
            max_call_oi, max_call = call_oi, value
        if put_oi > max_put_oi:
            max_put_oi, max_put = put_oi, value

    atm_call, atm_put = _leg(data, atm_key, CE), _leg(data, atm_key, PE)
    ce_ltp = atm_call.get("last_price")
    pe_ltp = atm_put.get("last_price")
    straddle = (
        float(ce_ltp) + float(pe_ltp)
        if ce_ltp is not None and pe_ltp is not None
        else None
    )
    ce_iv, pe_iv = _number(atm_call.get("implied_volatility")), _number(
        atm_put.get("implied_volatility")
    )

    return {
        "atm_strike": atm_value,
        "ce_ltp": float(ce_ltp) if ce_ltp is not None else None,
        "pe_ltp": float(pe_ltp) if pe_ltp is not None else None,
        "straddle": straddle,
        "straddle_pct": (straddle / spot * 100) if straddle and spot else None,
        "pcr_oi": (pe_oi / ce_oi) if ce_oi else None,
        "pcr_volume": (pe_volume / ce_volume) if ce_volume else None,
        "ce_oi_total": ce_oi,
        "pe_oi_total": pe_oi,
        "max_oi_call": max_call,
        "max_oi_put": max_put,
        "max_pain": max_pain(data, scoped),
        "ce_iv": ce_iv,
        "pe_iv": pe_iv,
        "iv_skew": (pe_iv - ce_iv) if ce_iv is not None and pe_iv is not None else None,
        "strike_step": strike_step(pairs),
        "strikes_scoped": len(scoped),
    }


def max_pain(data, scoped):
    """
    The strike at which option writers lose least, over `scoped`.

    For a candidate expiry price K, a call struck at S is in the money by
    (K - S) and a put by (S - K). Total writer pain at K is the OI-weighted
    sum of both across the window; max pain is the K that minimises it.

    See the module docstring for why a windowed answer is not the same as a
    whole-chain one.
    """
    legs = [
        (value, int((_leg(data, key, CE)).get("oi") or 0),
         int((_leg(data, key, PE)).get("oi") or 0))
        for value, key in scoped
    ]
    if not legs:
        return None
    best_strike, best_pain = None, None
    for candidate, _, _ in legs:
        pain = sum(
            call_oi * max(0.0, candidate - strike)
            + put_oi * max(0.0, strike - candidate)
            for strike, call_oi, put_oi in legs
        )
        if best_pain is None or pain < best_pain:
            best_strike, best_pain = candidate, pain
    return best_strike


def raw_legs(data, spot, per_side):
    """
    The legs to store verbatim - `per_side` strikes either side of ATM, both
    sides of each. per_side=2 gives 5 strikes and 10 rows.

    Returned as storage-shaped dicts rather than the payload's own nesting, so
    db.py holds no knowledge of Dhan's field names and the 0-means-absent rule
    is applied in exactly one place.
    """
    pairs = strikes_sorted(data)
    scoped = window(pairs, atm_index(pairs, spot), per_side)
    rows = []
    for value, key in scoped:
        for side, label in ((CE, "CE"), (PE, "PE")):
            leg = _leg(data, key, side)
            if not leg:
                logger.info("no %s leg at strike %s - skipped", label, value)
                continue
            greeks = leg.get("greeks") or {}
            rows.append({
                "strike": value,
                "option_type": label,
                "option_security_id": (
                    str(leg["security_id"]) if leg.get("security_id") is not None
                    else None
                ),
                "last_price": leg.get("last_price"),
                "oi": int(leg["oi"]) if leg.get("oi") is not None else None,
                "previous_oi": (
                    int(leg["previous_oi"]) if leg.get("previous_oi") is not None
                    else None
                ),
                "volume": int(leg["volume"]) if leg.get("volume") is not None else None,
                "previous_volume": (
                    int(leg["previous_volume"])
                    if leg.get("previous_volume") is not None else None
                ),
                "previous_close_price": leg.get("previous_close_price"),
                "average_price": leg.get("average_price"),
                "implied_volatility": _number(leg.get("implied_volatility")),
                "delta": _number(greeks.get("delta")),
                "theta": _number(greeks.get("theta")),
                "gamma": _number(greeks.get("gamma")),
                "vega": _number(greeks.get("vega")),
                "top_bid_price": leg.get("top_bid_price"),
                "top_bid_quantity": (
                    int(leg["top_bid_quantity"])
                    if leg.get("top_bid_quantity") is not None else None
                ),
                "top_ask_price": leg.get("top_ask_price"),
                "top_ask_quantity": (
                    int(leg["top_ask_quantity"])
                    if leg.get("top_ask_quantity") is not None else None
                ),
            })
    return rows
