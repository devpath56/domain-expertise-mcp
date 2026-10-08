"""Runner redproofs (mcp-runner-wire-20261007, slice wire.runner).

R1 manual run < 1s, writes the calibration store · R2 operational metrics
for a seat with trace data · R3 24 hourly runs, 0 misses (compressed) ·
R4 state sidecar emitted and validated · plus: no LLM / no network, the
backfill is idempotent, a missing trace is refused and logged, cost flag.

Every run emits tests/state/wire.runner-state.json (state-emission/v1):
verdict=done iff every check passed, which includes all metrics computed
and the store written.

Run: python3 tests/test_runner.py
"""
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "jobs"))

from core import calibration  # noqa: E402
from core import derivation as d  # noqa: E402
import runner  # noqa: E402

STATE_PATH = ROOT / "tests" / "state" / "wire.runner-state.json"
SPEC_ID, SLICE_ID = "mcp-metric-loop-spec-20261007", "wire.runner"
PASS, FAIL, CHECKS = [], [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    CHECKS.append({"check": name, "passed": bool(cond), "detail": str(detail)[:300]})
    print(("ok  " if cond else "FAIL") + f" {name}" + (f" — {detail}" if detail and not cond else ""))


TMP = Path(tempfile.mkdtemp(prefix="runner-test-"))
calibration.CAL_DIR = TMP / "calibration"   # never the repo's store
runner.LOG_PATH = TMP / "logs" / "runner.jsonl"
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


def ts(sec, base=NOW - timedelta(days=1)):
    return (base + timedelta(seconds=sec)).isoformat()


def call(jid, seat="s", sec=0, ms=10, tokens=(40, 20), billed=None, complete=True,
         test_state="delivered"):
    """One governed-path call as the server writes it."""
    t0 = NOW - timedelta(days=1) + timedelta(seconds=sec)
    at = lambda k: (t0 + timedelta(milliseconds=k)).isoformat()  # noqa: E731
    rows = [{"type": "Judgment", "id": jid, "judgment_id": jid, "seat": seat,
             "ts": at(0), "inputs": {"q": jid}}]
    if not complete:
        return rows
    rows += [{"type": "MachineRun", "id": f"m_{jid}", "judgment_id": jid, "ts": at(1),
              "verdict": "OK", "checks": []},
             {"type": "Test", "id": f"t_{jid}", "judgment_id": jid, "seat": seat,
              "ts": at(2), "state": "delivered"}]
    if tokens is not None:
        rows.append({**rows[0], "ts": at(ms), "tokens": {
            "emission": {"prompt": tokens[0], "completion": tokens[1]},
            "billed": billed, "schema_version": 1}})
    if test_state != "delivered":
        rows.append({**rows[2], "ts": at(ms + 1), "state": test_state, "evidence": "e"})
    return rows


def write_trace(rows, name="trace.jsonl"):
    p = TMP / name
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return p


# ---------------------------------------------------------------- R1
real = TMP / "real-trace.jsonl"
shutil.copy(ROOT / "trace" / "trace.jsonl", real)
before = (ROOT / "trace" / "trace.jsonl").read_bytes()
t0 = time.perf_counter()
e = runner.run_once(real, now=NOW)
wall = time.perf_counter() - t0
check("R1 manual run on current data completes", e["status"] == "ok", e)
check("R1 run < 1s", wall < 1.0, f"{wall:.4f}s")
snaps = calibration.read_derivations()
check("R1 snapshot written to calibration store", len(snaps) == 1
      and snaps[0]["derived"]["n_judgments"] == e["judgments_processed"], len(snaps))
check("R1 run log carries ts, duration, judgments, cost",
      all(k in e for k in ("ts", "duration_s", "judgments_processed", "cost_usd")), e)
check("R1 runner never writes the trace store",
      (ROOT / "trace" / "trace.jsonl").read_bytes() == before)

# ---------------------------------------------------------------- R2
ops = snaps[0]["operational"]["seats"]
with_data = [s for s, v in ops.items() if v["latency_ms"]["n"] and v["tokens_per_call"]["emission"]["n"]]
check("R2 operational metrics present for >=1 seat with trace data", bool(with_data), list(ops))
dt = ops.get("defect-triage", {})
check("R2 defect-triage: latency, tokens/call, error rate, success/failure all derived",
      dt.get("latency_ms", {}).get("n", 0) > 0
      and dt.get("tokens_per_call", {}).get("emission", {}).get("total", 0) > 0
      and dt.get("error_rate", {}).get("den") == dt.get("calls")
      and sum(dt.get("tool_calls", {}).values()) == dt.get("calls"), dt)

# ------------------------------------------- operational: exact on synthetic
rows = []
for i in range(30):
    rows += call(f"a{i}", seat="big", sec=i, ms=10 + i, tokens=(40 + i, 20), billed=100)
rows += call("p1", seat="big", sec=40, complete=False)          # raised mid-path
rows += call("legacy", seat="big", sec=41, tokens=None)          # pre-row-43 record
rows += call("z", seat="small", sec=50, tokens=(0, 5))           # D9 zero tokens
o = runner.operational(rows, NOW - timedelta(days=7), NOW)
big = o["seats"]["big"]
check("ops: partial call is a failure, token-less legacy call is not",
      big["tool_calls"] == {"success": 31, "failure": 1}, big["tool_calls"])
check("ops: error rate published at n>=30", big["error_rate"]["value"] == round(1 / 32, 4),
      big["error_rate"])
check("ops: latency p50/p95 from record timing (ms)",
      big["latency_ms"]["n"] == 31 and big["latency_ms"]["p50"] is not None
      and big["latency_ms"]["p95"] >= big["latency_ms"]["p50"], big["latency_ms"])
check("ops: tokens/call over annotated calls, missing annotation counted",
      big["tokens_per_call"]["emission"]["n"] == 30
      and big["tokens_per_call"]["emission"]["missing"]["count"] == 2
      and big["tokens_per_call"]["emission"]["mean"] == round(sum(60 + i for i in range(30)) / 30, 2),
      big["tokens_per_call"]["emission"])
small = o["seats"]["small"]
check("ops: counts only below n=30 (no rate, no percentile)",
      small["error_rate"]["value"] is None and small["latency_ms"]["p50"] is None, small)
check("ops: 0 tokens alerted, never a data point (D9)",
      any(a["kind"] == "zero_tokens" and a["judgment_id"] == "z" for a in o["alerts"])
      and small["tokens_per_call"]["emission"]["n"] == 0, o["alerts"])

# ------------------------------------------------ backfill is idempotent
bf = write_trace(call("r1", seat="bf", test_state="resolved_pass")
                 + call("r2", seat="bf", sec=5), "bf.jsonl")
e1 = runner.run_once(bf, now=NOW)
e2 = runner.run_once(bf, now=NOW)
cal = calibration.read("bf")
check("backfill: resolved Test missing from calibration is recorded once",
      e1["calibration_backfilled"] == 1 and e2["calibration_backfilled"] == 0
      and [c["test_id"] for c in cal] == ["t_r1"], (e1, e2, cal))

# ------------------------------------------------ refusal and cost flag
log_before = len(runner.LOG_PATH.read_text().splitlines())
try:
    runner.run_once(TMP / "absent.jsonl", now=NOW)
    refused = None
except d.DerivationRefused as err:
    refused = str(err)
last = json.loads(runner.LOG_PATH.read_text().splitlines()[-1])
check("refusal: missing trace raises and is logged, nothing written",
      refused and last["status"] == "refused"
      and len(runner.LOG_PATH.read_text().splitlines()) == log_before + 1
      and len(calibration.read_derivations()) == 3, last)
rate = runner.USD_PER_SECOND
runner.USD_PER_SECOND = 1e6
flagged = runner.run_once(bf, now=NOW)
runner.USD_PER_SECOND = rate
check("cost: a run above $0.01 is flagged", flagged["cost_flag"] is True, flagged)
check("cost: a real run is under $0.01", e["cost_flag"] is False and e["cost_usd"] < 0.01, e)

# ------------------------------------------------ no LLM, no network
src = (ROOT / "jobs" / "runner.py").read_text()
banned = [w for w in ("urllib", "subprocess", "anthropic", "judge_from_env",
                      "run_verdict_justified", "requests") if w in src]
check("script only: runner names no LLM or network path", not banned, banned)
_sock = socket.socket


class _NoNet(socket.socket):
    def __init__(self, *a, **k):
        raise AssertionError("runner opened a socket")


socket.socket = _NoNet
try:
    net_ok = runner.run_once(real, now=NOW)["status"] == "ok"
except AssertionError as err:
    net_ok = str(err)
finally:
    socket.socket = _sock
check("script only: a pass opens no socket", net_ok is True, net_ok)

# ---------------------------------------------------------------- R3
runner.LOG_PATH = TMP / "logs" / "hourly.jsonl"
base = NOW.replace(minute=0) - timedelta(hours=23)
for h in range(24):
    runner.run_once(real, now=base + timedelta(hours=h, minutes=0, seconds=3))
m = runner.misses(24, now=NOW + timedelta(minutes=5))
check("R3 24 hourly runs, 0 misses (compressed schedule)",
      m["runs"] == 24 and m["misses"] == [], m)
lines = runner.LOG_PATH.read_text().splitlines()
runner.LOG_PATH.write_text("\n".join(lines[:5] + lines[6:]) + "\n")
m2 = runner.misses(24, now=NOW + timedelta(minutes=5))
check("R3 the miss check sees a dropped hour (red proof)", len(m2["misses"]) == 1, m2)
cli_log = TMP / "cli" / "runner.jsonl"
cli = subprocess.run([sys.executable, str(ROOT / "jobs" / "runner.py"), "--trace", str(real),
                      "--ticks", "3", "--every", "0.2", "--log", str(cli_log),
                      "--cal-dir", str(TMP / "cli" / "calibration")],
                     capture_output=True, text=True, cwd=TMP, env={"PATH": "/usr/bin:/bin"})
check("R3 the CLI loop runs 3 real ticks into the given log and store",
      cli.returncode == 0 and len(cli_log.read_text().splitlines()) == 3
      and len(calibration.read_derivations(TMP / "cli" / "calibration")) == 3,
      cli.stderr[-300:])

cron = (ROOT / "jobs" / "runner.cron").read_text()
check("schedule: hourly cron line calls the runner",
      any(l.startswith("0 * * * * ") and "jobs/runner.py" in l for l in cron.splitlines()), cron)

# ---------------------------------------------------------------- R4
def head():
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                             text=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "jobs", "core", "tests"],
                               cwd=ROOT, capture_output=True, text=True).stdout.strip()
        return sha + ("+dirty" if dirty else "") if sha else "unknown"
    except OSError:
        return "unknown"


