"""defect-triage machine: DebugAssist query contract defect-triage.debugassist.v1.

Four-state decision rule. Confidence is checked BEFORE the defect probability
so an untrusted p cannot close an issue.
"""
INPUT_SCHEMA = {
    "type": "object",
    "required": ["issue", "repo"],
    "properties": {
        "contract": {"type": "string", "default": "defect-triage.debugassist.v1"},
        "query_id": {"type": "string"},
        "issue": {
            "type": "object",
            "required": ["title", "body"],
            "properties": {
                "title": {"type": "string"},
                "body": {"type": "string"},
            },
        },
        "repo": {
            "type": "object",
            "required": ["name"],
            "properties": {"name": {"type": "string"}},
        },
        "is_defect": {"type": "number", "description": "P(the repro shows Y), set by calling model"},
        "confidence": {"type": "number", "description": "P(verdict stays on same side of 0.5 once the one missing fact is known)"},
        "intuition": {"type": "string"},
        "clarification_rounds": {"type": "integer", "default": 0},
    },
}

CONFIDENCE_BAR = 0.6
DEFECT_BAR = 0.5


def _contract_check(inputs: dict) -> dict:
    qid = inputs.get("query_id", "")
    issue = inputs.get("issue") or {}
    repo = inputs.get("repo") or {}
    title = (issue.get("title") or "").strip()
    body = (issue.get("body") or "").strip()
    repo_name = (repo.get("name") or "").strip()

    # Missing field declines first
    missing = [f for f, v in
               (("issue.title", title), ("issue.body", body), ("repo.name", repo_name)) if not v]
    if missing:
        return {
            "contract": "defect-triage.debugassist.v1",
            "query_id": qid, "seat": "defect-triage",
            "state": "DECLINED", "reason": "MISSING_FIELD", "needs": missing,
        }

    is_defect = inputs.get("is_defect")
    confidence = inputs.get("confidence")

    # The calling model supplies is_defect and confidence; without them we cannot decide
    if is_defect is None or confidence is None:
        return {
            "contract": "defect-triage.debugassist.v1",
            "query_id": qid, "seat": "defect-triage",
            "state": "NEEDS_PERSON",
            "question": "What is the single missing fact that would move is_defect across 0.5?",
            "answerer": "issue reporter or repo maintainer",
            "flips_to": {
                "fact_confirms_defect": "DEFECT",
                "fact_refutes_defect": "NOT_A_DEFECT",
            },
            "note": "is_defect and/or confidence not supplied by caller",
        }

    # Decision rule, fixed order: confidence first
    if confidence < CONFIDENCE_BAR:
        return {
            "contract": "defect-triage.debugassist.v1",
            "query_id": qid, "seat": "defect-triage",
            "state": "NEEDS_PERSON",
            "question": "What is the one missing fact that would settle this?",
            "confidence": confidence,
            "is_defect": is_defect,
        }
    if is_defect < DEFECT_BAR:
        return {
            "contract": "defect-triage.debugassist.v1",
            "query_id": qid, "seat": "defect-triage",
            "state": "NOT_A_DEFECT",
            "is_defect": is_defect,
            "confidence": confidence,
        }
    return {
        "contract": "defect-triage.debugassist.v1",
        "query_id": qid, "seat": "defect-triage",
        "state": "DEFECT",
        "is_defect": is_defect,
        "confidence": confidence,
        "focus_rule": "caller selects focus by the four ordered rules; non-focus symptoms go in set_aside",
    }


def check(inputs: dict) -> dict:
    """Server interface: verdict + checks + contract fields."""
    r = _contract_check(inputs)
    state = r.get("state", "")
    checks = [
        {"id": "required_fields", "passed": r.get("reason") != "MISSING_FIELD",
         "detail": "issue.title/body and repo.name present" if r.get("reason") != "MISSING_FIELD" else f"missing: {r.get('needs')}"},
        {"id": "confidence_first", "passed": True,
         "detail": "confidence checked before is_defect per decision rule"},
        {"id": "four_state", "passed": state in ("DEFECT", "NOT_A_DEFECT", "NEEDS_PERSON", "DECLINED"),
         "detail": f"state={state}"},
    ]
    return {"verdict": state, "checks": checks, **r}


def make_test(inputs: dict, result: dict) -> dict:
    return {
        "statement": f"The triage verdict '{result['verdict']}' for '{inputs.get('issue', {}).get('title', '')}' holds after investigation.",
        "resolve_rule": "PASS if the validated outcome matches the verdict; FAIL if it flips; EXPIRED after 30 days.",
    }
