"""Thin-slice tests for the Domain Expertise MCP server.

Run: .venv/bin/python tests/test_server.py
"""
import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import calibration, registry, trace  # noqa: E402
import server  # noqa: E402

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("ok  " if cond else "FAIL") + f" {name}" + (f" — {detail}" if detail and not cond else ""))


def isolate_tmp():
    tmp = Path(tempfile.mkdtemp())
    trace.TRACE_PATH = tmp / "trace.jsonl"
    calibration.CAL_DIR = tmp / "calibration"
    return tmp


INPUTS = {
    "crash-triage": {"signature": "NPE in payments-service on retry path"},
    "metric-design": {"metric": "crash count", "decision": "whether to roll back the deploy"},
    "qe-bar": {
        "acceptance_criteria": ["retry succeeds within 2s for 100/100 attempts against expected output"],
        "fix_description": "adds idempotency key",
    },
    "harness-review": {
        "holds": ["5 tool definitions, tier 1"],
        "reaches": ["sandbox exec via documented seam"],
        "missing_piece_behavior": "run fails loudly with MISSING_TOOL error; spend cap enforced in meter.py",
    },
    "incident-lens": {
        "failure_account": "deploy went out with the flag off",
        "proposed_guard": "add a pre-deploy check in deploy.py that refuses when the flag is unset",
    },
    "statistical-rigor": {
        "claim": "fix reduced crash rate by 40%",
        "n": 120,
        "denominator": "deploys of payments-service in october",
        "claim_population": "deploys of payments-service in october",
    },
    "allspaw-debugassist": {
        "incident": {"description": "payments NPE on retry path seen in prod"},
        "fix": {"summary": "added idempotency key check"},
    },
    "qe-ic-debugassist": {
        "contract": "debugassist.fix-validation/1",
        "verdict": "PASS",
        "check": "repro fails before, passes after",
        "flip_condition": "repro passes before fix",
    },
    "defect-triage": {
        "issue": {"title": "crash", "body": "app crashes on login"},
        "repo": {"name": "myapp"},
        "is_defect": 0.85, "confidence": 0.9,
    },
    "cause-locator": {
        "issue": {"description": "login crash", "repro_output": "File src/auth.py line 42"},
        "repo": {"listing": ["src/auth.py", "src/login.py"]},
        "candidates": [{"path": "src/auth.py", "lines": "40-45", "reason": "stack frame",
                        "confidence": 0.8, "confirm_if": "x", "eliminate_if": "y"}],
    },
}


def test_registry_is_data_not_code():
    reg = registry.load_registry()
    check("registry loads seats", len(reg["seats"]) >= 6, f"got {len(reg['seats'])}")
    ids = {s["id"] for s in reg["seats"]}
    check("expected seat ids", ids == set(INPUTS), f"got {ids}")
    src = (ROOT / "server.py").read_text()
    hardcoded = [sid for sid in INPUTS if f'"{sid}"' in src or f"'{sid}'" in src]
    check("no seat ids hardcoded in server.py", not hardcoded, f"found {hardcoded}")


def test_judge_emits_full_trace():
    isolate_tmp()
    for seat in registry.load_registry()["seats"]:
        sid = seat["id"]
        out = server._judge(seat, dict(INPUTS[sid]))
        jid = out["judgment_id"]
        recs = trace.query(judgment_id=jid)
        types = {r["type"] for r in recs}
        need = {"Judgment", "Turn", "MachineRun", "GroundingLink", "Test"}
        check(f"{sid}: trace has {sorted(need)}", need <= types, f"got {sorted(types)}")
        check(f"{sid}: verdict present", bool(out.get("verdict")), str(out.get("verdict")))
        check(f"{sid}: falsifiable test emitted", bool(out.get("falsifiable_test", {}).get("test_id")))
        # no judgment without trace: every returned judgment_id resolves in the log
        check(f"{sid}: judgment traceable", any(
            r["type"] == "Judgment" and r["id"] == jid for r in recs))


def test_intuition_preregistration():
    isolate_tmp()
    seat = registry.get_seat(registry.load_registry(), "crash-triage")
    out = server._judge(seat, {**INPUTS["crash-triage"],
                               "intuition": "retry-storm before seeing the trace"})
    check("intuition preregistered", out["intuition_id"] is not None)
    intu = trace.query(type="Intuition", judgment_id=out["judgment_id"])
    check("intuition status preregistered",
          intu and intu[0]["status"] == "preregistered")


def test_resolve_updates_calibration():
    isolate_tmp()
    seat = registry.get_seat(registry.load_registry(), "crash-triage")
    out = server._judge(seat, dict(INPUTS["crash-triage"]))
    tid = out["falsifiable_test"]["test_id"]
    res = asyncio.run(server.resolve_test(tid, "resolved_pass", evidence="fix touched retry code"))
    check("resolve_test returns new state", res["state"] == "resolved_pass")
    cal = asyncio.run(server.get_calibration("crash-triage"))
    s = cal["crash-triage"]
    check("calibration counts the judgment", s["judgments"] == 1, str(s))
    check("pass rate suppressed below n=30", s["pass_rate"] is None and s["resolved"] == 1)


def test_machine_verdicts_are_sane():
    isolate_tmp()
    reg = registry.load_registry()
    outs = {s["id"]: server._judge(s, dict(INPUTS[s["id"]])) for s in reg["seats"]}
    check("crash-triage MATCHED", outs["crash-triage"]["verdict"] == "MATCHED")
    check("metric-design NEEDS_DENOMINATOR", outs["metric-design"]["verdict"] == "NEEDS_DENOMINATOR")
    check("qe-bar SHIP", outs["qe-bar"]["verdict"] == "SHIP")
    check("harness-review HELD", outs["harness-review"]["verdict"] == "HELD")
    check("incident-lens CONDITION", outs["incident-lens"]["verdict"] == "CONDITION")
    check("statistical-rigor ANSWERABLE", outs["statistical-rigor"]["verdict"] == "ANSWERABLE")


def test_tools_registered():
    async def go():
        tools = await server.mcp.list_tools()
        return {t.name for t in tools}
    names = asyncio.run(go())
    expected = {s["id"].replace("-", "_") for s in registry.load_registry()["seats"]}
    expected |= {"resolve_test", "get_calibration", "query_trace"}
    check("9 tools registered", names == expected, f"got {sorted(names)}")


def test_query_trace_filters():
    isolate_tmp()
    seat = registry.get_seat(registry.load_registry(), "incident-lens")
    out = server._judge(seat, dict(INPUTS["incident-lens"]))
    res = asyncio.run(server.query_trace(record_type="Test", seat="incident-lens"))
    check("query_trace filters", res["count"] >= 1 and
          all(r["seat"] == "incident-lens" for r in res["records"]))


if __name__ == "__main__":
    test_registry_is_data_not_code()
    test_judge_emits_full_trace()
    test_intuition_preregistration()
    test_resolve_updates_calibration()
    test_machine_verdicts_are_sane()
    test_tools_registered()
    test_query_trace_filters()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)
