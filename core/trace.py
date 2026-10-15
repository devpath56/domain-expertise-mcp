"""Trace emission: every judgment leaves records.

Record types: Judgment, Turn, MachineRun, GroundingLink, Intuition, Test.
One JSONL file, one record per line, append-only. No judgment may return
without its trace records — the server enforces this in the judge path,
and tests/test_server.py asserts it.
"""
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRACE_PATH = ROOT / "trace" / "trace.jsonl"

# Secret redaction on the write path (D10). Deterministic pattern list, no
# ML, no retroactive scrub: a record is redacted once, before it lands.
REDACTED = "[REDACTED]"
SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),                 # AWS access key id
    re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}"),         # Anthropic / OpenAI keys
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),                 # GitHub tokens
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),                 # Slack tokens
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),                      # Google API key
    re.compile(r"\b(?:sk|rk)_live_[0-9A-Za-z]{16,}\b"),            # Stripe live keys
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),  # JWT
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"(?i)\b(?:api[_-]?key|secret|token|password|passwd)\b(\s*[:=]\s*)[\"']?[^\s\"',;]{8,}"),
]


def redact(value):
    """Replace credential-shaped substrings with [REDACTED], recursively."""
    if isinstance(value, str):
        for pat in SECRET_PATTERNS:
            value = pat.sub(REDACTED, value)
        return value
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


# Version stamp (D15): every record carries seat id + its SKILL.md version.
# SKILL.md has no version field today, so the version is the content hash;
# a frontmatter `version:` wins when one is added.
_STAMPS: dict = {}  # judgment_id -> {"seat", "skill_version"}, set by emit_judgment


def skill_version(seat_id: str) -> str:
    p = ROOT / "seats" / seat_id / "SKILL.md"
    if not p.exists():
        return "missing"
    text = p.read_text()
    m = re.match(r"---\n(.*?)\n---", text, re.S)
    if m:
        v = re.search(r"^version:\s*(\S+)", m.group(1), re.M)
        if v:
            return v.group(1)
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()[:12]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write(record: dict, path: Path | None = None) -> dict:
    path = path or TRACE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = _STAMPS.get(record.get("judgment_id"))
    if stamp:
        record = {**stamp, **record}
    record = redact(record)
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")
    return record


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def emit_judgment(seat_id: str, inputs: dict, path: Path | None = None,
                  consumer: str | None = None) -> dict:
    """consumer is the per-consumer cost-attribution key, persisted verbatim.
    None (legacy callers) writes no consumer field."""
    jid = new_id("judg")
    _STAMPS[jid] = {"seat": seat_id, "skill_version": skill_version(seat_id)}
    record = {
        "type": "Judgment",
        "id": jid,
        "judgment_id": jid,  # self-referential: every related record is queryable by judgment_id
        "seat": seat_id,
        "ts": _now(),
        "inputs": inputs,
    }
    if consumer is not None:
        record["consumer"] = consumer
    return _write(record, path)


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
    consumer: str | None = None,
) -> dict:
    """Falsifiable test emitted by a judgment. States:
    delivered -> pending -> resolved_pass | resolved_fail | expired.
    consumer rides on the Test so resolve_test can refuse demo traffic."""
    record = {
        "type": "Test",
        "id": new_id("test"),
        "judgment_id": judgment_id,
        "seat": seat_id,
        "ts": _now(),
        "statement": statement,
        "resolve_rule": resolve_rule,
        "state": "delivered",
    }
    if consumer is not None:
        record["consumer"] = consumer
    return _write(record, path)


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
