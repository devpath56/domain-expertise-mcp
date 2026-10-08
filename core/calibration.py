"""RPD calibration loop per seat.

Append-only calibration.jsonl per seat. Each entry links a judgment to the
resolution of its falsifiable test. Summary is computed, never stored:
counts, pass rate, avg clarification rounds. No rates are published below
n=30 (operator commandment: count, don't rate until n>=30).
"""
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CAL_DIR = ROOT / "calibration"
MIN_N_FOR_RATE = 30


def _path(seat_id: str) -> Path:
    CAL_DIR.mkdir(parents=True, exist_ok=True)
    return CAL_DIR / f"{seat_id}.jsonl"


def record(
    seat_id: str,
    judgment_id: str,
    test_id: str,
    outcome: str,
    clarification_rounds: int = 0,
    evidence: str = "",
) -> dict:
    """outcome in: resolved_pass | resolved_fail | expired."""
    entry = {
        "seat": seat_id,
        "judgment_id": judgment_id,
        "test_id": test_id,
        "ts": datetime.now(timezone.utc).isoformat(),
        "outcome": outcome,
        "clarification_rounds": clarification_rounds,
        "evidence": evidence[:500],
    }
    with open(_path(seat_id), "a") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def read(seat_id: str) -> list:
    p = _path(seat_id)
    if not p.exists():
        return []
    out = []
    with open(p) as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def summary(seat_id: str) -> dict:
    entries = read(seat_id)
    resolved = [e for e in entries if e["outcome"] in ("resolved_pass", "resolved_fail")]
    passed = [e for e in resolved if e["outcome"] == "resolved_pass"]
    n = len(resolved)
    rounds = [e.get("clarification_rounds", 0) for e in entries]
    return {
        "seat": seat_id,
        "judgments": len(entries),
        "resolved": n,
        "passed": len(passed),
        "failed": n - len(passed),
        "expired": sum(1 for e in entries if e["outcome"] == "expired"),
        "pass_rate": (len(passed) / n) if n >= MIN_N_FOR_RATE else None,
        "pass_rate_note": (
            None
            if n >= MIN_N_FOR_RATE
            else f"n={n} < {MIN_N_FOR_RATE}: counts only, no rate published"
        ),
        "avg_clarification_rounds": (sum(rounds) / len(rounds)) if rounds else 0.0,
    }


def summary_all(seat_ids: list) -> dict:
    return {sid: summary(sid) for sid in seat_ids}


# --- derived snapshots (jobs/runner.py) ---
# One row per runner pass: the derive() output plus operational metrics.
# Kept beside the per-seat files, never inside them, so summary() still
# reads test resolutions only.

def _runs_path(cal_dir: Path | None = None) -> Path:
    d = (cal_dir or CAL_DIR) / "runs"
    d.mkdir(parents=True, exist_ok=True)
    return d / "derived.jsonl"


def record_derivation(snapshot: dict, cal_dir: Path | None = None) -> dict:
    """Append one derived snapshot. Append-only, like the seat files."""
    with open(_runs_path(cal_dir), "a") as f:
        f.write(json.dumps(snapshot, default=str) + "\n")
    return snapshot


def read_derivations(cal_dir: Path | None = None) -> list:
    p = _runs_path(cal_dir)
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []
