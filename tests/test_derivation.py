"""Metric derivation redproofs (metric-derivation-20261007).

RP-D1 dedupe · RP-D2 counts below n=30 · RP-D3 missing attribution ·
RP-D4 mechanical refusal · RP-D5 G13 per definition · RP-E1 live
verdict_justified annotations carry their evidence.

Plus the hard constraints: D5 parity gate, D9 zero/null tokens.

Run: python3 tests/test_derivation.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import derivation as d  # noqa: E402

PASS, FAIL = [], []
W0, W1 = "2026-10-01T00:00:00+00:00", "2026-10-08T00:00:00+00:00"


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("ok  " if cond else "FAIL") + f" {name}" + (f" — {detail}" if detail and not cond else ""))


def refused(fn, *a, **k):
    try:
        fn(*a, **k)
    except d.DerivationRefused as e:
        return str(e)
    return None


def ts(day, sec=0):
    return f"2026-10-0{day}T00:00:{sec:02d}+00:00"


def judgment(jid, seat="s", day=2, sec=0, **extra):
    return {"type": "Judgment", "id": jid, "judgment_id": jid, "seat": seat,
            "ts": ts(day, sec), "inputs": {"q": jid}, **extra}


def test(tid, jid, state, day=2, sec=1, seat="s"):
    return {"type": "Test", "id": tid, "judgment_id": jid, "seat": seat,
            "ts": ts(day, sec), "state": state}


def machine(jid, verdict="DEFECT", day=2):
    return {"type": "MachineRun", "id": f"m_{jid}", "judgment_id": jid,
            "ts": ts(day, 1), "verdict": verdict,
            "checks": [{"id": "required_fields", "passed": True}]}


def bulk(n, state="resolved_pass", seat="s", **extra):
    out = []
    for i in range(n):
        jid = f"j{seat}{i}"
        out += [judgment(jid, seat=seat, **extra), test(f"t{seat}{i}", jid, state, seat=seat)]
    return out


# ---- RP-D1: duplicate versions -> one record per judgment id (latest)
recs = [judgment("j1"), judgment("j1", sec=5, tokens={"emission": {"prompt": 7, "completion": 3}, "billed": None}),
        judgment("j1", sec=9, tokens={"emission": {"prompt": 8, "completion": 3}, "billed": None}),
        judgment("j2"),
        test("t1", "j1", "delivered"), test("t1", "j1", "resolved_pass", sec=7)]
out = d.derive(recs, W0, W1)
check("RP-D1 emitted count equals distinct judgment ids",
      out["n_judgments"] == 2 and out["judgment_ids"] == ["j1", "j2"], out["judgment_ids"])
latest = [r for r in d.latest_versions(recs) if r["id"] == "j1"]
check("RP-D1 the kept version is the latest", len(latest) == 1 and latest[0]["tokens"]["emission"]["prompt"] == 8)
m1 = out["seats"]["s"]["metric.1"]
check("RP-D1 a versioned test counts once, at its latest state",
      m1["num"] == 1 and m1["den"] == 1 and m1["pending"] == 0, m1)
# latest-by-ts even when an older version is appended later
shuffled = [judgment("j1", sec=9, inputs={"v": "new"}), judgment("j1", sec=1, inputs={"v": "old"})]
check("RP-D1 latest is by ts, not file order",
      d.latest_versions(shuffled)[0]["inputs"] == {"v": "new"})
check("RP-D1 windowing uses creation ts (annotation does not move a judgment)",
      d.derive([judgment("j1", day=1), judgment("j1", day=3)], ts(1), ts(2))["n_judgments"] == 1)

# ---- RP-D2: n < 30 -> counts only, no rate/percentage
def rate_fields(seat_out):
    found = []
    for k, m in seat_out.items():
        if not isinstance(m, dict):
            continue
        for f in ("value", "p50", "p95", "ungrounded_share"):
            if m.get(f) is not None:
                found.append(f"{k}.{f}")
        if m.get("missing", {}).get("pct") is not None:
            found.append(f"{k}.missing.pct")
        for p in (m.get("patterns") or {}).values():
            if "delta" in p:
                found.append(f"{k}.delta")
    return found


small = d.derive(bulk(29), W0, W1)["seats"]["s"]
check("RP-D2 n=29: no rate, percentage or percentile computed", rate_fields(small) == [], rate_fields(small))
check("RP-D2 n=29: raw counts still present", small["metric.1"]["num"] == 29 and small["metric.1"]["den"] == 29)
check("RP-D2 n=29: the note says counts only", "counts only" in small["metric.1"]["note"])
big = d.derive(bulk(30), W0, W1)["seats"]["s"]
check("RP-D2 n=30: the rate is published", big["metric.1"]["value"] == 1.0, big["metric.1"])
mixed = bulk(20) + bulk(10, state="expired", seat="s")[0:0]  # one seat, n=20
mixed += [judgment(f"x{i}") for i in range(15)] + [test(f"tx{i}", f"x{i}", "expired") for i in range(15)]
mx = d.derive(mixed, W0, W1)["seats"]["s"]
check("RP-D2 metric.10 published once resolved+expired reaches 30",
      mx["metric.10"]["den"] == 35 and mx["metric.10"]["value"] == 0.4286, mx["metric.10"])
q = [judgment(f"q{i}") for i in range(30)] + [
    {"type": "Turn", "id": f"tu{i}", "judgment_id": f"q{i}", "ts": ts(2, 1), "role": "seat", "content": "which file?"}
    for i in range(30)]
m3 = d.derive(q, W0, W1)["seats"]["s"]["metric.3"]
check("RP-D2 metric.3 percentiles only at n>=30", m3["p50"] == 1 and m3["p95"] == 1, m3)
m3s = d.derive(q[:10] + q[30:40], W0, W1)["seats"]["s"]["metric.3"]
check("RP-D2 metric.3 below 30 -> counts only", m3s["p50"] is None and m3s["total_rounds"] == 10, m3s)

# ---- RP-D3: unattributable records -> separate output, never merged
orph = [judgment("ok"), test("t_ok", "ok", "resolved_pass"),
        test("t_orph", "ghost", "resolved_pass"),
        {"type": "Turn", "id": "tu_none", "ts": ts(2), "role": "seat", "content": "?"},
        {"type": "Judgment", "id": "noseat", "judgment_id": "noseat", "ts": ts(2), "inputs": {}},
        test("t_noseat", "noseat", "resolved_fail")]
o = d.derive(orph, W0, W1)
ma = {r["id"]: r["reason"] for r in o["missing_attribution"]["records"]}
check("RP-D3 every unattributable record is listed",
      set(ma) == {"t_orph", "tu_none", "noseat", "t_noseat"}, ma)
check("RP-D3 count matches the listed records", o["missing_attribution"]["count"] == 4)
m1 = o["seats"]["s"]["metric.1"]
check("RP-D3 attributed aggregates hold only attributed records",
      m1["num"] == 1 and m1["den"] == 1 and list(o["seats"]) == ["s"], m1)
check("RP-D3 reasons are stated", all(ma.values()))

# ---- RP-D4: missing required inputs -> refusal, no partial output
cases = {
    "records None": (None, W0, W1),
    "records not a list": ({"a": 1}, W0, W1),
    "window_start missing": ([judgment("a")], None, W1),
    "window_end unparseable": ([judgment("a")], W0, "next tuesday"),
    "window inverted": ([judgment("a")], W1, W0),
    "record without ts": ([{"type": "Judgment", "id": "a", "seat": "s"}], W0, W1),
    "record without id": ([{"type": "Judgment", "ts": ts(2), "seat": "s"}], W0, W1),
}
for name, args in cases.items():
    msg = refused(d.derive, *args)
    check(f"RP-D4 refuses: {name}", msg is not None, "no refusal")
check("RP-D4 a refusal returns no output object (raises, nothing partial)",
      refused(d.derive, None, W0, W1).startswith("missing required input"))
check("RP-D4 judge config unset is a refusal, not a default",
      refused(d.judge_from_env, {}) is not None)
check("RP-D4 judge case without a MachineRun is refused",
      refused(d.judge_case, judgment("a"), []) is not None)
check("RP-D4 CLI exits 2 on a missing trace store",
      d.main(["metrics", "--start", W0, "--end", W1, "--trace", "/nonexistent.jsonl"]) == 2)

# ---- D5: metric.8 / G10 / G11 refused while parity unmeasured
tok = {"emission": {"prompt": 10, "completion": 5}, "billed": 100}
o = d.derive(bulk(30, tokens=tok), W0, W1)
check("D5 metric.8 refused when parity unmeasured",
      o["seats"]["s"]["metric.8"]["refused"] is True and "num_tokens" not in o["seats"]["s"]["metric.8"])
check("D5 G10 and G11 refused too", set(o["refusals"]) == {"metric.8", "G10", "G11"})
check("D5 parity={measured:false} is still refused",
      d.derive(bulk(30, tokens=tok), W0, W1, {"measured": False})["seats"]["s"]["metric.8"]["refused"])
o = d.derive(bulk(30, tokens=tok), W0, W1, {"measured": True, "ratio": 1.02})
check("D5 measured parity opens metric.8", o["seats"]["s"]["metric.8"]["value"] == 100.0, o["seats"]["s"]["metric.8"])

# ---- D9: zero = alert, null = unknown
nul = {"emission": {"prompt": 10, "completion": 5}, "billed": None}
o = d.derive(bulk(30, tokens=nul), W0, W1, {"measured": True})
m8 = o["seats"]["s"]["metric.8"]
check("D9 null billed propagates as unknown, never 0",
      m8["value"] is None and m8["num_tokens"] is None and "unknown" in m8, m8)
zero = {"emission": {"prompt": 0, "completion": 5}, "billed": 0}
o = d.derive([judgment("z", tokens=zero)], W0, W1)
check("D9 zero tokens raise an alert", o["alerts"] and o["alerts"][0]["fields"] == ["prompt", "billed"], o["alerts"])

# ---- metric.6 / .7 behave per the map
pat = []
for i in range(30):
    jid = f"p{i}"
    pat += [judgment(jid, pattern="P", confidence=0.9, situation_key=f"k{i // 2}"),
            test(f"tp{i}", jid, "resolved_pass" if i < 21 else "resolved_fail")]
pat += [judgment(f"pp{i}", pattern="P", situation_key=f"k{i}") for i in range(30, 60)]
pat += [judgment(f"pq{i}", pattern="P", situation_key=f"k{i}") for i in range(30, 60)]
s = d.derive(pat, W0, W1)["seats"]["s"]
check("metric.7 consistency computed over similar-situation groups",
      s["metric.7"]["value"] == 1.0 and s["metric.7"]["den"] == 45, s["metric.7"])
p = s["metric.6"]["patterns"]["P"]
check("metric.6 delta = |prior - actual| = |0.9 - 0.7| and flagged >15pp",
      p.get("delta") == 0.2 and p.get("flag") is True, p)

# ---- RP-D5: G13 matches the map definition
ids = [f"judg_{i:06x}" for i in range(20_000)]
share = sum(d.in_sample(i) for i in ids) / len(ids)
check("RP-D5 sample is ~20% of trace ids", 0.19 < share < 0.21, share)
check("RP-D5 sample is seeded by trace_id hash (deterministic)",
      [d.in_sample(i) for i in ids[:500]] == [d.in_sample(i) for i in ids[:500]])
ann = [{"eval": "verdict_justified", "seat": "a", "ts": ts(2), "score": 1.0 if i % 4 else 0.0}
       for i in range(40)] + [{"eval": "verdict_justified", "seat": "b", "ts": ts(2), "score": 1.0}] * 5
g = d.g13(ann, W0, W1)
check("RP-D5 G13 = mean score per seat at n>=30", g["seats"]["a"]["value"] == 0.75, g["seats"]["a"])
check("RP-D5 G13 counts only below n=30", g["seats"]["b"]["value"] is None and g["seats"]["b"]["n"] == 5)
check("RP-D5 G13 states it measures justification, not correctness",
      g["measures"] == "justification, not correctness")
check("RP-D5 deviations are documented in the output",
      g["deviations"] and d.derive([judgment("a")], W0, W1)["deviations"] == d.DEVIATIONS)
check("RP-D5 judge prompt is versioned at evals/verdict_justified/v1.md",
      d.JUDGE_PROMPT_PATH.exists() and "{{verdict}}" in d.JUDGE_PROMPT_PATH.read_text())

calls = []


def fake_judge(prompt):
    calls.append(prompt)
    return '{"score": 1, "explanation": "checks pass", "evidence": ["check:required_fields"]}'


sampled = [i for i in ids if d.in_sample(i)][:3]
unsampled = [i for i in ids if not d.in_sample(i)][:3]
live = []
for jid in sampled + unsampled:
    live += [judgment(jid), machine(jid)]
new, errs = d.run_verdict_justified(live, fake_judge, W0, W1, template=d.JUDGE_PROMPT_PATH.read_text())
check("RP-D5 only sampled judgments are judged", sorted(a["judgment_id"] for a in new) == sorted(sampled))
again, _ = d.run_verdict_justified(live, fake_judge, W0, W1, annotations=new)
check("RP-D5 never runs twice on a judgment it annotated", again == [])
check("RP-D5 its own annotation records are never candidates",
      all(r["type"] != "EvalAnnotation" for r in live) and
      d.run_verdict_justified(live + [dict(a, id=a["judgment_id"] + "_e") for a in new],
                              fake_judge, W0, W1, annotations=new)[0] == [])
check("RP-D5 the prompt carries inputs, checks and verdict",
      all('"DEFECT"' in c and "required_fields" in c and '"q"' in c for c in calls))

# ---- RP-E1: verdicts carry their justifying evidence
check("RP-E1 judge output without evidence is refused",
      refused(d.parse_judgment, '{"score": 1, "explanation": "x", "evidence": []}') is not None)
check("RP-E1 judge output without explanation is refused",
      refused(d.parse_judgment, '{"score": 1, "explanation": "", "evidence": ["c"]}') is not None)
check("RP-E1 out-of-range score is refused",
      refused(d.parse_judgment, '{"score": 1.4, "explanation": "x", "evidence": ["c"]}') is not None)
bad, errs = d.run_verdict_justified(live, lambda p: '{"score": 1}', W0, W1)
check("RP-E1 an evidence-free verdict becomes an error, never an annotation", bad == [] and len(errs) == 3)

live_ann = d.read_annotations()
trace_ids = {r["id"] for r in d._load_trace() if r["type"] == "Judgment"}
check("RP-E1 live run: annotations exist", len(live_ann) > 0, "run: python3 core/derivation.py eval --start ...")
check("RP-E1 live run: every annotation names a judgment in the live trace store",
      live_ann and all(a["judgment_id"] in trace_ids for a in live_ann))
check("RP-E1 live run: every annotation carries evidence and an explanation",
      live_ann and all(a["evidence"] and a["explanation"].strip() for a in live_ann))
check("RP-E1 live run: every annotated judgment was in the 20% sample",
      live_ann and all(d.in_sample(a["trace_id"]) for a in live_ann))
check("RP-E1 live run: the judge was a real provider, not an injected stub",
      live_ann and all(a["judge"]["provider"] != "injected" for a in live_ann))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
