"""Trace emission: every judgment leaves records.

Record types: Judgment, Turn, MachineRun, GroundingLink, Intuition, Test.
One JSONL file, one record per line, append-only. No judgment may return
without its trace records — the server enforces this in the judge path,
and tests/test_server.py asserts it.
"""
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRACE_PATH = ROOT / "trace" / "trace.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write(record: dict, path: Path | None = None) -> dict:
    path = path or TRACE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")
    return record


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def emit_judgment(seat_id: str, inputs: dict, path: Path | None = None) -> dict:
    jid = new_id("judg")
    return _write(
        {
            "type": "Judgment",
            "id": jid,
            "judgment_id": jid,  # self-referential: every related record is queryable by judgment_id
            "seat": seat_id,
            "ts": _now(),
            "inputs": inputs,
        },
        path,
    )


def emit_turn(judgment_id: str, role: str, content: str, path: Path | None = None) -> dict:
    return _write(
        {
            "type": "Turn",
            "id": new_id("turn"),
            "judgment_id": judgment_id,
            "ts": _now(),
            "role": role,
            "content": content[:2000],
        },
        path,
    )


def emit_machine_run(
    judgment_id: str, machine: str, verdict: str, checks: list, path: Path | None = None
) -> dict:
    return _write(
        {
            "type": "MachineRun",
            "id": new_id("mach"),
            "judgment_id": judgment_id,
            "ts": _now(),
            "machine": machine,
            "verdict": verdict,
            "checks": checks,
        },
        path,
    )


def emit_grounding_link(
    judgment_id: str, source: str, ref: str, path: Path | None = None
) -> dict:
    return _write(
        {
            "type": "GroundingLink",
            "id": new_id("grnd"),
            "judgment_id": judgment_id,
            "ts": _now(),
            "source": source,
            "ref": ref,
        },
        path,
    )


def emit_intuition(
    judgment_id: str, statement: str, path: Path | None = None
) -> dict:
    """Preregistered intuition: the caller's hypothesis BEFORE the outcome
    is observed. Status becomes validated / falsified / expired on resolve."""
    return _write(
        {
            "type": "Intuition",
            "id": new_id("intu"),
            "judgment_id": judgment_id,
            "ts": _now(),
            "statement": statement,
            "status": "preregistered",
        },
        path,
    )


def emit_test(
    judgment_id: str,
    seat_id: str,
    statement: str,
    resolve_rule: str,
    path: Path | None = None,
) -> dict:
    """Falsifiable test emitted by a judgment. States:
    delivered -> pending -> resolved_pass | resolved_fail | expired."""
    return _write(
        {
            "type": "Test",
            "id": new_id("test"),
            "judgment_id": judgment_id,
            "seat": seat_id,
            "ts": _now(),
            "statement": statement,
            "resolve_rule": resolve_rule,
            "state": "delivered",
        },
        path,
    )


def read_all(path: Path | None = None) -> list:
    path = path or TRACE_PATH
    if not path.exists():
        return []
    records = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def query(path: Path | None = None, **filters) -> list:
    """Filter trace records by exact field match, e.g. query(type='Test', seat='crash-triage')."""
    out = []
    for r in read_all(path):
        if all(r.get(k) == v for k, v in filters.items()):
            out.append(r)
    return out
