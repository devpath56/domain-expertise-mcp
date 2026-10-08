"""Tests for the advise on-ramp (config/question_templates.yaml + core/advise.py).

One test per template: the built inputs satisfy the seat's INPUT_SCHEMA
required fields. No LLM needed. Every call here passes consumer="test" so
the advise_calls tripwire can exclude it.

Run: .venv/bin/python tests/test_advise.py
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import advise, calibration, registry, trace  # noqa: E402
import server  # noqa: E402

PASS = []
FAIL = []

# Isha's two DebugAssist questions, literal from debug_assist/advisors.py:
# is the second story blame-free (allspaw), is the guard a condition (qe-ic-advisor).
ISHA_Q1 = "Is this second story about conditions, never people? Name any sentence that blames a person, and any juncture the evidence does not support."
ISHA_Q2 = "Is this guard a condition (it runs by itself in CI) or an instruction (someone must remember it)? Which triggers of the bug's class does it miss?"
STORY = "Login crashes with a NullPointerException on the retry path after the session token expires."
GUARD = "Attempt 1: null-check the session token in auth.retry() before refreshing."


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("ok  " if cond else "FAIL") + f" {name}" + (f" — {detail}" if detail and not cond else ""))


def isolate_tmp():
    tmp = Path(tempfile.mkdtemp())
    trace.TRACE_PATH = tmp / "trace.jsonl"
    calibration.CAL_DIR = tmp / "calibration"
    advise.TELEMETRY_PATH = tmp / "telemetry" / "advise_calls.jsonl"
    return tmp


def missing_required(schema: dict, value, path="") -> list:
    """Required fields absent or empty, walked recursively through objects."""
    out = []
    for name in schema.get("required", []):
        if not isinstance(value, dict) or value.get(name) in (None, ""):
            out.append(f"{path}{name}")
    for name, sub in schema.get("properties", {}).items():
        if sub.get("type") == "object" and isinstance(value, dict) and isinstance(value.get(name), dict):
            out += missing_required(sub, value[name], f"{path}{name}.")
    return out


REPO_CTX = {"repo_name": "myrepo", "repo_listing": ["auth/retry.py", "auth/session.py"]}
LOCATE_CTX = {
    "repo_listing": ["auth/retry.py", "auth/session.py"],
    "repro_output": 'File "auth/retry.py", line 42, in refresh: AttributeError: NoneType has no attribute token',
    "candidates": [
        {"path": "auth/retry.py", "lines": "38-46", "reason": "top stack frame", "confidence": 0.8},
        {"path": "auth/invented.py", "lines": "1-9", "reason": "not in the listing", "confidence": 0.9},
    ],
}


def test_each_template_satisfies_its_seat_schema():
    reg = registry.load_registry()
    config = advise.load_templates()
    for t in config["templates"]:
        alias = t["alias"]
        seat_id, inputs = advise.build(alias, ISHA_Q1, STORY, context=REPO_CTX)
        seat = registry.get_seat(reg, seat_id)
        miss = missing_required(seat["machine_module"].INPUT_SCHEMA, inputs)
        check(f"{alias} -> {seat_id}: built inputs satisfy INPUT_SCHEMA required", not miss, f"missing {miss}")
        flat = json.dumps(inputs)
        check(f"{alias}: no placeholder left unsubstituted",
              "{question}" not in flat and "{evidence}" not in flat and "{context." not in flat)


def test_both_aliases_resolve():
    ids = {s["id"] for s in registry.load_registry()["seats"]}
    for alias, seat_id in advise.load_templates()["aliases"].items():
        check(f"alias {alias} resolves to a registered seat", seat_id in ids, seat_id)


def test_r1_allspaw_returns_judgment():
    isolate_tmp()
    out = asyncio.run(server.advise("allspaw", ISHA_Q1, STORY, consumer="test"))
    check("R1 advise(allspaw) returns a JudgmentResult", out.get("seat") == "allspaw-debugassist"
          and bool(out.get("judgment_id")) and bool(out.get("verdict")), str(out)[:300])


def test_r2_qeic_returns_judgment():
    isolate_tmp()
    out = asyncio.run(server.advise("qe-ic-advisor", ISHA_Q2, GUARD, consumer="test"))
    check("R2 advise(qe-ic-advisor) returns a JudgmentResult", out.get("seat") == "qe-ic-debugassist"
          and bool(out.get("judgment_id")) and bool(out.get("verdict")), str(out)[:300])


def test_r3_unknown_alias_errors_clearly():
    isolate_tmp()
    try:
        asyncio.run(server.advise("nonexistent", ISHA_Q1, STORY, consumer="test"))
        check("R3 unknown alias raises", False, "no error raised")
    except ValueError as e:
        msg = str(e)
        check("R3 unknown alias raises ValueError naming the alias and the known list",
              "nonexistent" in msg and "allspaw" in msg and "qe-ic-advisor" in msg, msg)


def test_four_aliases_known():
    check("aliases: all four DebugAssist seats", set(advise.load_templates()["aliases"]) ==
          {"allspaw", "qe-ic-advisor", "defect-triage", "cause-locator"})


def test_four_seats_r1_defect_triage_with_repo_name():
    isolate_tmp()
    out = asyncio.run(server.advise("defect-triage", "Is this a real defect?", STORY,
                                    consumer="test", context={"repo_name": "myrepo"}))
    mr = out.get("machine_result", {})
    check("four-seats R1 advise(defect-triage, context=repo_name) returns a JudgmentResult",
          out.get("seat") == "defect-triage" and bool(out.get("judgment_id")) and bool(out.get("verdict")), str(out)[:300])
    check("four-seats R1 no DECLINED for missing repo",
          out.get("verdict") != "DECLINED" and "repo.name" not in (mr.get("needs") or []), str(mr)[:300])


def test_four_seats_r2_defect_triage_without_context_errors():
    tmp = isolate_tmp()
    for ctx in (None, {}, {"repo_name": ""}):
        try:
            asyncio.run(server.advise("defect-triage", "Is this a real defect?", STORY, consumer="test", context=ctx))
            check(f"four-seats R2 context={ctx!r} raises", False, "no error raised")
        except ValueError as e:
            check(f"four-seats R2 context={ctx!r} raises naming context.repo_name", "context.repo_name" in str(e), str(e))
    rows = [json.loads(l) for l in (tmp / "telemetry" / "advise_calls.jsonl").read_text().splitlines()]
    check("four-seats R2 a missing-context call is logged unmatched", all(r["matched"] is False for r in rows), str(rows))


def test_four_seats_r3_cause_locator_candidates_from_listing():
    isolate_tmp()
    out = asyncio.run(server.advise("cause-locator", "Which file holds the cause?", STORY,
                                    consumer="test", context=LOCATE_CTX))
    mr = out.get("machine_result", {})
    paths = [c["path"] for c in mr.get("candidates", [])]
    check("four-seats R3 advise(cause-locator, context=repo_listing) returns CANDIDATES",
          out.get("seat") == "cause-locator" and out.get("verdict") == "CANDIDATES", str(out)[:300])
    check("four-seats R3 every candidate is a verbatim member of the listing (invented path dropped)",
          paths == ["auth/retry.py"], str(paths))


def test_four_seats_cause_locator_listing_passes_through_as_list():
    _, inputs = advise.build("cause-locator", "q", STORY, context={"repo_listing": ["a.py", "b.py"]})
    check("cause-locator repo.listing stays a list", inputs["repo"]["listing"] == ["a.py", "b.py"], str(inputs))
    check("cause-locator optional keys absent -> dropped, not defaulted",
          "repro_output" not in inputs["issue"] and "candidates" not in inputs, str(inputs))
    isolate_tmp()
    out = asyncio.run(server.advise("cause-locator", "q", STORY, consumer="test",
                                    context={"repo_listing": ["a.py", "b.py"]}))
    check("cause-locator with listing only: seat answers CAUSE_NOT_FOUND naming its need (no silent candidates)",
          out.get("verdict") == "CAUSE_NOT_FOUND" and out["machine_result"].get("code") == "NO_LOCATING_CUE", str(out)[:300])
    try:
        advise.build("cause-locator", "q", STORY, context={"repo_name": "x"})
        check("cause-locator without repo_listing raises", False, "no error raised")
    except ValueError as e:
        check("cause-locator without repo_listing raises naming context.repo_listing", "context.repo_listing" in str(e), str(e))


def test_four_seats_r4_old_aliases_need_no_context():
    _, a = advise.build("allspaw", ISHA_Q1, STORY)
    _, b = advise.build("allspaw", ISHA_Q1, STORY, context={"repo_name": "ignored"})
    check("four-seats R4 allspaw builds identically with or without context", a == b, f"{a} vs {b}")
    _, q = advise.build("qe-ic-advisor", ISHA_Q2, GUARD)
    check("four-seats R4 qe-ic-advisor unchanged",
          q == {"contract": "debugassist.fix-validation/1", "bug": GUARD, "attempt": ISHA_Q2}, str(q))


def test_substitution_is_one_pass():
    _, inputs = advise.build("defect-triage", "{evidence}", "see {context.repo_name} and {question}",
                             context={"repo_name": "myrepo"})
    check("caller text is never re-read as a placeholder",
          inputs["issue"] == {"title": "{evidence}", "body": "see {context.repo_name} and {question}"}, str(inputs))
    try:
        advise.build("defect-triage", "q", "e", context=["myrepo"])
        check("non-object context raises", False, "no error raised")
    except ValueError as e:
        check("non-object context raises", "context must be an object" in str(e), str(e))


def test_telemetry_one_line_per_call():
    tmp = isolate_tmp()
    asyncio.run(server.advise("allspaw", ISHA_Q1, STORY, consumer="test"))
    try:
        asyncio.run(server.advise("nonexistent", ISHA_Q1, STORY, consumer="test"))
    except ValueError:
        pass
    rows = [json.loads(l) for l in (tmp / "telemetry" / "advise_calls.jsonl").read_text().splitlines()]
    check("telemetry: one line per call", len(rows) == 2, str(rows))
    check("telemetry: fields ts, seat_alias, matched, consumer",
          all(set(r) == {"ts", "seat_alias", "matched", "consumer"} for r in rows), str(rows))
    check("telemetry: matched true then false", [r["matched"] for r in rows] == [True, False], str(rows))
    check("telemetry: test calls carry consumer=test", all(r["consumer"] == "test" for r in rows))


def test_advise_listed_by_server():
    async def go():
        return {t.name for t in await server.mcp.list_tools()}
    check("advise listed by the MCP server", "advise" in asyncio.run(go()))


if __name__ == "__main__":
    test_each_template_satisfies_its_seat_schema()
    test_both_aliases_resolve()
    test_r1_allspaw_returns_judgment()
    test_r2_qeic_returns_judgment()
    test_r3_unknown_alias_errors_clearly()
    test_four_aliases_known()
    test_four_seats_r1_defect_triage_with_repo_name()
    test_four_seats_r2_defect_triage_without_context_errors()
    test_four_seats_r3_cause_locator_candidates_from_listing()
    test_four_seats_cause_locator_listing_passes_through_as_list()
    test_four_seats_r4_old_aliases_need_no_context()
    test_substitution_is_one_pass()
    test_telemetry_one_line_per_call()
    test_advise_listed_by_server()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)
