"""statistical-rigor machine: can the data answer this? (ds-ic c01, c04, c07, c06).

Deterministic: n>=30 for rates (count, don't rate below it), denominator
must match the claim population, effect must be named, self-grading flagged.
Refusal is a correct answer.
"""
import re

INPUT_SCHEMA = {
    "type": "object",
    "required": ["claim", "n", "denominator", "claim_population"],
    "properties": {
        "claim": {"type": "string"},
        "n": {"type": "integer"},
        "denominator": {"type": "string"},
        "claim_population": {"type": "string"},
        "intuition": {"type": "string"},
        "clarification_rounds": {"type": "integer", "default": 0},
    },
}

_MIN_N_RATE = 30
_EFFECT = re.compile(r"\d+\s*%|\breduced by\b|\bincreased by\b|\bfrom .* to\b|\beffect size\b|\bdelta\b", re.I)
_RATE_WORDS = re.compile(r"\b(rate|percent|%|proportion|ratio)\b", re.I)
_SELF_GRADE = re.compile(r"\b(our (own )?judge|self-?graded|author.*judge|fix.*validat.*own)\b", re.I)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def check(inputs: dict) -> dict:
    claim = inputs.get("claim") or ""
    n = inputs.get("n") or 0
    denom = _norm(inputs.get("denominator"))
    pop = _norm(inputs.get("claim_population"))
    checks = []

    is_rate = bool(_RATE_WORDS.search(claim))
    need = _MIN_N_RATE if is_rate else 1
    checks.append({"id": "n_sufficient", "competency": "c04",
                   "passed": n >= need,
                   "detail": f"n={n} >= {need}" if n >= need else
                   f"n={n} < {need}: counts only, no rate may be published"})
    denom_match = bool(denom) and bool(pop) and (denom == pop or denom in pop or pop in denom)
    checks.append({"id": "denominator_match", "competency": "c01",
                   "passed": denom_match,
                   "detail": "denominator matches claim population" if denom_match else
                   f"computed over '{denom}' but claimed about '{pop}': REFUSED"})
    effect = bool(_EFFECT.search(claim))
    checks.append({"id": "effect_named", "competency": "c04", "passed": effect,
                   "detail": "effect size named" if effect else "direction only, no effect size"})
    self_grade = bool(_SELF_GRADE.search(claim))
    checks.append({"id": "no_self_grading", "competency": "c06", "passed": not self_grade,
                   "detail": "judge may be the producer: disqualified" if self_grade else "no self-grading detected"})

    if not denom_match:
        verdict = "DENOMINATOR_MISMATCH"
    elif n < need:
        verdict = "NEEDS_N"
    elif not effect:
        verdict = "UNANSWERABLE"
    else:
        verdict = "ANSWERABLE"
    return {"verdict": verdict, "checks": checks, "min_n": need,
            "refusal_note": "refusal is a correct answer when the data cannot carry the claim"}


def make_test(inputs: dict, result: dict) -> dict:
    need = result.get("min_n", _MIN_N_RATE)
    return {
        "statement": f"Collect to n={need} on the matched population; the claimed effect holds at the stated size.",
        "resolve_rule": "PASS if the effect holds; FAIL if it vanishes or reverses; "
        "EXPIRED if the population changes first.",
    }
