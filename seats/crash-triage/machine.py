"""crash-triage machine: pattern library over crash signatures.

Deterministic lexical match. A match is a prior, not a diagnosis; base rates
are counts until n>=30.
"""
import re

INPUT_SCHEMA = {
    "type": "object",
    "required": ["signature"],
    "properties": {
        "signature": {"type": "string", "description": "error type, service, one-line description"},
        "stack_excerpt": {"type": "string", "description": "up to 20 lines of stack trace"},
        "intuition": {"type": "string", "description": "caller's preregistered hypothesis"},
        "clarification_rounds": {"type": "integer", "default": 0},
    },
}

# pattern: (regex, name, likely_cause, check_first, prior_correct, prior_seen, fix_area)
PATTERNS = [
    (
        re.compile(r"npe|nullpointer", re.I),
        "retry-storm NPE",
        "missing idempotency key or downstream timeout shorter than the retry window",
        ["idempotency key generation", "retry backoff config", "downstream latency p99"],
        22, 23, "retry/idempotency handling code",
    ),
    (
        re.compile(r"timeout|timed out|deadline", re.I),
        "downstream timeout cascade",
        "a slow dependency under load; callers pile up behind the slowest call",
        ["dependency latency dashboard", "timeout vs retry budget", "circuit breaker state"],
        14, 16, "timeout/retry configuration",
    ),
    (
        re.compile(r"oom|out of memory|memory", re.I),
        "unbounded growth",
        "a collection or cache with no eviction bound growing per request",
        ["heap dump top consumers", "cache eviction policy", "request-scoped allocations"],
        9, 11, "allocation/caching code",
    ),
    (
        re.compile(r"race|concurrent|deadlock", re.I),
        "concurrency hazard",
        "shared mutable state across threads/tasks without ordering",
        ["shared state in the stack frames", "lock ordering", "repro under --repeat"],
        7, 9, "concurrency control code",
    ),
    (
        re.compile(r"config|misconfig|flag", re.I),
        "config drift",
        "a flag or config value that differs between the failing env and the working one",
        ["diff of config across envs", "recent flag changes", "default vs override"],
        11, 12, "configuration",
    ),
]


def check(inputs: dict) -> dict:
    sig = (inputs.get("signature") or "").strip()
    checks = []
    if not sig:
        return {
            "verdict": "UNEVALUABLE",
            "checks": [{"id": "has_signature", "passed": False, "detail": "no signature given"}],
            "pattern": None,
        }
    checks.append({"id": "has_signature", "passed": True, "detail": "signature present"})
    stack = inputs.get("stack_excerpt") or ""
    haystack = f"{sig}\n{stack}"
    for rx, name, cause, first, ok, seen, area in PATTERNS:
        if rx.search(haystack):
            checks.append({"id": "pattern_match", "passed": True, "detail": f"matched {name}"})
            return {
                "verdict": "MATCHED",
                "checks": checks,
                "pattern": name,
                "likely_cause": cause,
                "check_first": first,
                "prior": {"correct": ok, "seen": seen},
                "fix_area": area,
                "confidence": "high" if seen >= 10 else "medium",
            }
    checks.append({"id": "pattern_match", "passed": False, "detail": "no library pattern matched"})
    return {"verdict": "NO_MATCH", "checks": checks, "pattern": None}


def make_test(inputs: dict, result: dict) -> dict:
    if result["verdict"] == "MATCHED":
        area = result["fix_area"]
        return {
            "statement": f"The validated fix for '{inputs['signature']}' will touch {area}.",
            "resolve_rule": "PASS if the validated fix diff touches the named area; "
            "FAIL if the validated fix is elsewhere; EXPIRED if no fix validates in 30 days.",
        }
    return {
        "statement": f"A cause will be named with a real source file and lines for '{inputs['signature']}'.",
        "resolve_rule": "PASS if the investigation names a real file+lines; FAIL if it ends CAUSE NOT FOUND; "
        "EXPIRED after 30 days.",
    }
