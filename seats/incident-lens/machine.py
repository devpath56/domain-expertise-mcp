"""incident-lens machine: the A1 reader (Allspaw a03/a05).

Lexical proxy over guard text. Ported from the allspaw A1 reader's ruled
decisions (operator, 2026-10-01): a guard naming no mechanism reads
INSTRUCTION; the reading is a proxy, not a sufficiency judgment.
"""
import re

INPUT_SCHEMA = {
    "type": "object",
    "required": ["failure_account", "proposed_guard"],
    "properties": {
        "failure_account": {"type": "string"},
        "proposed_guard": {"type": "string", "description": "guard text DebugAssist proposes to write"},
        "intuition": {"type": "string"},
        "clarification_rounds": {"type": "integer", "default": 0},
    },
}

_INSTRUCTION = re.compile(
    r"\b(be careful|remember to|ensure|make sure|should always|don't forget|be vigilant|take care)\b", re.I)
_MECHANISM = re.compile(
    r"\b([a-z_]+\.(py|mjs|js|sh|yml|yaml|json)|guardrail|check|gate|hook|policy|test_|assert|deny|refuse|block)\b", re.I)


def check(inputs: dict) -> dict:
    guard = (inputs.get("proposed_guard") or "").strip()
    account = (inputs.get("failure_account") or "").strip()
    checks = []
    checks.append({"id": "account_present", "competency": "a02", "passed": bool(account),
                   "detail": "failure account present" if account else "no account: nothing to read"})
    if not guard:
        checks.append({"id": "guard_present", "competency": "a05", "passed": False,
                       "detail": "no guard text: UNEVALUABLE"})
        return {"verdict": "UNEVALUABLE", "checks": checks}

    instruct = bool(_INSTRUCTION.search(guard))
    names_mech = bool(_MECHANISM.search(guard))
    checks.append({"id": "names_mechanism", "competency": "a05", "passed": names_mech,
                   "detail": "names a file/control/mechanism" if names_mech else
                   "names no mechanism -> INSTRUCTION by ruling 2026-10-01"})
    checks.append({"id": "not_person_instruction", "competency": "a03", "passed": not instruct,
                   "detail": "instructs a person" if instruct else "does not instruct a person"})

    verdict = "CONDITION" if (names_mech and not instruct) else "INSTRUCTION"
    rewrite = None
    if verdict == "INSTRUCTION":
        rewrite = ("Restate as a changed condition: name the file, control, or gate that makes "
                   "the failure inexpressible, and who/what enforces it without a human remembering.")
    return {"verdict": verdict, "checks": checks,
            "guard_rewrite": rewrite,
            "proxy_note": "lexical proxy only: checks the named control exists in text, not that the guard is sufficient"}


def make_test(inputs: dict, result: dict) -> dict:
    return {
        "statement": "The deployed guard holds: the failure class does not recur in 60 days.",
        "resolve_rule": "PASS if no recurrence; FAIL if it recurs with the guard in place; "
        "EXPIRED if the guard is removed or never deployed.",
    }
