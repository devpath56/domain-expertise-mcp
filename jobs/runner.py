"""Runner (mcp-runner-wire-20261007, slice wire.runner).

A scheduled plain script, never an agent: no LLM call, no network. One pass
  1. reads the trace store,
  2. calls core.derivation.derive() over the trailing 7-day window,
  3. derives operational metrics from the same records (below),
  4. writes the snapshot to the calibration store (core.calibration) and
     backfills any resolved/expired Test the calibration files do not hold,
  5. appends one line to the run log: ts, duration, judgments, cost.

Operational metrics, per seat. The trace log carries no latency or error
field, so both are derived from what it does carry, and said so in output:
- latency_ms: first Judgment record -> last record of that judgment (the
  token annotation or the Test). Covers trace emission and token counting;
  machine.check() runs before emit_judgment and is NOT inside it.
- tokens_per_call: emission prompt + completion from the token annotation;
  billed tokens counted separately (null = not reported, never 0; D9).
- tool-call success/failure: a call succeeded iff its judgment reached the
  end of the governed path (MachineRun and Test records). A missing token
  annotation is missingness on tokens_per_call, not a failure: judgments
  traced before row 43 have none. A call that raised before emit_judgment
  leaves no record, so it is invisible here.
- error_rate: failures / calls.
The repo's counts-only rule holds: percentiles, means and rates are null
below n=30, counts always.

Cost: wall seconds x RUNNER_USD_PER_SECOND (declared, default Render cron
Starter $0.00016/min). A run above $0.01 is flagged on stderr and in the log.

CLI:
  python jobs/runner.py                     # one pass (what cron calls)
  python jobs/runner.py --ticks 24 --every 1  # compressed schedule: 24 passes, 1s apart
  python jobs/runner.py misses --hours 24     # hourly slots in the log with no run
Schedule: jobs/runner.cron (hourly).
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import calibration  # noqa: E402
from core import derivation as d  # noqa: E402

LOG_PATH = ROOT / "logs" / "runner.jsonl"
WINDOW_DAYS = 7
USD_PER_SECOND = float(os.environ.get("RUNNER_USD_PER_SECOND", 0.00016 / 60))
COST_CEILING_USD = 0.01
CAL_OUTCOMES = d.RESOLVED | {"expired"}

OPERATIONAL_NOTES = [
    "latency_ms = first Judgment record ts -> last record ts of that judgment; "
    "machine.check() runs before emit_judgment and is not inside the span.",
    "success = judgment left MachineRun + Test; no token annotation is "
    "missingness on tokens_per_call, not failure; a call that raised before "
    "emit_judgment leaves no record and is not counted.",
    "percentiles, means and rates are null below n=30; counts always.",
]


def _ts(value):
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _mean(xs):
    return round(sum(xs) / len(xs), 2) if len(xs) >= d.MIN_N else None


def _dist(xs):
    return {"n": len(xs), "total": sum(xs), "mean": _mean(xs),
            "p50": d._percentile(xs, 0.50), "p95": d._percentile(xs, 0.95)}


def operational(records, start, end):
    """Per-seat latency, tokens/call, error rate, success/failure."""
    calls = {}
    for r in records:
        jid = r.get("judgment_id") or (r["id"] if r.get("type") == "Judgment" else None)
        if not jid:
            continue
        c = calls.setdefault(jid, {"seat": None, "created": None, "last": None,
                                   "types": set(), "tokens": None})
        t = _ts(r["ts"])
        c["last"] = t if c["last"] is None else max(c["last"], t)
        c["types"].add(r["type"])
        if r["type"] == "Judgment":
            c["seat"] = r.get("seat") or c["seat"]
            c["created"] = t if c["created"] is None else min(c["created"], t)
            if r.get("tokens"):
                c["tokens"] = r["tokens"]

    by_seat, alerts = {}, []
    for jid, c in calls.items():
        if not c["seat"] or c["created"] is None or not (start <= c["created"] < end):
            continue
        s = by_seat.setdefault(c["seat"], {"lat": [], "tok": [], "billed": [],
                                           "billed_null": 0, "no_tokens": 0,
                                           "ok": 0, "fail": 0})
        complete = {"MachineRun", "Test"} <= c["types"]
        s["ok" if complete else "fail"] += 1
        if complete:
            s["lat"].append(round((c["last"] - c["created"]).total_seconds() * 1000, 3))
        if c["tokens"] is None:
            s["no_tokens"] += 1
            continue
        em = c["tokens"].get("emission") or {}
        p, q = em.get("prompt"), em.get("completion")
        if p == 0 or q == 0:
            alerts.append({"kind": "zero_tokens", "judgment_id": jid, "seat": c["seat"]})
        elif isinstance(p, int) and isinstance(q, int):
            s["tok"].append(p + q)
        billed = c["tokens"].get("billed")
        if billed is None:
            s["billed_null"] += 1
        elif billed == 0:
            alerts.append({"kind": "zero_billed", "judgment_id": jid, "seat": c["seat"]})
        else:
            s["billed"].append(billed)

    seats = {}
    for seat, s in sorted(by_seat.items()):
        n = s["ok"] + s["fail"]
        seats[seat] = {
            "calls": n,
            "tool_calls": {"success": s["ok"], "failure": s["fail"]},
            "error_rate": d._rate("ops.error_rate", s["fail"], n, d._missing(0, n)),
            "latency_ms": _dist(s["lat"]),
            "tokens_per_call": {"emission": {**_dist(s["tok"]),
                                             "missing": d._missing(s["no_tokens"], n)},
                                "billed": {**_dist(s["billed"]),
                                           "missing": d._missing(
                                               s["billed_null"] + s["no_tokens"], n)}},
        }
    return {"seats": seats, "alerts": alerts, "notes": OPERATIONAL_NOTES}


def backfill_calibration(records):
    """Record each resolved/expired Test the calibration store lacks.

    resolve_test already writes calibration; this catches a resolution that
    reached the trace by another path. Keyed on test_id, so it is idempotent."""
    latest = {}
    for r in records:
        if r.get("type") == "Test":
            latest[r["id"]] = r  # append-only store: last line is newest
    have, written = {}, []
    for tid, t in latest.items():
        if t.get("state") not in CAL_OUTCOMES or not t.get("seat"):
            continue
        seat = t["seat"]
        if seat not in have:
            have[seat] = {e["test_id"] for e in calibration.read(seat)}
        if tid in have[seat]:
            continue
        calibration.record(seat, t["judgment_id"], tid, t["state"],
                           evidence=t.get("evidence", ""))
        have[seat].add(tid)
        written.append(tid)
    return written


def _log(entry, path=None):
    path = Path(path or LOG_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def run_once(trace_path=None, now=None, log_path=None):
    """One pass. Returns the log entry; raises DerivationRefused on bad input
    after logging the refusal."""
    t0 = time.perf_counter()
    now = now or datetime.now(timezone.utc)
    end = now
    start = end - timedelta(days=WINDOW_DAYS)
    entry = {"ts": now.isoformat(), "window": {"start": start.isoformat(),
                                               "end": end.isoformat()}}
    try:
        records = d._load_trace(trace_path)
        derived = d.derive(records, start.isoformat(), end.isoformat())
        ops = operational(records, start, end)
        backfilled = backfill_calibration(records)
        calibration.record_derivation({"ts": entry["ts"], "window": entry["window"],
                                       "derived": derived, "operational": ops})
    except d.DerivationRefused as e:
        entry.update(status="refused", error=str(e))
        _finish(entry, t0, log_path)
        raise
    entry.update(status="ok", judgments_processed=derived["n_judgments"],
                 trace_records=len(records), seats=len(derived["seats"]),
                 ops_seats=len(ops["seats"]), calibration_backfilled=len(backfilled),
                 alerts=len(derived["alerts"]) + len(ops["alerts"]))
    return _finish(entry, t0, log_path)


def _finish(entry, t0, log_path):
    duration = time.perf_counter() - t0
    cost = duration * USD_PER_SECOND
    entry.update(duration_s=round(duration, 4), cost_usd=round(cost, 8),
                 cost_flag=cost > COST_CEILING_USD)
    if entry["cost_flag"]:
        print(f"FLAG: run cost ${cost:.4f} exceeds ceiling ${COST_CEILING_USD}",
              file=sys.stderr)
    _log(entry, log_path)
    return entry


def misses(hours, now=None, log_path=None):
    """Hourly slots in the trailing `hours` with no ok run in the log."""
    now = now or datetime.now(timezone.utc)
    path = Path(log_path or LOG_PATH)
    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()] \
        if path.exists() else []
    slots = {_ts(r["ts"]).replace(minute=0, second=0, microsecond=0)
             for r in rows if r.get("status") == "ok"}
    top = now.replace(minute=0, second=0, microsecond=0)
    want = [top - timedelta(hours=h) for h in range(hours)]
    missing = [s.isoformat() for s in want if s not in slots]
    return {"hours": hours, "runs": len(want) - len(missing), "misses": missing}


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="runner")
    ap.add_argument("cmd", nargs="?", default="run", choices=["run", "misses"])
    ap.add_argument("--trace")
    ap.add_argument("--ticks", type=int, default=1)
    ap.add_argument("--every", type=float, default=0.0, help="seconds between ticks")
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--log", help="run log path (default logs/runner.jsonl)")
    ap.add_argument("--cal-dir", help="calibration store dir (default calibration/)")
    a = ap.parse_args(argv)
    if a.cal_dir:
        calibration.CAL_DIR = Path(a.cal_dir)
    if a.cmd == "misses":
        out = misses(a.hours, log_path=a.log)
        print(json.dumps(out, indent=2))
        return 1 if out["misses"] else 0
    try:
        for i in range(a.ticks):
            if i:
                time.sleep(a.every)
            print(json.dumps(run_once(a.trace, log_path=a.log)))
    except d.DerivationRefused as err:
        print(f"REFUSED: {err}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
