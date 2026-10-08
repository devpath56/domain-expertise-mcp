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
    # A person named by role, not pronoun: "The engineer forgot to set the env var."
    re.compile(r"\b(engineer|developer|dev|operator|on-?call|reviewer|author|committer|admin|"
               r"someone|somebody|intern|person)s? (should have|failed to|forgot to|didn't|did not|"
               r"neglected to)\b", re.I),
]

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def _blame_scan(*texts) -> list:
    """Sentences (verbatim, stripped) that any BLAME_PATTERN matches, in input order."""
    hits = []
    for text in texts:
        for sentence in _SENTENCE_END.split(text or ""):
            sentence = sentence.strip()
            if sentence and any(p.search(sentence) for p in BLAME_PATTERNS):
                hits.append(sentence)
    return hits


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

    # The machine runs its own blame patterns over the inputs. Reported on
    # every path; it never changes the state or a decline code.
    blame = _blame_scan(desc, summary)

    # Rule: required fields, non-empty
    missing = []
    if not desc:
        missing.append("incident.description")
    if not summary:
        missing.append("fix.summary")
    if missing:
        return {**_declined(qid, "MISSING_FIELD", missing), "blame_sentences": blame}

    # Rule: outcome-only (no failure described) -> OUTCOME_ONLY
    if len(desc) < 20:
        return {**_declined(qid, "OUTCOME_ONLY", ["incident.description must describe the failure, not just the outcome"]),
                "blame_sentences": blame}

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
        "blame_sentences": blame,
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
        {"id": "blame_scan", "passed": not r.get("blame_sentences"),
         "detail": (f"{len(r['blame_sentences'])} sentence(s) blame a person: {r['blame_sentences']}"
                    if r.get("blame_sentences") else "no input sentence matches a blame pattern")},
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
