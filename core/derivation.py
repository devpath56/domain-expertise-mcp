"""Metric derivation layer (metric-derivation-20261007).

Reads Judgment trace records (core/trace.py, annotated by
core/phoenix_trace.py) and computes the spec metrics metric.1..metric.10
per seat, plus the online eval `verdict_justified` and its rollup G13.

Hard constraints (stops, not suggestions):
- D2: missingness is reported beside every number, always ("k of N").
- D5: metric.8 / G10 / G11 are mechanically refused while token
  emission-vs-billed parity is unmeasured. No human in the loop.
- D9: a 0-token count is an instrument bug -> alert, never a data point.
  Missing billed tokens stay null and propagate as unknown.
- Counts only until n >= 30: no rate, percentage or percentile is computed
  on a sub-30 sample.

Redproofs RP-D1..RP-D5 and RP-E1 live in tests/test_derivation.py.

CLI:
  python core/derivation.py metrics --start ISO --end ISO [--parity FILE]
  python core/derivation.py eval [--start ISO --end ISO]   # one G13 pass;
      # hourly via cron: 0 * * * * cd <repo> && python core/derivation.py eval
"""
import hashlib
import json
import os
import statistics
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRACE_PATH = ROOT / "trace" / "trace.jsonl"
EVAL_DIR = ROOT / "evals" / "verdict_justified"
JUDGE_PROMPT_PATH = EVAL_DIR / "v1.md"
ANNOTATIONS_PATH = EVAL_DIR / "annotations.jsonl"

MIN_N = 30
SAMPLE_RATE = 0.20
EVAL_NAME = "verdict_justified"
EVAL_VERSION = "v1"

ADVISOR_ROLES = {"seat", "advisor"}
CHILD_TYPES = {"Turn", "MachineRun", "GroundingLink", "Intuition", "Test"}
RESOLVED = {"resolved_pass", "resolved_fail"}
PENDING = {"delivered", "pending", None}
# Fields that do not change the situation a seat was asked about (metric.7).
VOLATILE_INPUTS = {"billed_tokens", "intuition", "query_id", "confidence",
                   "clarification_rounds", "pattern"}

FLAGS = {
    "metric.4": ("gt", 0.30),   # UPR
    "metric.5": ("gt", 0.10),   # ungrounded share
    "metric.6": ("gt", 0.15),   # calibration delta
    "metric.7": ("lt", 0.70),   # consistency
    "metric.10": ("gt", 0.30),  # expire rate
}

# Deviations from the metric map, stated in every output (RP-D5).
DEVIATIONS = [
    "D2 vs RP-D2: missingness is always reported as count + denominator; its "
    "percentage is published only when the denominator is >= 30.",
    "metric.6: the map says n>=10 per pattern; the global counts-only rule "
    "(n>=30) is stricter and wins, so a pattern needs 30 resolved judgments.",
    "metric.6/7: 'pattern' is read from an explicit `pattern` field on the "
    "Judgment record or its inputs; no seat emits one yet, so both report "
    "100% missing until one does. Nothing is inferred.",
    "metric.7: 'similar situation' = same seat + same canonical inputs "
    f"(minus {sorted(VOLATILE_INPUTS)}), or an explicit `situation_key`.",
    "metric.5: 'sections' are read from a `sections` list on the Judgment "
    "record ({grounded: bool}); GroundingLink rows are not sections.",
    "metric.3: an advisor question turn is a Turn with question=true, or a "
    "seat/advisor-role Turn whose content ends in '?'.",
    "G10/G11: not defined in this brief; refused under the same D5 parity "
    "gate as metric.8 rather than computed.",
    "G13 source: Phoenix seat.* spans carry seat.id and token counts only "
    "(no judgment id, inputs, checks or verdict), so the judge reads its "
    "evidence from the trace store; Phoenix is queried for the ingested "
    "span count, and annotations are written to the local annotation log.",
]


class DerivationRefused(Exception):
    """Mechanical refusal: required input missing or a gate is closed."""


# ---------------------------------------------------------------- helpers

def _parse_ts(value, what):
    if not isinstance(value, str) or not value:
        raise DerivationRefused(f"missing required input: {what}")
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as e:
        raise DerivationRefused(f"unparseable {what}: {value!r}") from e
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _pct(k, of):
    return round(k / of, 4) if of >= MIN_N else None


def _missing(k, of):
    return {"count": k, "of": of, "pct": _pct(k, of)}


def _flag(metric, value):
    if metric not in FLAGS or value is None:
        return None
    op, threshold = FLAGS[metric]
    return value > threshold if op == "gt" else value < threshold


def _rate(metric, num, den, missing, **extra):
    """A ratio, published only at den >= 30. Counts always."""
    value = round(num / den, 4) if den >= MIN_N else None
    out = {"num": num, "den": den, "n": den, "value": value,
           "missing": missing, "flag": _flag(metric, value)}
    if value is None:
        out["note"] = f"n={den} < {MIN_N}: counts only, no rate"
    out.update(extra)
    return out


def latest_versions(records):
    """RP-D1: one record per (type, id), the latest version.

    Latest = greatest ts, ties broken by position (the store is append-only).
    `_created` keeps the first-seen ts so windowing uses creation time."""
    best, created = {}, {}
    for i, r in enumerate(records):
        key = (r["type"], r["id"])
        ts = _parse_ts(r.get("ts"), f"ts on {r['type']} {r['id']}")
        created[key] = min(created.get(key, ts), ts)
        if key not in best or (ts, i) >= best[key][0]:
            best[key] = ((ts, i), r)
    return [dict(r, _created=created[k]) for k, (_, r) in best.items()]


def _validate(records):
    if records is None:
        raise DerivationRefused("missing required input: records")
    if not isinstance(records, list):
        raise DerivationRefused("records must be a list of trace records")
    for i, r in enumerate(records):
        if not isinstance(r, dict):
            raise DerivationRefused(f"record {i} is not an object")
        for field in ("type", "id", "ts"):
            if not r.get(field):
                raise DerivationRefused(f"record {i} missing required field: {field}")


def _tokens(j):
    """(prompt, completion, billed) from either record shape; None = absent."""
    t = j.get("tokens") or {}
    em = t.get("emission") or t
    billed = t.get("billed", j.get("billed_tokens"))
    return em.get("prompt"), em.get("completion"), billed


def _pattern(j):
    return j.get("pattern", (j.get("inputs") or {}).get("pattern"))


def _confidence(j):
    c = j.get("confidence", (j.get("inputs") or {}).get("confidence"))
    return c if isinstance(c, (int, float)) and not isinstance(c, bool) else None


def _situation(j):
    if j.get("situation_key"):
        return (j["seat"], j["situation_key"])
    inputs = {k: v for k, v in (j.get("inputs") or {}).items()
              if k not in VOLATILE_INPUTS}
    return (j["seat"], json.dumps(inputs, sort_keys=True, default=str))


def _is_question(turn):
    if "question" in turn:
        return bool(turn["question"])
    return (turn.get("role") in ADVISOR_ROLES
            and str(turn.get("content", "")).rstrip().endswith("?"))


def _percentile(values, q):
    if len(values) < MIN_N:
        return None
    xs = sorted(values)
    k = (len(xs) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (k - lo), 4)


# ---------------------------------------------------------------- metrics

