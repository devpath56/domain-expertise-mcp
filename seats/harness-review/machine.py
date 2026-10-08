"""harness-review machine: grade the agent surface (h01-h09).

Deterministic completeness checks. Declared != working: unresolved items are
counted, never silently passed.
"""
import re

INPUT_SCHEMA = {
    "type": "object",
    "required": ["holds", "reaches", "missing_piece_behavior"],
    "properties": {
        "holds": {"type": "array", "items": {"type": "string"}, "description": "what the agent is handed each turn"},
        "reaches": {"type": "array", "items": {"type": "string"}, "description": "what the agent may reach for"},
        "missing_piece_behavior": {"type": "string", "description": "what happens when a named referent is missing"},
        "intuition": {"type": "string"},
        "clarification_rounds": {"type": "integer", "default": 0},
    },
}

_SPEND = re.compile(r"\b(budget|cap|timeout|limit|quota)\b", re.I)
_IN_CODE = re.compile(r"\b(code|config|guardrail|meter|enforced)\b", re.I)


def check(inputs: dict) -> dict:
    holds = inputs.get("holds") or []
    reaches = inputs.get("reaches") or []
    missing = (inputs.get("missing_piece_behavior") or "").strip()
    checks = []

    checks.append({"id": "holds_declared", "competency": "h01", "passed": bool(holds),
                   "detail": f"{len(holds)} items declared" if holds else "nothing declared as held"})
    checks.append({"id": "reaches_declared", "competency": "h03", "passed": bool(reaches),
                   "detail": f"{len(reaches)} seams declared" if reaches else "no documented seams"})
    checks.append({"id": "missing_piece_named", "competency": "h05",
                   "passed": bool(missing),
                   "detail": "missing-piece behavior stated" if missing else
                   "SILENCE: unstated missing-piece behavior is BROKEN, not 'nothing missing'"})
    spend = bool(_SPEND.search(missing + " " + " ".join(holds + reaches)))
    in_code = bool(_IN_CODE.search(missing))
    checks.append({"id": "spend_stop", "competency": "h09", "passed": spend and in_code,
                   "detail": "spend stop in code" if (spend and in_code) else
                   "no spend stop, or stop lives in prose not code"})

    failed = [c["id"] for c in checks if not c["passed"]]
    if not missing:
        verdict = "BROKEN"
    elif failed:
        verdict = "INCOMPLETE"
    else:
        verdict = "HELD"
    seams = [f"undeclared reach: {r}" for r in reaches if not r.strip()] + (
        ["missing-piece behavior unstated: agent fails silently"] if not missing else [])
    return {"verdict": verdict, "checks": checks, "failed_checks": failed, "seams": seams}


def make_test(inputs: dict, result: dict) -> dict:
    return {
        "statement": "The next 10 agent runs on this surface: no run fails from a missing or "
        "misdeclared surface piece.",
        "resolve_rule": "PASS if 10/10 clean; FAIL if any run fails on surface grounds; "
        "EXPIRED after 30 days.",
    }
