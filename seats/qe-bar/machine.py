"""qe-bar machine: grade acceptance criteria deterministically (q01, q03, q05, q06).

Per-criterion lexical checks: falsifiable, names an oracle, measurable,
not satisfiable by silence.
"""
import re

INPUT_SCHEMA = {
    "type": "object",
    "required": ["acceptance_criteria", "fix_description"],
    "properties": {
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
        "fix_description": {"type": "string"},
        "intuition": {"type": "string"},
        "clarification_rounds": {"type": "integer", "default": 0},
    },
}

_VAGUE = re.compile(r"\b(should|properly|correctly|appropriately|adequately|well)\b", re.I)
_MEASURABLE = re.compile(r"\d+\s*(ms|s\b|seconds?|times|/ ?\d+|x\b)|\b(percent|all|none|zero|every)\b|%", re.I)
_ORACLE = re.compile(r"\b(expected|baseline|prior|spec|golden|fixture|contract)\b", re.I)
_SILENCE = re.compile(r"\b(zero .* reported|no .* (reported|logged|alerts)|100% uptime)\b", re.I)


def grade_one(criterion: str) -> dict:
    checks = []
    vague = bool(_VAGUE.search(criterion))
    checks.append({"id": "falsifiable", "competency": "q01", "passed": not vague,
                   "detail": "contains vague qualifier" if vague else "states an invariant"})
    oracle = bool(_ORACLE.search(criterion))
    checks.append({"id": "names_oracle", "competency": "q03", "passed": oracle,
                   "detail": "oracle named" if oracle else "no comparison target named"})
    meas = bool(_MEASURABLE.search(criterion))
    checks.append({"id": "measurable", "competency": "q05", "passed": meas,
                   "detail": "carries a number/threshold" if meas else "adjective-only"})
    silence = bool(_SILENCE.search(criterion))
    checks.append({"id": "gaming_probe", "competency": "q06", "passed": not silence,
                   "detail": "satisfiable by under-reporting" if silence else "not silence-satisfiable"})
    grade = "FALSIFIABLE" if all(c["passed"] for c in checks) else (
        "VAGUE" if vague or not meas else "NO_ORACLE")
    return {"criterion": criterion, "grade": grade, "checks": checks}


def check(inputs: dict) -> dict:
    criteria = inputs.get("acceptance_criteria") or []
    per = [grade_one(c) for c in criteria]
    grades = [p["grade"] for p in per]
    if not per:
        verdict = "BAR_TOO_WEAK"
    elif all(g == "FALSIFIABLE" for g in grades):
        verdict = "SHIP"
    elif any(g == "VAGUE" for g in grades):
        verdict = "BAR_TOO_WEAK"
    else:
        verdict = "NOT_YET"
    # Tier triple: lexical rung only; precision unmeasured here -> rung claimed with evidence or D.
    tier = "C" if verdict == "SHIP" else "D"
    summary_checks = [
        {"id": "all_criteria_falsifiable", "competency": "q01",
         "passed": verdict == "SHIP",
         "detail": f"{sum(1 for g in grades if g == 'FALSIFIABLE')}/{len(grades)} criteria falsifiable"},
        {"id": "tier_claimed_with_evidence", "competency": "q05",
         "passed": True,
         "detail": f"tier {tier}: rung from lexical checks; precision unmeasured -> treat as D until measured"},
    ]
    return {"verdict": verdict, "per_criterion": per, "checks": summary_checks,
            "tier": tier,
            "tier_note": "rung from lexical checks; precision unmeasured -> treat as D until measured"}


def make_test(inputs: dict, result: dict) -> dict:
    return {
        "statement": f"The next fix judged by these {len(inputs.get('acceptance_criteria', []))} criteria "
        "ships with no recurrence of the same class in 30 days.",
        "resolve_rule": "PASS if no recurrence; FAIL if the class recurs despite passing; "
        "EXPIRED if no fix is judged in 30 days.",
    }
