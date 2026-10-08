"""metric-design machine: deterministic critic checks (m01-m09, m10).

Lexical checks only. Whether the metric is the RIGHT one is m10 (elicitation)
and stays with the caller/operator.
"""
import re

INPUT_SCHEMA = {
    "type": "object",
    "required": ["metric", "decision"],
    "properties": {
        "metric": {"type": "string"},
        "decision": {"type": "string", "description": "the decision this metric drives"},
        "denominator": {"type": "string"},
        "intuition": {"type": "string"},
        "clarification_rounds": {"type": "integer", "default": 0},
    },
}

_RAW_COUNT = re.compile(r"\b(total|count|number of)\b", re.I)
_NORM = re.compile(r"\b(per|rate|ratio|%|percent|normalized)\b", re.I)


def check(inputs: dict) -> dict:
    metric = (inputs.get("metric") or "").strip()
    decision = (inputs.get("decision") or "").strip()
    denom = (inputs.get("denominator") or "").strip()
    checks = []

    c = {"id": "has_decision", "competency": "m10",
         "passed": bool(decision), "detail": "decision named" if decision else "no decision: vanity metric"}
    checks.append(c)
    c = {"id": "has_denominator", "competency": "m03",
         "passed": bool(denom), "detail": "denominator stated" if denom else "denominator unstated"}
    checks.append(c)
    raw = bool(_RAW_COUNT.search(metric)) and not bool(_NORM.search(metric))
    checks.append({"id": "gaming_probe", "competency": "m04", "passed": not raw,
                   "detail": "raw count with no normalization: gaming-prone" if raw else "normalized or not a raw count"})
    actionable = bool(re.search(r"\b(alert|gate|block|rollback|page|escalate|ship|stop)\b", metric + " " + decision, re.I))
    checks.append({"id": "actionable", "competency": "m07", "passed": actionable,
                   "detail": "ends in a next action" if actionable else "no action named"})

    failed = [c["id"] for c in checks if not c["passed"]]
    if not decision:
        verdict = "NEEDS_DECISION"
    elif not denom:
        verdict = "NEEDS_DENOMINATOR"
    elif raw:
        verdict = "GAMING_RISK"
    else:
        verdict = "GROUNDED"
    return {"verdict": verdict, "checks": checks, "failed_checks": failed}


def make_test(inputs: dict, result: dict) -> dict:
    return {
        "statement": f"On the next 3 uses of '{inputs['metric']}' for '{inputs['decision']}', "
        "the decision-maker states the denominator without re-reading the definition.",
        "resolve_rule": "PASS if all 3 uses state it; FAIL if any use proceeds on an unstated "
        "denominator; EXPIRED after 60 days.",
    }
