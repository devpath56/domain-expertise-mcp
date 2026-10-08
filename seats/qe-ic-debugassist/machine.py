"""qe-ic-debugassist machine: DebugAssist query contracts for qe-ic-advisor.

Two contracts:
  A. debugassist.fix-validation/1 — is the proposed fix validated?
  B. debugassist.test-soundness/1 — is the judging test sound?

No-vibes rule: every PASS/SOUND names its check and flip condition.
"looks good" / "LGTM" are malformed -> treated as FAIL / UNEVALUABLE.
"""
import re

INPUT_SCHEMA = {
    "type": "object",
    "required": ["contract"],
    "properties": {
        "contract": {"type": "string", "enum": ["debugassist.fix-validation/1", "debugassist.test-soundness/1"]},
        "query_id": {"type": "string"},
        "bug": {"type": "string"},
        "attempt": {"type": "string"},
        "fix_diff": {"type": "string"},
        "judging_test": {"type": "object"},
        "test_results": {"type": "array"},
        "affected_suites": {"type": "array", "items": {"type": "string"}},
        "verdict": {"type": "string", "description": "caller's verdict: PASS/FAIL or SOUND/UNSOUND"},
        "check": {"type": "string", "description": "the named check"},
        "flip_condition": {"type": "string", "description": "outcome that would flip the verdict"},
        "missing": {"type": "array", "items": {"type": "string"}},
        "intuition": {"type": "string"},
        "clarification_rounds": {"type": "integer", "default": 0},
    },
}

VIBES = [
    re.compile(r"\blooks good\b", re.I),
    re.compile(r"\bLGTM\b"),
    re.compile(r"\bseems fine\b", re.I),
    re.compile(r"\bshould work\b", re.I),
]


def _vibes_check(text):
    text = text or ""
    return [p.pattern for p in VIBES if p.search(text)]


# Guard kind: a condition runs by itself (CI, test, lint, assert, gate);
# an instruction needs someone to remember it.
INSTRUCTION_PATTERNS = [
    re.compile(r"\bremember to\b", re.I),
    re.compile(r"\bbe careful\b", re.I),
    re.compile(r"\bmake sure\b", re.I),
    re.compile(r"\b(don't|do not) forget\b", re.I),
    re.compile(r"\bbe sure to\b", re.I),
    re.compile(r"\bkeep in mind\b", re.I),
]
CONDITION_PATTERNS = [
    re.compile(r"\bCI check", re.I),
    re.compile(r"\btests? fails?\b", re.I),
    re.compile(r"\blint", re.I),
    re.compile(r"\bassert", re.I),
    re.compile(r"\bgates?\b", re.I),
    re.compile(r"\bblocks the build\b", re.I),
    re.compile(r"\bfails the build\b", re.I),
]


def _guard_kind(*texts) -> tuple:
    """(kind, matched) over the caller's bug/attempt/fix text. Only condition
    patterns -> condition; only instruction patterns -> instruction; both or
    neither -> unclear. matched holds the literal phrases found."""
    text = " ".join(t for t in texts if t)
    matched = {
        "condition": [m.group(0) for p in CONDITION_PATTERNS for m in [p.search(text)] if m],
        "instruction": [m.group(0) for p in INSTRUCTION_PATTERNS for m in [p.search(text)] if m],
    }
    if matched["condition"] and not matched["instruction"]:
        return "condition", matched
    if matched["instruction"] and not matched["condition"]:
        return "instruction", matched
    return "unclear", matched


def _contract_check(inputs: dict) -> dict:
    qid = inputs.get("query_id", "")
    contract = inputs.get("contract", "")
    verdict = (inputs.get("verdict") or "").upper()
    check_name = inputs.get("check") or ""
    flip = inputs.get("flip_condition") or ""

    # No-vibes rule: malformed PASS/SOUND
    vibes = _vibes_check(check_name) + _vibes_check(flip)
    if contract == "debugassist.fix-validation/1":
        if verdict == "PASS":
            if not check_name or not flip or vibes:
                return {
                    "contract": contract, "query_id": qid, "seat": "qe-ic-advisor",
                    "state": "FAIL",
                    "reason": "malformed PASS: names no check, no flip condition, or uses vibes language",
                    "vibes_found": vibes,
                }
            return {"contract": contract, "query_id": qid, "seat": "qe-ic-advisor",
                    "state": "PASS", "check": check_name, "flip_condition": flip}
        if verdict == "UNEVALUABLE":
            return {"contract": contract, "query_id": qid, "seat": "qe-ic-advisor",
                    "state": "UNEVALUABLE", "missing": inputs.get("missing", [])}
        return {"contract": contract, "query_id": qid, "seat": "qe-ic-advisor",
                "state": "FAIL", "check": check_name or "unstated"}

    if contract == "debugassist.test-soundness/1":
        if verdict == "SOUND":
            if not check_name or not flip or vibes:
                return {
                    "contract": contract, "query_id": qid, "seat": "qe-ic-advisor",
                    "state": "UNEVALUABLE",
                    "reason": "malformed SOUND: names no check, no flip condition, or uses vibes language",
                    "vibes_found": vibes,
                }
            return {"contract": contract, "query_id": qid, "seat": "qe-ic-advisor",
                    "state": "SOUND", "check": check_name, "flip_condition": flip}
        if verdict == "UNEVALUABLE":
            return {"contract": contract, "query_id": qid, "seat": "qe-ic-advisor",
                    "state": "UNEVALUABLE", "missing": inputs.get("missing", [])}
        return {"contract": contract, "query_id": qid, "seat": "qe-ic-advisor",
                "state": "UNSOUND", "check": check_name or "unstated"}

    return {"contract": contract, "query_id": qid, "seat": "qe-ic-advisor",
            "state": "DECLINED", "reason": "unknown contract"}


def check(inputs: dict) -> dict:
    """Server interface: verdict + checks + contract fields."""
    r = _contract_check(inputs)
    state = r.get("state", "")
    guard_check = []
    if r.get("contract") == "debugassist.fix-validation/1":
        # Additive: the guard classification rides on every fix-validation
        # answer (including the advise on-ramp's verdict-less FAIL); it never
        # changes the state.
        kind, matched = _guard_kind(inputs.get("bug"), inputs.get("attempt"), inputs.get("fix_diff"))
        r = {**r, "guard_kind": kind, "guard_patterns_matched": matched}
        guard_check = [{"id": "guard_kind", "passed": kind == "condition",
                        "detail": f"guard_kind={kind}; condition={matched['condition']}, "
                                  f"instruction={matched['instruction']}"}]
    checks = [
        {"id": "no_vibes", "passed": not r.get("vibes_found"),
         "detail": f"vibes found: {r.get('vibes_found')}" if r.get("vibes_found") else "no vibes language"},
        {"id": "named_check", "passed": bool(r.get("check")),
         "detail": f"check='{r.get('check', '')}'"},
        {"id": "flip_condition", "passed": bool(r.get("flip_condition")),
         "detail": "flip condition named" if r.get("flip_condition") else "no flip condition"},
    ] + guard_check
    return {"verdict": state, "checks": checks, **r}


def make_test(inputs: dict, result: dict) -> dict:
    return {
        "statement": f"The {result.get('contract')} verdict '{result['verdict']}' holds under independent review.",
        "resolve_rule": "PASS if an independent reviewer confirms; FAIL if overturned; EXPIRED after 30 days.",
    }
