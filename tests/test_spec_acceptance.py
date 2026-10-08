"""Spec-derived acceptance tests for the Domain Expertise MCP server.

Derived from ~/workspace/specs/trace-infrastructure-rpd-calibration-spec.md.
These test what the SPEC demands, not what the builder asserted.

Run: python3 tests/test_spec_acceptance.py
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import calibration, registry, trace

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("ok  " if cond else "FAIL") + f" {name}" + (f" — {detail}" if detail and not cond else ""))


def isolate_tmp():
    tmp = Path(tempfile.mkdtemp())
    trace.TRACE_PATH = tmp / "trace.jsonl"
    calibration.CAL_DIR = tmp / "calibration"
    calibration.CAL_DIR.mkdir(exist_ok=True)
    return tmp


def read_records(tmp):
    p = tmp / "trace.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


# --- Seat contracts ---

def _machine(seat_id):
    from core import registry as reg
    r = reg.load_registry()
    for s in r["seats"]:
        if s["id"] == seat_id:
            return s["machine_module"]
    raise KeyError(seat_id)


def test_allspaw_contract_shape():
    isolate_tmp()
    m = _machine("allspaw-debugassist")
    r = m.check({"query_id": "t1", "incident": {"description": ""}, "fix": {"summary": "x"}})
    check("allspaw: missing description -> DECLINED/MISSING_FIELD",
          r["state"] == "DECLINED" and r["reason"] == "MISSING_FIELD", str(r.get("state")))
    r = m.check({"query_id": "t2",
                 "incident": {"description": "payments service NPE on retry path, seen in prod logs"},
                 "fix": {"summary": "added idempotency key check before retry"}})
    check("allspaw: valid inputs -> READY_FOR_JUDGMENT",
          r["state"] == "READY_FOR_JUDGMENT" and r["contract"] == "allspaw.debugassist.v1", str(r.get("state")))
    check("allspaw: returns no-blame rules",
          any("blame" in rule for rule in r.get("rules", [])), str(r.get("rules")))


def test_defect_triage_decision_order():
    isolate_tmp()
    m = _machine("defect-triage")
    base = {"query_id": "t1",
            "issue": {"title": "crash on login", "body": "app crashes when logging in with SSO"},
            "repo": {"name": "myapp"}}
    r = m.check({**base, "is_defect": 0.9, "confidence": 0.4})
    check("defect-triage: low confidence + high is_defect -> NEEDS_PERSON (order matters)",
          r["state"] == "NEEDS_PERSON", str(r.get("state")))
    r = m.check({**base, "is_defect": 0.3, "confidence": 0.8})
    check("defect-triage: high conf + low is_defect -> NOT_A_DEFECT",
          r["state"] == "NOT_A_DEFECT", str(r.get("state")))
    r = m.check({**base, "is_defect": 0.8, "confidence": 0.9})
    check("defect-triage: high conf + high is_defect -> DEFECT",
          r["state"] == "DEFECT", str(r.get("state")))


def test_cause_locator_path_rule():
    isolate_tmp()
    m = _machine("cause-locator")
    listing = ["src/auth.py", "src/login.py", "src/db.py"]
    base = {"query_id": "t1",
            "issue": {"description": "login crash", "repro_output": "Traceback: File src/auth.py line 42"},
            "repo": {"listing": listing}}
    r = m.check({**base, "candidates": [
        {"path": "src/madeup.py", "reason": "guess", "confidence": 0.9},
        {"path": "src/auth.py", "lines": "40-45", "reason": "stack frame", "confidence": 0.8,
         "confirm_if": "x", "eliminate_if": "y"},
    ]})
    paths = [c["path"] for c in r.get("candidates", [])]
    check("cause-locator: invented path dropped, only listing members survive",
          r["state"] == "CANDIDATES" and paths == ["src/auth.py"], str(paths))
    r = m.check({**base, "candidates": [
        {"path": "src/login.py", "reason": "nearby", "confidence": 0.9},
    ], "confidence_bar": 0.6})
    check("cause-locator: file-level capped at 0.5, below bar -> CAUSE_NOT_FOUND",
          r["state"] == "CAUSE_NOT_FOUND" and r["code"] == "BELOW_BAR",
          f"{r.get('state')}/{r.get('code')}")


def test_qeic_no_vibes():
    isolate_tmp()
    m = _machine("qe-ic-debugassist")
    r = m.check({"query_id": "t1", "contract": "debugassist.fix-validation/1",
                 "verdict": "PASS", "check": "looks good", "flip_condition": "should work"})
    check("qe-ic: vibes PASS -> FAIL",
          r["state"] == "FAIL" and r.get("vibes_found"), str(r.get("state")))
    r = m.check({"query_id": "t2", "contract": "debugassist.fix-validation/1",
                 "verdict": "PASS", "check": "repro test fails before, passes after",
                 "flip_condition": "repro test passes before the fix"})
    check("qe-ic: named check + flip -> PASS",
          r["state"] == "PASS", str(r.get("state")))
    r = m.check({"query_id": "t3", "contract": "debugassist.test-soundness/1",
                 "verdict": "SOUND", "check": "LGTM", "flip_condition": ""})
    check("qe-ic: vibes SOUND -> UNEVALUABLE",
          r["state"] == "UNEVALUABLE", str(r.get("state")))


# --- Spec §5 RPD loop: query -> intuition -> judgment + test -> trace records ---

def test_rpd_loop_emits_trace_records():
    tmp = isolate_tmp()
    # 1. Intuition preregistered
    jid = "judg_test1"
    trace.emit_intuition(jid, "pattern: retry-storm NPE, confidence 0.7")
    # 2. Judgment
    trace.emit_judgment("defect-triage", {"issue": "crash"})
    # 3. Test emitted with the judgment
    trace.emit_test(jid, "defect-triage", "is_defect > 0.5", "repro shows defect")
    recs = read_records(tmp)
    types = {r["type"] for r in recs}
    check("rpd: intuition record emitted", "Intuition" in types, str(types))
    check("rpd: judgment record emitted", "Judgment" in types, str(types))
    check("rpd: test record emitted with judgment", "Test" in types, str(types))


def test_machine_run_recorded():
    tmp = isolate_tmp()
    trace.emit_machine_run("j1", "seats.defect-triage.machine", "DEFECT", ["confidence_bar", "defect_bar"])
    recs = read_records(tmp)
    check("trace: machine run recorded",
          any(r["type"] == "MachineRun" for r in recs),
          str([r["type"] for r in recs]))


def test_registry_serves_new_seats():
    isolate_tmp()
    seats = registry.load_registry()["seats"]
    ids = {s["id"] for s in seats}
    for want in ("allspaw-debugassist", "qe-ic-debugassist", "defect-triage", "cause-locator"):
        check(f"registry: serves {want}", want in ids, f"missing from {sorted(ids)}")


def test_no_hardcoded_seats():
    src = (ROOT / "server.py").read_text()
    for sid in ("allspaw-debugassist", "defect-triage", "cause-locator", "crash-triage"):
        check(f"server: no hardcoded '{sid}'",
              sid not in src, "found in server.py")


if __name__ == "__main__":
    test_allspaw_contract_shape()
    test_defect_triage_decision_order()
    test_cause_locator_path_rule()
    test_qeic_no_vibes()
    test_rpd_loop_emits_trace_records()
    test_machine_run_recorded()
    test_registry_serves_new_seats()
    test_no_hardcoded_seats()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)