def emit_state(checks):
    state = {
        "emitter_kind": "test-suite", "emitter_version": head(),
        "emitted_at": datetime.now(timezone.utc).isoformat(),
        "spec_id": SPEC_ID, "slice_id": SLICE_ID, "schema": "state-emission/v1",
        "verdict": "done" if checks and all(c["passed"] for c in checks) else "not_done",
        "checks": checks,
        "evidence": {"suite": "tests/test_runner.py", "passed": len(PASS), "failed": len(FAIL)},
    }
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state) + "\n")
    return state


def validate_state(path):
    """VERIFIED iff the sidecar is state-emission/v1 and its verdict follows
    from its checks. Returns (VERIFIED|REJECTED, reason)."""
    try:
        s = json.loads(Path(path).read_text())
    except (OSError, ValueError) as err:
        return "REJECTED", f"unreadable: {err}"
    need = ("emitter_kind", "emitter_version", "emitted_at", "spec_id", "slice_id",
            "schema", "verdict", "checks", "evidence")
    if [k for k in need if k not in s]:
        return "REJECTED", f"missing {[k for k in need if k not in s]}"
    if s["schema"] != "state-emission/v1" or s["slice_id"] != SLICE_ID:
        return "REJECTED", "wrong schema or slice"
    if not s["checks"] or not all(isinstance(c.get("passed"), bool) for c in s["checks"]):
        return "REJECTED", "checks empty or unbooleaned"
    want = "done" if all(c["passed"] for c in s["checks"]) else "not_done"
    if s["verdict"] != want:
        return "REJECTED", f"verdict {s['verdict']} does not follow from checks ({want})"
    return "VERIFIED", s["verdict"]


# red proof: a sidecar whose verdict lies about its checks is rejected
bad = TMP / "bad-state.json"
bad.write_text(json.dumps({**emit_state([{"check": "x", "passed": False, "detail": ""}]),
                           "verdict": "done"}))
check("R4 validator rejects a done verdict over a failed check",
      validate_state(bad)[0] == "REJECTED", validate_state(bad))
check("R4 metrics computed + store written gate the verdict",
      all(c["passed"] for c in CHECKS if c["check"].startswith(("R1", "R2"))))
emit_state(list(CHECKS))
v, why = validate_state(STATE_PATH)
check("R4 sidecar emitted and validated", v == "VERIFIED" and why == "done", (v, why))
final = emit_state(list(CHECKS))   # re-emit so the sidecar carries the R4 checks too
v, why = validate_state(STATE_PATH)
print(f"sidecar {STATE_PATH.relative_to(ROOT)}: {v} ({why})")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL or v != "VERIFIED" else 0)
