"""Phoenix telemetry redproofs (phoenix-repair-20261007, test.rp-p1..rp-p5).

Telemetry is explicit opt-in, silent when off, and honest about
reachability when on. The disabled-path proofs run in a child interpreter
with every PHOENIX_* variable removed, so nothing an earlier test imported
or configured can make them pass.

Run: .venv/bin/python tests/test_phoenix_trace.py
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import calibration, phoenix_trace, trace  # noqa: E402

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("ok  " if cond else "FAIL") + f" {name}" + (f" — {detail}" if detail and not cond else ""))


def isolate_tmp():
    tmp = Path(tempfile.mkdtemp())
    trace.TRACE_PATH = tmp / "trace.jsonl"
    calibration.CAL_DIR = tmp / "calibration"
    return tmp


def clean_env():
    return {k: v for k, v in os.environ.items() if not k.startswith("PHOENIX_")}


def run_child(body: str) -> dict:
    """Run body in a fresh interpreter with no PHOENIX_* env; it prints JSON last."""
    prelude = textwrap.dedent(f"""
        import json, logging, socket, sys, tempfile, threading, time
        from pathlib import Path
        sys.path.insert(0, {str(ROOT)!r})

        # Warm the tokenizer first: its one-time download is not telemetry.
        import tiktoken
        tiktoken.get_encoding("cl100k_base")

        CONNECTS = []
        _orig_connect = socket.socket.connect
        _orig_connect_ex = socket.socket.connect_ex
        def _connect(self, addr): CONNECTS.append(repr(addr)); return _orig_connect(self, addr)
        def _connect_ex(self, addr): CONNECTS.append(repr(addr)); return _orig_connect_ex(self, addr)
        socket.socket.connect = _connect
        socket.socket.connect_ex = _connect_ex

        LOGS = []
        class _Grab(logging.Handler):
            def emit(self, rec): LOGS.append(f"{{rec.name}}: {{rec.getMessage()}}")
        logging.getLogger().addHandler(_Grab())
        logging.getLogger().setLevel(logging.DEBUG)
        THREADS_BEFORE = {{t.name for t in threading.enumerate()}}
    """)
    proc = subprocess.run(
        [sys.executable, "-c", prelude + textwrap.dedent(body)],
        env=clean_env(), capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        return {"error": proc.stderr[-2000:]}
    return json.loads(proc.stdout.strip().splitlines()[-1])


TELEMETRY_WORDS = ("phoenix", "6006", "refused", "opentelemetry", "otlp", "exporter")


def telemetry_lines(logs):
    return [l for l in logs if any(w in l.lower() for w in TELEMETRY_WORDS)]


class _Collector:
    """A local OTLP/HTTP endpoint that records every POST."""

    def __init__(self):
        posts = self.posts = []

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                posts.append((self.path, len(self.rfile.read(n))))
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/v1/traces"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_rp_p1_and_p3_disabled_default():
    """RP-P1: no env -> no exporter, no socket, not live.
    RP-P3: 10 judgments with telemetry off -> 0 connects, 0 telemetry log lines."""
    out = run_child("""
        import server
        from core import calibration, phoenix_trace, trace
        tmp = Path(tempfile.mkdtemp())
        trace.TRACE_PATH = tmp / "trace.jsonl"
        calibration.CAL_DIR = tmp / "calibration"
        after_import = list(CONNECTS)
        seat = next(s for s in server.REG["seats"] if s["id"] == "crash-triage")
        for i in range(10):
            phoenix_trace.traced_judge(seat, {"signature": f"NPE #{i}"}, server._judge)
        # Drain whatever provider exists (never importing OTel to do it), so a
        # queued export happens now rather than after the batch interval.
        otel = sys.modules.get("opentelemetry.trace")
        provider = getattr(phoenix_trace, "_provider", None) or (otel and otel.get_tracer_provider())
        if hasattr(provider, "force_flush"):
            provider.force_flush(5000)
        time.sleep(0.5)  # room for any background exporter to try
        print(json.dumps({
            "connects_after_import": after_import,
            "connects": CONNECTS,
            "logs": LOGS,
            "new_threads": sorted({t.name for t in threading.enumerate()} - THREADS_BEFORE),
            "exporter_module_loaded": any("opentelemetry.exporter" in m for m in sys.modules),
            "provider": getattr(phoenix_trace, "_provider", None) is not None,
            "live": phoenix_trace._phoenix_live,
            "diag": getattr(phoenix_trace, "diagnostics", lambda: {"state": "?", "exporter": "?"})(),
            "judgments": len(trace.query(type="Judgment")),
        }))
    """)
    if "error" in out:
        check("rp-p1/p3 child ran", False, out["error"])
        return
    # RP-P1
    check("test.rp-p1 no exporter constructed", not out["provider"] and not out["diag"]["exporter"])
    check("test.rp-p1 exporter module never imported", not out["exporter_module_loaded"])
    check("test.rp-p1 no socket at import", out["connects_after_import"] == [], out["connects_after_import"])
    check("test.rp-p1 _phoenix_live False", out["live"] is False)
    check("test.rp-p1 diagnostics say disabled", out["diag"]["state"] == "disabled", out["diag"])
    # RP-P3
    check("test.rp-p3 ten judgments ran", out["judgments"] >= 10, out["judgments"])
    check("test.rp-p3 zero connection attempts", out["connects"] == [], out["connects"])
    check("test.rp-p3 zero telemetry log lines", telemetry_lines(out["logs"]) == [], telemetry_lines(out["logs"]))
    check("test.rp-p3 no background threads", out["new_threads"] == [], out["new_threads"])


def test_rp_p2_explicit_enable():
    """RP-P2: opt-in -> exporter constructed AND liveness came from the probe."""
    isolate_tmp()
    col = _Collector()
    try:
        # Liveness is not construction: a failing probe against the same
        # reachable endpoint must leave no exporter and live False.
        d = phoenix_trace.configure(
            env={"PHOENIX_ENABLED": "true", "PHOENIX_OTLP_ENDPOINT": col.url},
            probe_fn=lambda ep: (False, "unreachable: forced"))
        check("test.rp-p2 failed probe -> not live", d["state"] == "unreachable"
              and not phoenix_trace.phoenix_live() and not d["exporter"], d)
        check("test.rp-p2 failed probe -> nothing posted", col.posts == [], col.posts)

        d = phoenix_trace.configure(
            env={"PHOENIX_ENABLED": "true", "PHOENIX_OTLP_ENDPOINT": col.url})
        check("test.rp-p2 exporter constructed", d["exporter"] and phoenix_trace._provider is not None, d)
        check("test.rp-p2 live via probe", d["state"] == "live" and phoenix_trace.phoenix_live(), d)
        check("test.rp-p2 probe reached the endpoint", col.posts == [("/v1/traces", 0)], col.posts)

        import server
        seat = next(s for s in server.REG["seats"] if s["id"] == "crash-triage")
        phoenix_trace.traced_judge(seat, {"signature": "NPE on retry"}, server._judge)
        phoenix_trace.flush()
        spans = [p for p in col.posts[1:] if p[1] > 0]
        check("test.rp-p2 judgment span exported", len(spans) >= 1, col.posts)

        d = phoenix_trace.configure(env={"PHOENIX_OTLP_ENDPOINT": col.url})
        check("test.rp-p2 endpoint alone opts in", d["state"] == "live", d)
        d = phoenix_trace.configure(
            env={"PHOENIX_ENABLED": "false", "PHOENIX_OTLP_ENDPOINT": col.url})
        check("test.rp-p2 PHOENIX_ENABLED=false wins", d["state"] == "disabled" and not d["exporter"], d)
    finally:
        phoenix_trace.configure(env={})
        col.close()


def test_rp_p4_honest_diagnostics():
    """RP-P4: enabled but unreachable -> diagnostics say so, not live, no exporter."""
    url = f"http://127.0.0.1:{free_port()}/v1/traces"
    d = phoenix_trace.configure(env={"PHOENIX_ENABLED": "true", "PHOENIX_OTLP_ENDPOINT": url})
    check("test.rp-p4 diagnostics report unreachable", d["state"] == "unreachable"
          and d["detail"].startswith("unreachable"), d)
    check("test.rp-p4 _phoenix_live False", phoenix_trace._phoenix_live is False)
    check("test.rp-p4 no exporter left retrying", phoenix_trace._provider is None and not d["exporter"], d)
    check("test.rp-p4 diagnostics() matches", phoenix_trace.diagnostics() == d)

    d = phoenix_trace.configure(env={"PHOENIX_ENABLED": "true"})
    check("test.rp-p4 enabled without endpoint is misconfigured, not defaulted",
          d["state"] == "misconfigured" and d["endpoint"] is None and not d["exporter"], d)
    phoenix_trace.configure(env={})


def test_rp_p5_health_independent():
    """RP-P5: /health and MCP tools/list work with telemetry disabled."""
    out = run_child("""
        from starlette.testclient import TestClient
        import server
        with TestClient(server.app) as c:
            h = c.get("/health")
            r = c.post("/mcp/", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                       headers={"Accept": "application/json, text/event-stream"})
        body = r.text
        if body.lstrip().startswith("event:") or "data:" in body:
            body = next(l[5:] for l in body.splitlines() if l.startswith("data:"))
        tools = [t["name"] for t in json.loads(body)["result"]["tools"]]
        print(json.dumps({
            "health_status": h.status_code, "health": h.json(),
            "list_status": r.status_code, "tools": tools,
            "connects": CONNECTS, "logs": LOGS,
        }))
    """)
    if "error" in out:
        check("rp-p5 child ran", False, out["error"])
        return
    check("test.rp-p5 /health 200 ok", out["health_status"] == 200 and out["health"]["status"] == "ok", out["health"])
    check("test.rp-p5 /health reports telemetry disabled",
          out["health"]["telemetry"]["state"] == "disabled", out["health"])
    check("test.rp-p5 tools/list answers", out["list_status"] == 200 and len(out["tools"]) == 15, out["tools"])  # 14 + advise
    check("test.rp-p5 health tool count matches listing", out["health"]["tools"] == len(out["tools"]))
    check("test.rp-p5 no socket activity", out["connects"] == [], out["connects"])
    check("test.rp-p5 no telemetry log lines", telemetry_lines(out["logs"]) == [], telemetry_lines(out["logs"]))


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)
