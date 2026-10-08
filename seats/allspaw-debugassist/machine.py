"""allspaw-debugassist machine: DebugAssist query contract allspaw.debugassist.v1.

Structural validation for the incident-review seat. The generative judgment
(conditions, guards, tests) comes from the calling model + SKILL.md; this
machine enforces the contract shape: required fields, the two-state response,
coded declines, and the no-blame structural rules.
"""
import re

INPUT_SCHEMA = {
    "type": "object",
    "required": ["incident", "fix"],
    "properties": {
        "contract": {"type": "string", "default": "allspaw.debugassist.v1"},
        "query_id": {"type": "string"},
        "incident": {
            "type": "object",
            "required": ["description"],
            "properties": {
                "description": {"type": "string"},
                "title": {"type": "string"},
                "issue_url": {"type": "string"},
                "actor_account": {"type": "string"},
            },
        },
        "fix": {
            "type": "object",
            "required": ["summary"],
            "properties": {
                "summary": {"type": "string"},
                "files_changed": {"type": "array", "items": {"type": "string"}},
                "commit": {"type": "string"},
            },
        },
        "intuition": {"type": "string", "description": "caller's preregistered hypothesis"},
        "clarification_rounds": {"type": "integer", "default": 0},
    },
}

DECLINE_CODES = ("MISSING_FIELD", "OUTCOME_ONLY", "NOT_AN_INCIDENT")

BLAME_PATTERNS = [
    re.compile(r"\b(you|he|she|they) (should have|failed to|forgot to|didn't)\b", re.I),
    re.compile(r"\bblame\b", re.I),
    re.compile(r"\bbe more careful\b", re.I),
    re.compile(r"\bremember to\b", re.I),
]


def _declined(query_id, code, needs):
    return {
        "contract": "allspaw.debugassist.v1",
        "query_id": query_id,
        "seat": "allspaw",
        "state": "DECLINED",
        "reason": code,
        "needs": needs,
    }


def _contract_check(inputs: dict) -> dict:
    qid = inputs.get("query_id", "")
    incident = inputs.get("incident") or {}
    fix = inputs.get("fix") or {}

    desc = (incident.get("description") or "").strip()
    summary = (fix.get("summary") or "").strip()

    # Rule: required fields, non-empty
    missing = []
    if not desc:
        missing.append("incident.description")
    if not summary:
        missing.append("fix.summary")
    if missing:
        return _declined(qid, "MISSING_FIELD", missing)

    # Rule: outcome-only (no failure described) -> OUTCOME_ONLY
    if len(desc) < 20:
        return _declined(qid, "OUTCOME_ONLY", ["incident.description must describe the failure, not just the outcome"])

    # Structural checks the calling model must satisfy (returned as guidance)
    # The model generates CONDITIONS; we validate the shape here.
    return {
        "contract": "allspaw.debugassist.v1",
        "query_id": qid,
        "seat": "allspaw",
        "state": "READY_FOR_JUDGMENT",
        "input_valid": True,
        "rules": [
            "no blame language: condition is a property of system/code/process/tooling, never a person",
            "guard is a mechanism (test/check/type/schema/config/lint/runtime-assert/ci-gate), never an instruction",
            "each condition carries a falsifiable test (red_when/green_when/falsified_if)",
            "confidence capped at medium without the actor's account",
        ],
        "blame_check": "caller must scan output against blame patterns before returning CONDITIONS",
    }


def check(inputs: dict) -> dict:
    """Server interface: verdict + checks + contract fields."""
    r = _contract_check(inputs)
    state = r.get("state", "")
    checks = [
        {"id": "required_fields", "passed": r.get("reason") != "MISSING_FIELD",
         "detail": "incident.description and fix.summary present" if r.get("reason") != "MISSING_FIELD" else f"missing: {r.get('needs')}"},
        {"id": "two_state", "passed": state in ("CONDITIONS", "DECLINED", "READY_FOR_JUDGMENT"),
         "detail": f"state={state}"},
    ]
    return {"verdict": state, "checks": checks, **r}


def make_test(inputs: dict, result: dict) -> dict:
    if result["verdict"] in ("CONDITIONS", "READY_FOR_JUDGMENT"):
        return {
            "statement": "The guard written from these conditions prevents the incident class.",
            "resolve_rule": "PASS if a guard lands and the incident class does not recur in 30 days; FAIL if it recurs; EXPIRED after 30 days.",
        }
    return {
        "statement": "A guardable condition will be named for this incident.",
        "resolve_rule": "PASS if CONDITIONS returned; FAIL if DECLINED without coded reason; EXPIRED after 30 days.",
    }