def _seat_metrics(js, kids, parity, consistency_by_seat, cal_js, cal_kids):
    """metric.1..10 for one seat's judgments (already windowed, deduped)."""
    nj = len(js)
    tests = [t for j in js for t in kids[j["id"]]["Test"]]
    no_test = sum(1 for j in js if not kids[j["id"]]["Test"])
    resolved = [t for t in tests if t.get("state") in RESOLVED]
    passed = [t for t in resolved if t.get("state") == "resolved_pass"]
    expired = [t for t in tests if t.get("state") == "expired"]
    pending = [t for t in tests if t.get("state") in PENDING]
    m = {}

    m["metric.1"] = _rate("metric.1", len(passed), len(resolved),
                          _missing(no_test, nj),
                          pending=len(pending), expired=len(expired),
                          name="judgment test pass rate (NSM)")
    m["metric.2"] = _rate("metric.2", len(resolved), len(resolved) + len(expired),
                          _missing(no_test, nj), pending=len(pending),
                          name="test completion rate")

    # metric.3: advisor question turns per judgment
    rounds, no_turns = [], 0
    for j in js:
        turns = kids[j["id"]]["Turn"]
        if not turns:
            no_turns += 1
            continue
        rounds.append(sum(1 for t in turns if _is_question(t)))
    m["metric.3"] = {
        "name": "clarification rounds per judgment",
        "n": len(rounds), "total_rounds": sum(rounds),
        "p50": _percentile(rounds, 0.50), "p95": _percentile(rounds, 0.95),
        "counter_metric": "metric.1", "missing": _missing(no_turns, nj),
        **({"note": f"n={len(rounds)} < {MIN_N}: counts only, no percentiles"}
           if len(rounds) < MIN_N else {}),
    }

    # metric.4: UPR over turns that carry info_added
    turns = [t for j in js for t in kids[j["id"]]["Turn"]]
    labelled = [t for t in turns if isinstance(t.get("info_added"), bool)]
    m["metric.4"] = _rate("metric.4",
                          sum(1 for t in labelled if t["info_added"] is False),
                          len(labelled), _missing(len(turns) - len(labelled), len(turns)),
                          name="unnecessary path ratio")

    # metric.5: ungrounded sections share (flag >10%)
    sections, no_sections = [], 0
    for j in js:
        s = j.get("sections")
        if not isinstance(s, list) or not s:
            no_sections += 1
            continue
        sections.extend(s)
    grounded = sum(1 for s in sections
                   if isinstance(s, dict) and (s.get("grounded") or s.get("grounding")))
    g = _rate("metric.5", grounded, len(sections), _missing(no_sections, nj),
              name="groundedness")
    ungrounded = None if g["value"] is None else round(1 - g["value"], 4)
    g.update(ungrounded_share=ungrounded, flag=_flag("metric.5", ungrounded))
    m["metric.5"] = g

    # metric.7: consistency (precondition for metric.6)
    m["metric.7"] = consistency_by_seat

    # metric.6: calibration delta per pattern, rolling 30 days
    by_pattern, cal_missing, cal_resolved = {}, 0, 0
    for j in cal_js:
        rt = [t for t in cal_kids[j["id"]]["Test"] if t.get("state") in RESOLVED]
        if not rt:
            continue
        cal_resolved += 1
        p, c = _pattern(j), _confidence(j)
        if p is None or c is None:
            cal_missing += 1
            continue
        hit = any(t["state"] == "resolved_pass" for t in rt)
        by_pattern.setdefault(p, []).append((c, hit))
    precondition = consistency_by_seat.get("value")
    patterns = {}
    for p, rows in sorted(by_pattern.items()):
        n = len(rows)
        entry = {"n": n, "passed": sum(1 for _, h in rows if h)}
        if n >= MIN_N and precondition is not None and precondition >= 0.70:
            prior = statistics.fmean(c for c, _ in rows)
            actual = entry["passed"] / n
            delta = round(abs(prior - actual), 4)
            entry.update(prior_confidence=round(prior, 4),
                         actual_outcome_rate=round(actual, 4),
                         delta=delta, flag=_flag("metric.6", delta))
        else:
            entry["note"] = (f"n={n} < {MIN_N}: counts only" if n < MIN_N else
                             "precondition unmet: metric.7 consistency not >= 70%")
        patterns[p] = entry
    m["metric.6"] = {"name": "calibration delta", "window_days": 30,
                     "patterns": patterns, "resolved_judgments": cal_resolved,
                     "precondition_metric.7": precondition,
                     "missing": _missing(cal_missing, cal_resolved)}

    # metric.8: cost per successful judgment (D5 gate, D9 nulls)
    m["metric.8"] = _cost(js, len(passed), parity)

    # metric.9: planning efficiency
    runs = sum(len(kids[j["id"]]["MachineRun"]) for j in js)
    reasoning = sum(1 for t in turns if t.get("role") in ADVISOR_ROLES)
    no_run = sum(1 for j in js if not kids[j["id"]]["MachineRun"])
    m["metric.9"] = _rate("metric.9", runs, runs + reasoning,
                          _missing(no_run, nj), machine_runs=runs,
                          reasoning_turns=reasoning, name="planning efficiency")

    m["metric.10"] = _rate("metric.10", len(expired), len(resolved) + len(expired),
                           _missing(no_test, nj), name="expire rate")
    return m


