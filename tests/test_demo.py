"""Tests for the landing page (GET / and /demo) and its live panel (POST /demo/advise).

Runs against the Starlette app in-process with an API key set, so the
checks also prove the demo routes stay open while /mcp stays closed, and
that the key never appears in the served page. Trace, calibration and
advise telemetry are redirected to a temp dir.

Run: .venv/bin/python tests/test_demo.py
"""
import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from starlette.testclient import TestClient  # noqa: E402

from core import advise, calibration, trace  # noqa: E402
import server  # noqa: E402

PASS = []
FAIL = []
KEY = "demo-test-key-0123456789"


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("ok  " if cond else "FAIL") + f" {name}" + (f" — {detail}" if detail and not cond else ""))


def main():
    tmp = Path(tempfile.mkdtemp())
    trace.TRACE_PATH = tmp / "trace.jsonl"
    calibration.CAL_DIR = tmp / "calibration"
    advise.TELEMETRY_PATH = tmp / "telemetry" / "advise_calls.jsonl"

    with TestClient(server.build_app(api_key=KEY)) as c:
        for path in ("/", "/demo"):
            r = c.get(path)
            check(f"landing.get {path} 200 html",
                  r.status_code == 200 and r.headers["content-type"].startswith("text/html"),
                  f"{r.status_code} {r.headers.get('content-type')}")
        page = r.text
        check("landing.live panel posts to /demo/advise", "/demo/advise" in page)
        check("landing.holds no key", KEY not in page)
        check("landing.no canned chat answer", "retry-storm pattern (22/23 correct)" not in page)
        check("landing.no unpublished npx package", "npx" not in page and "@debug-assist/domain-expertise" not in page)
        check("landing.config is http to /mcp/ with a key placeholder",
              '"type": "http"' in page and 'onrender.com/mcp/"' in page and "Bearer ${MCP_API_KEY}" in page)
        # On Render, POST /mcp (no slash) answers 307 to /mcp/ (measured 2026-10-08);
        # a curl without -L would fail the install smoke test.
        check("landing.no slashless /mcp URL", 'onrender.com/mcp"' not in page and "onrender.com/mcp<" not in page)
        check("landing.install workflow reads the /health fingerprint", "key_sha256_8" in page and 'id="install"' in page)
        check("landing.figures labelled as targets",
              "measured, not claimed" not in page and page.count("target pass rate") == 6)
        imgs = sorted(set(re.findall(r'src="(/static/img/[^"]+)"', page)))
        check("landing.no inline images", "base64," not in page)
        bad = [u for u in imgs if c.get(u).status_code != 200]
        check("landing.every image is served", len(imgs) >= 8 and not bad, f"{len(imgs)} imgs, missing {bad}")

        r = c.post("/mcp/", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        check("demo.mcp still needs the key", r.status_code == 401, r.status_code)

        for alias, seat in (("allspaw", "allspaw-debugassist"), ("qe-ic-advisor", "qe-ic-debugassist")):
            r = c.post("/demo/advise", json={"seat_alias": alias, "question": "q", "evidence": "e"})
            body = r.json()
            check(f"demo.advise {alias} -> {seat}",
                  r.status_code == 200 and body.get("seat") == seat and "verdict" in body
                  and "judgment_id" in body and "falsifiable_test" in body, f"{r.status_code} {body}")

        r = c.post("/demo/advise", json={"seat_alias": "nope", "question": "q", "evidence": "e"})
        check("demo.advise unknown alias -> 400 naming known aliases",
              r.status_code == 400 and "unknown seat_alias" in r.json().get("error", "")
              and "allspaw" in r.json()["error"], f"{r.status_code} {r.text}")

        r = c.post("/demo/advise", json={"seat_alias": "defect-triage", "question": "q", "evidence": "e",
                                         "context": {"repo_name": "myrepo"}})
        check("demo.advise defect-triage with context.repo_name -> 200, not DECLINED",
              r.status_code == 200 and r.json().get("seat") == "defect-triage"
              and r.json().get("verdict") != "DECLINED", f"{r.status_code} {r.text[:300]}")
        r = c.post("/demo/advise", json={"seat_alias": "defect-triage", "question": "q", "evidence": "e"})
        check("demo.advise defect-triage without context -> 400 naming context.repo_name",
              r.status_code == 400 and "context.repo_name" in r.json().get("error", ""), f"{r.status_code} {r.text}")
        r = c.post("/demo/advise", json={"seat_alias": "cause-locator", "question": "q", "evidence": "e",
                                         "context": {"repo_listing": ["a.py", "b.py"]}})
        check("demo.advise cause-locator with context.repo_listing -> 200",
              r.status_code == 200 and r.json().get("seat") == "cause-locator", f"{r.status_code} {r.text[:300]}")
        r = c.post("/demo/advise", json={"seat_alias": "defect-triage", "question": "q", "evidence": "e",
                                         "context": "myrepo"})
        check("demo.advise non-object context -> 400", r.status_code == 400
              and "context" in r.json().get("error", ""), f"{r.status_code} {r.text}")

        opts = re.findall(r'<option value="([^"]+)">', page)
        check("landing.advisor dropdown lists the four advisors",
              '<select id="try-seat"' in page and opts == ["allspaw", "qe-ic-advisor", "defect-triage", "cause-locator"], opts)
        check("landing.no alias buttons left", "try-seats" not in page)
        check("landing.repo fields start hidden", '<div id="try-repo" hidden>' in page
              and 'id="try-repo-name"' in page and 'id="try-repo-listing"' in page)
        fields = dict(re.findall(r'"([a-z-]+)": "(repo_[a-z]+)"', page))
        check("landing.repo fields shown only for the repo-dependent advisors, each with the key its template requires",
              fields == {"defect-triage": "repo_name", "cause-locator": "repo_listing"}, fields)
        check("landing.repro textarea sits in the cause-locator row, labelled and prefilled",
              re.search(r'<div id="try-repo-listing-row">.*?<label for="try-repro">Repro output — stack trace or failing assertion</label>'
                        r'<textarea id="try-repro">Traceback[^<]*auth/retry\.py[^<]*</textarea></div>', page, re.S) is not None)
        check("landing.repro wired to context.repro_output", "repro_output: document.getElementById('try-repro')" in page)
        check("landing.cause-locator evidence prefilled (an empty issue.description is MISSING_FIELD)",
              '"cause-locator": "Login crashes on the retry path' in page)
        listing = re.search(r'<textarea id="try-repo-listing"[^>]*>([^<]*)</textarea>', page).group(1).split()
        repro = re.search(r'<textarea id="try-repro">([^<]*)</textarea>', page).group(1)
        r = c.post("/demo/advise", json={"seat_alias": "cause-locator", "question": "Which file and lines hold the cause?",
                                         "evidence": "Login crashes on retry",
                                         "context": {"repo_listing": listing, "repro_output": repro.strip()}})
        body = r.json()
        check("demo.advise cause-locator with the page's prefilled listing + repro -> 200 JudgmentResult, past NO_LOCATING_CUE",
              r.status_code == 200 and body.get("verdict") in ("CANDIDATES", "CAUSE_NOT_FOUND")
              and body["machine_result"].get("code") != "NO_LOCATING_CUE"
              and (body["verdict"] == "CANDIDATES" or body["machine_result"].get("needs")), f"{r.status_code} {r.text[:300]}")
        tmpl = {t["alias"]: json.dumps(t["build_inputs"]) for t in advise.load_templates()["templates"]}
        check("landing.repo field keys match config/question_templates.yaml",
              all("{context." + k + "}" in tmpl[a] for a, k in fields.items()), fields)

        r = c.post("/demo/advise", json={"seat_alias": "allspaw"})
        check("demo.advise missing fields -> 400", r.status_code == 400, r.status_code)
        r = c.post("/demo/advise", content=b"not json", headers={"Content-Type": "application/json"})
        check("demo.advise non-JSON -> 400", r.status_code == 400, r.status_code)

    rows = [json.loads(l) for l in advise.TELEMETRY_PATH.read_text().splitlines()]
    check("demo.telemetry consumer=demo on every call",
          len(rows) == 7 and all(r["consumer"] == "demo" for r in rows), rows)
    check("demo.telemetry unknown alias and missing context logged unmatched",
          [r["matched"] for r in rows] == [True, True, False, True, False, True, True], rows)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
