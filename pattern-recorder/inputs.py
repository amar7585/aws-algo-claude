"""
pattern-recorder - one session's inputs for patterns.detect_session.

ONE ASSEMBLY PATH. The Lambda (handler.py, fed from SQL rows) and the offline
replay (replay/replay.py, fed from the pulled JSON) both build a session's
inputs here. "Live == replay" is then a check on one piece of code, not on two
copies that could drift: which contract, which previous session, which earlier
sessions feed the time-of-day volume.

Pure: no database, no clock. Rows in, plain bar lists out.

ROWS. Future and index rows are candle_5min rows {security_id, candle_ts, open,
high, low, close, volume}; fno rows are intraday_fno_data rows carrying at least
snapshot_ts and fut_security_id, plus the wall columns for today. Times are epoch
seconds; days are IST day numbers from structure.ist_day - never SQL timezone
arithmetic (CLAUDE.md hard rule 2).
"""

import collections
import os

import structure as st

# Earlier sessions whose same-clock bar feeds rvol_tod (definitions section 7).
RVOL_SESSIONS = int(os.environ.get("V2_RVOL_SESSIONS", "20"))


def to_bar(row):
    return {"ts": int(row["candle_ts"]), "open": float(row["open"]), "high": float(row["high"]),
            "low": float(row["low"]), "close": float(row["close"]),
            "volume": float(row["volume"] or 0)}


class Store:
    """
    The rows of a window, indexed by IST day: the futures by contract, the index,
    and the fno snapshots. Built once per Lambda run (or once per replay), then
    asked for any day's inputs.
    """

    def __init__(self, fut_rows, idx_rows, fno_rows):
        self.fut = collections.defaultdict(lambda: collections.defaultdict(list))
        for row in fut_rows:
            bar = to_bar(row)
            self.fut[str(row["security_id"])][st.ist_day(bar["ts"])].append(bar)
        self.idx = collections.defaultdict(list)
        for row in idx_rows:
            bar = to_bar(row)
            self.idx[st.ist_day(bar["ts"])].append(bar)
        for contract in self.fut:
            for day in self.fut[contract]:
                self.fut[contract][day].sort(key=lambda b: b["ts"])
        for day in self.idx:
            self.idx[day].sort(key=lambda b: b["ts"])
        self.fno = collections.defaultdict(list)
        for row in fno_rows:
            row = dict(row)
            row["snapshot_ts"] = int(row["snapshot_ts"])
            self.fno[st.ist_day(row["snapshot_ts"])].append(row)
        for day in self.fno:
            self.fno[day].sort(key=lambda r: r["snapshot_ts"])
        self.days = sorted(set(self.idx) | {d for c in self.fut for d in self.fut[c]})

    def active_contract(self, day):
        """
        The future the session ran on: the fut_security_id the stored snapshots
        carried (it rolls at monthly expiry), or - for days before snapshots
        existed - the contract that traded most. (contract, how) or (None, None).
        """
        rows = self.fno.get(day)
        if rows:
            counts = collections.Counter(str(r["fut_security_id"]) for r in rows if r.get("fut_security_id"))
            if counts:
                return counts.most_common(1)[0][0], "stored"
        best = max(((sum(b["volume"] for b in self.fut[c].get(day, [])), c) for c in self.fut),
                   default=(0, None))
        return (best[1], "max_volume") if best[0] > 0 else (None, None)

    def previous_day(self, day):
        earlier = [d for d in self.days if d < day]
        return earlier[-1] if earlier else None

    def rvol_history(self, day):
        """
        {ist_minute: [that clock bar's volume in each of the previous RVOL_SESSIONS
        sessions]} - each session read on ITS OWN active contract, so a roll does
        not compare a front month with a thin far month.
        """
        hist = collections.defaultdict(list)
        earlier = []
        for d in reversed([d for d in self.days if d < day]):
            contract, _ = self.active_contract(d)
            if contract is not None and self.fut[contract].get(d):
                earlier.append((d, contract))
            if len(earlier) == RVOL_SESSIONS:
                break
        for d, contract in reversed(earlier):
            for bar in self.fut[contract][d]:
                hist[st.ist_minute(bar["ts"])].append(bar["volume"])
        return dict(hist)

    def session_inputs(self, day, contract=None, closed_by=None, snapshot_ts=None):
        """
        Everything detect_session needs for `day`, as live would see it.

        contract    the active future; live passes the payload's fut_security_id,
                    the replay leaves it to active_contract()
        closed_by   a run's captured_at: only today's bars CLOSED by then
                    (candle_ts + 300 <= closed_by). None = the whole session.
        snapshot_ts only today's fno rows stamped at or before this run's snapshot.
        """
        if contract is None:
            contract, _ = self.active_contract(day)
        contract = None if contract is None else str(contract)
        prev_day = self.previous_day(day)

        def closed(bars):
            if closed_by is None:
                return list(bars)
            return [b for b in bars if b["ts"] + st.BAR_SECONDS <= closed_by]

        fno_today = self.fno.get(day, [])
        if snapshot_ts is not None:
            fno_today = [r for r in fno_today if r["snapshot_ts"] <= snapshot_ts]
        return {
            "contract": contract,
            "session_bars": closed(self.fut[contract].get(day, [])) if contract else [],
            "prev_bars": list(self.fut[contract].get(prev_day, [])) if contract and prev_day is not None else [],
            "index_bars": closed(self.idx.get(day, [])),
            "prev_index_bars": list(self.idx.get(prev_day, [])) if prev_day is not None else [],
            "fno_rows": fno_today,
            "rvol_history": self.rvol_history(day),
        }


def detect(session, detect_session):
    """Run the library on session_inputs() output (detect_session passed in to keep this module pure)."""
    return detect_session(session["session_bars"], session["prev_bars"], session["index_bars"],
                          session["prev_index_bars"], session["fno_rows"], session["rvol_history"])