def _parity_measured(parity):
    return isinstance(parity, dict) and parity.get("measured") is True


def _cost(js, n_passed, parity):
    if not _parity_measured(parity):
        return {"name": "cost per successful judgment", "refused": True,
                "reason": "D5: token emission-vs-billed parity is unmeasured; "
                          "metric.8 is mechanically refused"}
    billed = [_tokens(j)[2] for j in js]
    nulls = sum(1 for b in billed if b is None)
    out = {"name": "cost per successful judgment", "refused": False,
           "num_tokens": None, "den": n_passed, "n": n_passed, "value": None,
           "missing": _missing(nulls, len(js))}
    if nulls:
        out["unknown"] = f"billed_tokens null on {nulls} judgment(s): total is unknown"
        return out
    total = sum(b for b in billed if b)  # zeros were alerted and excluded (D9)
    out["num_tokens"] = total
    if n_passed >= MIN_N:
        out["value"] = round(total / n_passed, 2)
    else:
        out["note"] = f"n={n_passed} < {MIN_N}: counts only, no rate"
    return out


def _consistency(js):
    with_p = [j for j in js if _pattern(j) is not None]
    groups = {}
    for j in with_p:
        groups.setdefault(_situation(j), set()).add(_pattern(j))
    multi = [ps for key, ps in groups.items()
             if sum(1 for j in with_p if _situation(j) == key) >= 2]
    consistent = sum(1 for ps in multi if len(ps) == 1)
    return _rate("metric.7", consistent, len(multi),
                 _missing(len(js) - len(with_p), len(js)),
                 name="consistency")


