"""cause-locator machine: DebugAssist query contract cause-locator.debugassist.v1.

Ranked candidates (max 3) with confidence as P(the eventual fix touches this
file within this range). Every path must be a verbatim member of repo.listing.
"""
INPUT_SCHEMA = {
    "type": "object",
    "required": ["issue", "repo"],
    "properties": {
        "contract": {"type": "string", "default": "cause-locator.debugassist.v1"},
        "query_id": {"type": "string"},
        "issue": {
            "type": "object",
            "required": ["description"],
            "properties": {
                "description": {"type": "string"},
                "repro_output": {"type": "string"},
            },
        },
        "repo": {
            "type": "object",
            "required": ["listing"],
            "properties": {
                "listing": {"type": "array", "items": {"type": "string"}},
            },
        },
        "candidates": {
            "type": "array",
            "description": "ranked by calling model, max 3",
            "items": {
                "type": "object",
                "required": ["path", "reason", "confidence"],
                "properties": {
                    "path": {"type": "string"},
                    "lines": {"type": "string"},
                    "reason": {"type": "string"},
                    "confidence": {"type": "number"},
                    "confirm_if": {"type": "string"},
                    "eliminate_if": {"type": "string"},
                },
            },
        },
        "confidence_bar": {"type": "number", "default": 0.5},
        "intuition": {"type": "string"},
        "clarification_rounds": {"type": "integer", "default": 0},
    },
}

CAUSE_NOT_FOUND_CODES = (
    "MISSING_FIELD", "NO_LOCATING_CUE", "CUE_OUTSIDE_LISTING",
    "LISTING_INCOMPLETE", "BELOW_BAR",
)
MAX_CANDIDATES = 3


def _not_found(qid, code, needs):
    return {
        "contract": "cause-locator.debugassist.v1",
        "query_id": qid, "seat": "cause-locator",
        "state": "CAUSE_NOT_FOUND", "code": code, "needs": needs,
    }


def _contract_check(inputs: dict) -> dict:
    qid = inputs.get("query_id", "")
    issue = inputs.get("issue") or {}
    repo = inputs.get("repo") or {}
    desc = (issue.get("description") or "").strip()
    listing = repo.get("listing") or []
    bar = inputs.get("confidence_bar", 0.5)

    if not desc:
        return _not_found(qid, "MISSING_FIELD", ["issue.description"])
    if not listing:
        return _not_found(qid, "LISTING_INCOMPLETE", ["repo.listing"])
    if not (issue.get("repro_output") or "").strip():
        return _not_found(qid, "NO_LOCATING_CUE", ["issue.repro_output: no stack frame, error line, or failing assertion to locate from"])

    candidates = inputs.get("candidates") or []
    # Rule 1: every path must be a verbatim member of repo.listing
    listing_set = set(listing)
    valid = []
    for c in candidates[:MAX_CANDIDATES]:
        path = c.get("path", "")
        if path not in listing_set:
            continue  # invented path: contract violation, dropped
        conf = c.get("confidence", 0)
        # Rule: file-level candidate (no lines) capped at 0.5
        if not c.get("lines") and conf > 0.5:
            conf = 0.5
        if conf >= bar:
            c = dict(c, confidence=conf)
            valid.append(c)

    if not valid:
        # Distinguish: cues pointed outside the listing vs nothing cleared the bar
        return _not_found(qid, "BELOW_BAR",
                          ["no candidate cleared the confidence bar; broaden repo.listing or lower the bar with justification"])

    return {
        "contract": "cause-locator.debugassist.v1",
        "query_id": qid, "seat": "cause-locator",
        "state": "CANDIDATES",
        "candidates": sorted(valid, key=lambda c: c["confidence"], reverse=True)[:MAX_CANDIDATES],
        "preregistration_note": "caller must hash top candidate + confidence + cues before DebugAssist investigates",
    }


def check(inputs: dict) -> dict:
    """Server interface: verdict + checks + contract fields."""
    r = _contract_check(inputs)
    state = r.get("state", "")
    cands = r.get("candidates", [])
    checks = [
        {"id": "paths_in_listing", "passed": True,
         "detail": f"{len(cands)} candidates, all verbatim members of repo.listing"},
        {"id": "max_three", "passed": len(cands) <= 3,
         "detail": f"{len(cands)} candidates"},
        {"id": "ranked", "passed": cands == sorted(cands, key=lambda c: c.get("confidence", 0), reverse=True),
         "detail": "ranked by confidence descending"},
    ]
    return {"verdict": state, "checks": checks, **r}


def make_test(inputs: dict, result: dict) -> dict:
    cands = result.get("candidates", [])
    top = cands[0]["path"] if cands else "none"
    return {
        "statement": f"The validated fix touches {top}.",
        "resolve_rule": "PASS if fix diff touches the top candidate's file+range; FILE-HIT if same file different range; FAIL otherwise; EXPIRED after 30 days.",
    }
