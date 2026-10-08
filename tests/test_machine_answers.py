"""Tests for the machines answering, not just verdicting (mcp-advise-complete-20261008).

allspaw-debugassist runs its BLAME_PATTERNS over the inputs and names the
sentences; qe-ic-debugassist classifies the guard as condition, instruction
or unclear. Both are additive: states, decline codes and the no-vibes
PASS/SOUND rules are unchanged. R1-R4 go through the advise on-ramp exactly
as a caller does. Trace, calibration and advise telemetry go to a temp dir.

Run: .venv/bin/python tests/test_machine_answers.py
"""
import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import advise, calibration, registry, trace  # noqa: E402
import server  # noqa: E402

PASS = []
FAIL = []

ISHA_Q1 = "Is this second story about conditions, never people? Name any sentence that blames a person, and any juncture the evidence does not support."
ISHA_Q2 = "Is this guard a condition (it runs by itself in CI) or an instruction (someone must remember it)? Which triggers of the bug's class does it miss?"


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("ok  " if cond else "FAIL") + f" {name}" + (f" — {detail}" if detail and not cond else ""))


def isolate_tmp():
    tmp = Path(tempfile.mkdtemp())
    trace.TRACE_PATH = tmp / "trace.jsonl"
    calibration.CAL_DIR = tmp / "calibration"
    advise.TELEMETRY_PATH = tmp / "telemetry" / "advise_calls.jsonl"


def ask(alias, question, evidence):
    return asyncio.run(server.advise(alias, question, evidence, consumer="test"))


def machine(seat_id):
    return registry.get_seat(registry.load_registry(), seat_id)["machine_module"]


def blame_check(out):
    return next((c for c in out["checks"] if c["id"] == "blame_scan"), None)


def guard_check(out):
    return next((c for c in out["checks"] if c["id"] == "guard_kind"), None)


def test_r1_blame_sentence_named():
    out = ask("allspaw", ISHA_Q1, "The engineer forgot to set the env var.")
    mr = out["machine_result"]
    check("R1 blame_sentences names the sentence",
          mr.get("blame_sentences") == ["The engineer forgot to set the env var."], mr.get("blame_sentences"))
    check("R1 blame_scan check fails", blame_check(mr) and blame_check(mr)["passed"] is False, blame_check(mr))


def test_r2_clean_story():
    out = ask("allspaw", ISHA_Q1, "The deploy failed because the config loader did not read the env var.")
    mr = out["machine_result"]
    check("R2 blame_sentences == []", mr.get("blame_sentences") == [], mr.get("blame_sentences"))
    check("R2 blame_scan check passes", blame_check(mr) and blame_check(mr)["passed"] is True, blame_check(mr))
    check("R2 verdict unchanged (READY_FOR_JUDGMENT)", out["verdict"] == "READY_FOR_JUDGMENT", out["verdict"])


def test_r3_instruction_guard():
    out = ask("qe-ic-advisor", ISHA_Q2, "Remember to set the env var before deploying.")
    mr = out["machine_result"]
    check("R3 guard_kind == instruction", mr.get("guard_kind") == "instruction", mr.get("guard_patterns_matched"))
    check("R3 matched phrase reported", mr["guard_patterns_matched"]["instruction"] == ["Remember to"],
          mr.get("guard_patterns_matched"))
    check("R3 verdict-less call still FAIL (state unchanged)", out["verdict"] == "FAIL", out["verdict"])
    check("R3 guard_kind check fails", guard_check(mr) and guard_check(mr)["passed"] is False, guard_check(mr))


def test_r4_condition_guard():
    out = ask("qe-ic-advisor", ISHA_Q2, "CI check fails the build when the env var is missing.")
    mr = out["machine_result"]
    check("R4 guard_kind == condition", mr.get("guard_kind") == "condition", mr.get("guard_patterns_matched"))
    check("R4 guard_kind check passes", guard_check(mr) and guard_check(mr)["passed"] is True, guard_check(mr))


def test_questions_themselves_are_clean():
    """Isha's questions ride along as fix.summary / attempt; they must not
    trip the patterns, or every on-ramp call would carry a false finding."""
    check("Q1 alone matches no blame pattern", machine("allspaw-debugassist")._blame_scan(ISHA_Q1) == [])
    kind, matched = machine("qe-ic-debugassist")._guard_kind(ISHA_Q2)
    check("Q2 alone classifies unclear with no phrase", kind == "unclear"
          and matched == {"condition": [], "instruction": []}, matched)


def test_additive_only():
    a = machine("allspaw-debugassist")
    r = a.check({"incident": {"description": ""}, "fix": {"summary": "x"}})
    check("allspaw decline code unchanged (MISSING_FIELD)", r["state"] == "DECLINED" and r["reason"] == "MISSING_FIELD", r)
    r = a.check({"incident": {"description": "They forgot to"}, "fix": {"summary": "x"}})
    check("allspaw OUTCOME_ONLY unchanged and still scanned",
          r["reason"] == "OUTCOME_ONLY" and r["blame_sentences"] == ["They forgot to"], r)
    r = a.check({"incident": {"description": "You should have tested it. The cache key ignored the locale."},
                 "fix": {"summary": "Key the cache on locale."}})
    check("allspaw blame does not change the state",
          r["state"] == "READY_FOR_JUDGMENT" and r["blame_sentences"] == ["You should have tested it."], r)

    q = machine("qe-ic-debugassist")
    base = {"contract": "debugassist.fix-validation/1", "attempt": "Make sure the CI check passes."}
    r = q.check({**base, "verdict": "PASS", "check": "test_retry_expired_token", "flip_condition": "test goes red"})
    check("qe-ic named PASS still PASS", r["state"] == "PASS", r["state"])
    check("qe-ic both kinds -> unclear", r["guard_kind"] == "unclear", r["guard_patterns_matched"])
    r = q.check({**base, "verdict": "PASS", "check": "looks good", "flip_condition": "x"})
    check("qe-ic vibes PASS still FAIL", r["state"] == "FAIL" and r["vibes_found"], r)
    r = q.check({"contract": "debugassist.test-soundness/1", "verdict": "SOUND"})
    check("qe-ic test-soundness carries no guard_kind", "guard_kind" not in r and r["state"] == "UNEVALUABLE", r)


if __name__ == "__main__":
    isolate_tmp()
    test_r1_blame_sentence_named()
    test_r2_clean_story()
    test_r3_instruction_guard()
    test_r4_condition_guard()
    test_questions_themselves_are_clean()
    test_additive_only()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)