def derive(records, window_start, window_end, token_parity=None):
    """Compute metric.1..10 per seat over [window_start, window_end).

    Refuses (raises DerivationRefused) on any missing required input:
    no partial output, no zero-filled placeholders (RP-D4)."""
    _validate(records)
    start = _parse_ts(window_start, "window_start")
    end = _parse_ts(window_end, "window_end")
    if start >= end:
        raise DerivationRefused("window_start must be before window_end")

    recs = latest_versions(records)
    judgments = {r["id"]: r for r in recs if r["type"] == "Judgment"}
    missing_attr, alerts = [], []

    def empty():
        return {t: [] for t in CHILD_TYPES}

    kids = {}
    for r in recs:
        if r["type"] == "Judgment":
            if not r.get("seat"):
                missing_attr.append({"type": "Judgment", "id": r["id"],
                                     "reason": "judgment has no seat"})
            continue
        if r["type"] not in CHILD_TYPES:
            continue
        jid = r.get("judgment_id")
        parent = judgments.get(jid)
        if parent is None or not parent.get("seat"):
            missing_attr.append({"type": r["type"], "id": r["id"],
                                 "judgment_id": jid,
                                 "reason": "no judgment_id" if not jid
                                 else "judgment_id names no attributable judgment"})
            continue
        kids.setdefault(jid, empty())[r["type"]].append(r)

    attributed = [j for j in judgments.values() if j.get("seat")]
    for j in attributed:
        kids.setdefault(j["id"], empty())
    in_window = [j for j in attributed if start <= j["_created"] < end]
    cal_start = end - timedelta(days=30)
    cal_window = [j for j in attributed if cal_start <= j["_created"] < end]

    # D9: zero tokens are bugs -> alert, removed from token metrics
    for j in in_window:
        p, c, b = _tokens(j)
        zeros = [k for k, v in (("prompt", p), ("completion", c), ("billed", b))
                 if v == 0]
        if zeros:
            alerts.append({"kind": "zero_tokens", "judgment_id": j["id"],
                           "seat": j["seat"], "fields": zeros,
                           "detail": "0 tokens is an instrument bug, not a metric"})

    seats = sorted({j["seat"] for j in in_window})
    out_seats = {}
    for s in seats:
        js = [j for j in in_window if j["seat"] == s]
        cal = [j for j in cal_window if j["seat"] == s]
        out_seats[s] = {"judgments": len(js),
                        **_seat_metrics(js, kids, token_parity,
                                        _consistency(cal), cal, kids)}

    refusals = {}
    if not _parity_measured(token_parity):
        for g in ("metric.8", "G10", "G11"):
            refusals[g] = "D5: token emission-vs-billed parity unmeasured"

    return {
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "n_judgments": len(in_window),
        "judgment_ids": sorted(j["id"] for j in in_window),
        "seats": out_seats,
        "missing_attribution": {"count": len(missing_attr),
                                "records": missing_attr},
        "alerts": alerts,
        "refusals": refusals,
        "token_parity": token_parity if token_parity is not None
        else {"measured": False},
        "deviations": DEVIATIONS,
    }


# ------------------------------------------------- G13: verdict_justified

def in_sample(trace_id, rate=SAMPLE_RATE):
    """Deterministic 20% sample seeded by the trace_id hash: never chosen."""
    h = int(hashlib.sha256(trace_id.encode()).hexdigest()[:8], 16)
    return (h % 10_000) < int(rate * 10_000)


def judge_case(judgment, machine_runs):
    """The evidence the judge sees. Refuses when any part is missing."""
    if not machine_runs:
        raise DerivationRefused(f"{judgment['id']}: no MachineRun (no checks, no verdict)")
    run = machine_runs[-1]
    if run.get("verdict") in (None, "") or not isinstance(run.get("checks"), list):
        raise DerivationRefused(f"{judgment['id']}: MachineRun lacks verdict or checks")
    if not isinstance(judgment.get("inputs"), dict):
        raise DerivationRefused(f"{judgment['id']}: judgment lacks inputs")
    inputs = {k: v for k, v in judgment["inputs"].items() if v is not None}
    return {"judgment_id": judgment["id"], "seat": judgment["seat"],
            "inputs": inputs, "checks": run["checks"], "verdict": run["verdict"]}


def render_prompt(case, template=None):
    template = template if template is not None else JUDGE_PROMPT_PATH.read_text()
    return (template
            .replace("{{seat}}", case["seat"])
            .replace("{{inputs}}", json.dumps(case["inputs"], indent=2, sort_keys=True))
            .replace("{{checks}}", json.dumps(case["checks"], indent=2))
            .replace("{{verdict}}", json.dumps(case["verdict"])))


