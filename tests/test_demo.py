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
        check("landing.config is http to /mcp with a key placeholder",
              '"type": "http"' in page and "/mcp" in page and "Bearer ${MCP_API_KEY}" in page)
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

        r = c.post("/demo/advise", json={"seat_alias": "allspaw"})
        check("demo.advise missing fields -> 400", r.status_code == 400, r.status_code)
        r = c.post("/demo/advise", content=b"not json", headers={"Content-Type": "application/json"})
        check("demo.advise non-JSON -> 400", r.status_code == 400, r.status_code)

    rows = [json.loads(l) for l in advise.TELEMETRY_PATH.read_text().splitlines()]
    check("demo.telemetry consumer=demo on every call",
          len(rows) == 3 and all(r["consumer"] == "demo" for r in rows), rows)
    check("demo.telemetry unknown alias logged unmatched", [r["matched"] for r in rows] == [True, True, False], rows)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
