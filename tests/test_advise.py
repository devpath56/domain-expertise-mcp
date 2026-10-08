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


def test_each_template_satisfies_its_seat_schema():
    reg = registry.load_registry()
    config = advise.load_templates()
    for t in config["templates"]:
        alias = t["alias"]
        seat_id, inputs = advise.build(alias, ISHA_Q1, STORY)
        seat = registry.get_seat(reg, seat_id)
        miss = missing_required(seat["machine_module"].INPUT_SCHEMA, inputs)
        check(f"{alias} -> {seat_id}: built inputs satisfy INPUT_SCHEMA required", not miss, f"missing {miss}")
        flat = json.dumps(inputs)
        check(f"{alias}: no placeholder left unsubstituted", "{question}" not in flat and "{evidence}" not in flat)


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
    test_telemetry_one_line_per_call()
    test_advise_listed_by_server()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)