def parse_judgment(text):
    """Judge output -> {score, explanation, evidence}. Refuses anything else:
    a verdict without justifying evidence is not an annotation (RP-E1)."""
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`").split("\n", 1)[-1]
    lo, hi = s.find("{"), s.rfind("}")
    if lo < 0 or hi < lo:
        raise DerivationRefused(f"judge output is not JSON: {text[:200]!r}")
    try:
        obj = json.loads(s[lo:hi + 1])
    except json.JSONDecodeError as e:
        raise DerivationRefused(f"judge output is not JSON: {e}") from e
    score = obj.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 1:
        raise DerivationRefused(f"judge score must be in [0,1], got {score!r}")
    evidence = obj.get("evidence")
    if not isinstance(evidence, list) or not [e for e in evidence if str(e).strip()]:
        raise DerivationRefused("judge output carries no justifying evidence")
    if not str(obj.get("explanation", "")).strip():
        raise DerivationRefused("judge output carries no explanation")
    return {"score": float(score), "explanation": obj["explanation"],
            "evidence": [str(e) for e in evidence]}


def judge_from_env(env=None):
    """The judge function named by PHOENIX_EVALS_PROVIDER / _MODEL.

    Providers: `claude-cli` (claude -p) and `anthropic` (Messages API).
    Unset config is a refusal, never a default."""
    env = os.environ if env is None else env
    provider = (env.get("PHOENIX_EVALS_PROVIDER") or "").strip()
    model = (env.get("PHOENIX_EVALS_MODEL") or "").strip()
    if not provider or not model:
        raise DerivationRefused("missing required input: PHOENIX_EVALS_PROVIDER "
                                "and PHOENIX_EVALS_MODEL must both be set")
    if provider == "claude-cli":
        def call(prompt):
            p = subprocess.run(["claude", "-p", "--model", model],
                               input=prompt, capture_output=True, text=True,
                               timeout=300)
            if p.returncode != 0:
                raise DerivationRefused(f"judge call failed: {p.stderr[:300]}")
            return p.stdout
    elif provider == "anthropic":
        key = env.get("ANTHROPIC_API_KEY")
        if not key:
            raise DerivationRefused("missing required input: ANTHROPIC_API_KEY")
        base = (env.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com").rstrip("/")

        def call(prompt):
            body = json.dumps({"model": model, "max_tokens": 800,
                               "messages": [{"role": "user", "content": prompt}]})
            req = urllib.request.Request(
                f"{base}/v1/messages", data=body.encode(), method="POST",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"})
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.load(r)
            return "".join(b.get("text", "") for b in data.get("content", []))
    else:
        raise DerivationRefused(f"unknown PHOENIX_EVALS_PROVIDER: {provider!r}")
    call.provider, call.model = provider, model
    return call


def read_annotations(path=None):
    path = Path(path or ANNOTATIONS_PATH)
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def run_verdict_justified(records, judge_fn, window_start, window_end,
                          annotations=None, template=None):
    """One eval pass: sample judgments in the window, judge each once.

    Never runs on a judgment it already annotated, never on its own
    annotation records. Returns (new_annotations, errors)."""
    _validate(records)
    start = _parse_ts(window_start, "window_start")
    end = _parse_ts(window_end, "window_end")
    if judge_fn is None:
        raise DerivationRefused("missing required input: judge_fn")
    done = {a["judgment_id"] for a in (annotations or [])}
    recs = latest_versions(records)
    runs = {}
    for r in recs:
        if r["type"] == "MachineRun":
            runs.setdefault(r.get("judgment_id"), []).append(r)
    new, errors = [], []
    for j in sorted((r for r in recs if r["type"] == "Judgment"),
                    key=lambda r: r["_created"]):
        if (not j.get("seat") or not (start <= j["_created"] < end)
                or j["id"] in done or not in_sample(j["id"])):
            continue
        try:
            case = judge_case(j, runs.get(j["id"], []))
            verdict = parse_judgment(judge_fn(render_prompt(case, template)))
        except DerivationRefused as e:
            errors.append({"judgment_id": j["id"], "seat": j.get("seat"),
                           "error": str(e)})
            continue
        new.append({
            "type": "EvalAnnotation", "eval": EVAL_NAME, "version": EVAL_VERSION,
            "judgment_id": j["id"], "trace_id": j["id"], "seat": j["seat"],
            "ts": datetime.now(timezone.utc).isoformat(),
            "judge": {"provider": getattr(judge_fn, "provider", "injected"),
                      "model": getattr(judge_fn, "model", "injected")},
            "case": {"verdict": case["verdict"],
                     "checks": [c.get("id") for c in case["checks"]]},
            **verdict,
        })
    return new, errors


def g13(annotations, window_start, window_end):
    """G13 = mean verdict_justified score per seat over the window.
    Counts only below n=30. Measures justification, not correctness."""
    start = _parse_ts(window_start, "window_start")
    end = _parse_ts(window_end, "window_end")
    by_seat = {}
    for a in annotations:
        if a.get("eval") != EVAL_NAME:
            continue
        if not (start <= _parse_ts(a.get("ts"), "annotation ts") < end):
            continue
        by_seat.setdefault(a["seat"], []).append(a["score"])
    out = {}
    for seat, scores in sorted(by_seat.items()):
        n = len(scores)
        out[seat] = {"n": n, "score_sum": round(sum(scores), 4),
                     "value": round(sum(scores) / n, 4) if n >= MIN_N else None,
                     **({"note": f"n={n} < {MIN_N}: counts only"} if n < MIN_N else {})}
    return {"metric": "G13", "eval": f"{EVAL_NAME}/{EVAL_VERSION}",
            "sample_rate": SAMPLE_RATE, "measures": "justification, not correctness",
            "seats": out, "deviations": [DEVIATIONS[-1]]}


def phoenix_seat_span_count(env=None):
    """Ingested seat.* spans in Phoenix (the parity cross-check). None if
    Phoenix is not configured or not reachable: unknown, never 0."""
    env = os.environ if env is None else env
    base = (env.get("PHOENIX_BASE_URL") or env.get("PHOENIX_ENDPOINT") or "").strip()
    if not base:
        return None
    base = base.split("/v1/")[0].rstrip("/")
    project = env.get("PHOENIX_EVALS_PROJECT") or "default"
    try:
        with urllib.request.urlopen(
                f"{base}/v1/projects/{project}/spans?limit=1000", timeout=5) as r:
            data = json.load(r).get("data", [])
    except (OSError, ValueError):
        return None
    return sum(1 for s in data if str(s.get("name", "")).startswith("seat."))


# --------------------------------------------------------------------- cli

def _load_trace(path=None):
    path = Path(path or TRACE_PATH)
    if not path.exists():
        raise DerivationRefused(f"missing required input: trace store {path}")
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="derivation")
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("metrics")
    m.add_argument("--start", required=True)
    m.add_argument("--end", required=True)
    m.add_argument("--parity", help="JSON file with {measured: true, ...}")
    m.add_argument("--trace")
    e = sub.add_parser("eval")
    e.add_argument("--start")
    e.add_argument("--end")
    e.add_argument("--trace")
    a = ap.parse_args(argv)
    try:
        records = _load_trace(a.trace)
        if a.cmd == "metrics":
            parity = json.loads(Path(a.parity).read_text()) if a.parity else None
            print(json.dumps(derive(records, a.start, a.end, parity), indent=2))
            return 0
        now = datetime.now(timezone.utc)
        start = a.start or (now - timedelta(hours=1)).isoformat()
        end = a.end or now.isoformat()
        prior = read_annotations()
        new, errors = run_verdict_justified(records, judge_from_env(), start, end, prior)
        ANNOTATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(ANNOTATIONS_PATH, "a") as f:
            for row in new:
                f.write(json.dumps(row) + "\n")
        print(json.dumps({"annotated": len(new), "errors": errors,
                          "phoenix_seat_spans": phoenix_seat_span_count(),
                          # rollup window closes after the judging finished
                          "G13": g13(prior + new, "1970-01-01T00:00:00+00:00",
                                     (datetime.now(timezone.utc)
                                      + timedelta(seconds=1)).isoformat())},
                         indent=2))
        return 0
    except DerivationRefused as err:
        print(f"REFUSED: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
