"""Domain Expertise MCP server — stateless Streamable HTTP.

One MCP tool per seat, generated from seats.json (registry-driven, never
hardcoded). Every judgment runs the seat's deterministic machine and emits
trace records (Judgment, Turn, MachineRun, GroundingLink, Intuition, Test)
before returning — no judgment without a trace record.

Server-level tools:
  resolve_test    — resolve a falsifiable test (resolved_pass | resolved_fail | expired)
  get_calibration — per-seat calibration summary (counts; rates only at n>=30)
  query_trace     — filter the trace log

Run:
  .venv/bin/python server.py            # serves on http://127.0.0.1:8901/mcp
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.tools import Tool
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from core import calibration, registry, trace

mcp = FastMCP("domain-expertise", stateless_http=True)
REG = registry.load_registry()


def _judge(seat: dict, inputs: dict) -> dict:
    """The governed judge path. Machine runs, trace is emitted, then the result."""
    machine = seat["machine_module"]
    result = machine.check(inputs)

    judgment = trace.emit_judgment(seat["id"], inputs)
    jid = judgment["id"]
    trace.emit_turn(jid, "caller", f"query: {str(inputs)[:500]}")
    trace.emit_machine_run(jid, seat["machine"], result["verdict"], result["checks"])
    trace.emit_grounding_link(jid, "seat-contract", seat["skill"])
    trace.emit_turn(
        jid, "seat",
        f"verdict={result['verdict']} via {seat['id']} machine "
        f"({len(result['checks'])} deterministic checks)",
    )

    intuition_id = None
    if inputs.get("intuition"):
        intu = trace.emit_intuition(jid, inputs["intuition"])
        intuition_id = intu["id"]

    test_spec = machine.make_test(inputs, result)
    test = trace.emit_test(jid, seat["id"], test_spec["statement"], test_spec["resolve_rule"])

    return {
        "seat": seat["id"],
        "judgment_id": jid,
        "verdict": result["verdict"],
        "machine_result": result,
        "intuition_id": intuition_id,
        "falsifiable_test": {
            "test_id": test["id"],
            "statement": test_spec["statement"],
            "resolve_rule": test_spec["resolve_rule"],
            "state": "delivered",
        },
        "contract": seat["skill"],
        "calibration": calibration.summary(seat["id"]),
    }


# --- one tool per seat, from the registry ---
import inspect as _inspect

from pydantic import BaseModel, ConfigDict


class JudgmentResult(BaseModel):
    """Permissive envelope: lets this mcp version build an output schema so
    results are delivered as structuredContent (not just text)."""
    model_config = ConfigDict(extra="allow")


_TYPEMAP = {"string": str, "integer": int, "number": float,
            "boolean": bool, "array": list, "object": dict}

for _seat in REG["seats"]:
    def _make_fn(seat):
        schema = seat["machine_module"].INPUT_SCHEMA
        props = schema["properties"]
        required = set(schema.get("required", []))

        async def judge(**kwargs) -> JudgmentResult:
            # Token instrumentation wraps the governed path (row 43).
            # Phoenix is never on the critical path: opt-in, no-op otherwise.
            from core.phoenix_trace import traced_judge
            return traced_judge(seat, kwargs, _judge)

        # A real signature so from_function derives the argument model AND
        # the JSON schema from it (required first, then optionals).
        # billed_tokens is injected for every seat (row 43): optional,
        # caller-reported LLM tokens; null = not reported, never 0.
        _props = dict(props)
        _props.setdefault("billed_tokens", {
            "type": "integer",
            "description": "Caller-reported LLM tokens for this judgment (billed cost). Optional; omit when unknown.",
        })
        params = []
        for name in sorted(_props, key=lambda n: (n not in required, n)):
            ann = _TYPEMAP.get(_props[name].get("type"), str)
            default = _inspect.Parameter.empty if name in required else None
            params.append(_inspect.Parameter(
                name, _inspect.Parameter.KEYWORD_ONLY,
                default=default, annotation=ann))
        judge.__signature__ = _inspect.Signature(params, return_annotation=JudgmentResult)
        judge.__name__ = f"judge_{seat['id'].replace('-', '_')}"
        return judge

    _fn = _make_fn(_seat)
    _tool = Tool.from_function(
        _fn,
        name=_seat["id"].replace("-", "_"),
        description=(
            f"[{_seat['name']}] {_seat['description']} "
            f"Runs {_seat['machine']} deterministically, emits trace records, "
            f"and returns a falsifiable test. Contract: {_seat['skill']}"
        ),
    )
    # Pre-built Tool registered straight into the manager; pinned to mcp<2
    # where this holds.
    mcp._tool_manager._tools[_tool.name] = _tool


# --- server-level tools ---
async def resolve_test(test_id: str, outcome: str, evidence: str = "") -> JudgmentResult:
    """Resolve a falsifiable test. outcome: resolved_pass | resolved_fail | expired."""
    if outcome not in ("resolved_pass", "resolved_fail", "expired"):
        raise ValueError("outcome must be resolved_pass | resolved_fail | expired")
    tests = trace.query(type="Test", id=test_id)
    if not tests:
        raise ValueError(f"unknown test_id: {test_id}")
    latest = tests[-1]
    # append-only: new record carrying the same id forward to its new state
    updated = dict(latest)
    updated["state"] = "pending" if False else outcome  # delivered -> resolved directly
    updated["evidence"] = evidence[:500]
    from core.trace import _write, _now
    _write({**updated, "ts": _now()})
    entry = calibration.record(
        latest["seat"], latest["judgment_id"], test_id, outcome, evidence=evidence
    )
    return {"test_id": test_id, "state": outcome, "calibration": calibration.summary(latest["seat"])}


async def get_calibration(seat_id: str = "") -> JudgmentResult:
    """Calibration summary per seat (counts; rates only at n>=30). Empty seat_id = all seats."""
    ids = [seat_id] if seat_id else [s["id"] for s in REG["seats"]]
    unknown = [i for i in ids if i not in {s["id"] for s in REG["seats"]}]
    if unknown:
        raise ValueError(f"unknown seat(s): {unknown}")
    return calibration.summary_all(ids)


async def query_trace(record_type: str = "", seat: str = "", judgment_id: str = "") -> JudgmentResult:
    """Filter the trace log. All filters optional, exact match."""
    filters = {}
    if record_type:
        filters["type"] = record_type
    if seat:
        filters["seat"] = seat
    if judgment_id:
        filters["judgment_id"] = judgment_id
    records = trace.query(**filters)
    return {"count": len(records), "records": records[-50:]}


def _server_tool(fn, name, description):
    # Real named-parameter signatures: from_function derives model + schema.
    t = Tool.from_function(fn, name=name, description=description)
    mcp._tool_manager._tools[t.name] = t
    return t


_server_tool(resolve_test, "resolve_test",
             "Resolve a falsifiable test emitted by a judgment. "
             "outcome: resolved_pass | resolved_fail | expired. Updates calibration.")


async def report_outcome(test_id: str = "", judgment_id: str = "",
                         outcome: str = "", evidence: str = "") -> JudgmentResult:
    """Report a real-world outcome for a pending test.

    The outcome observer's entry point. Call when the real outcome lands:
    a fix diff is validated, an investigation concludes, a guard's 30-day
    window closes, an independent review confirms. The tool matches the
    outcome to the pending test and resolves it, updating calibration.

    outcome: pass | fail | expired (mapped to resolved_pass | resolved_fail | expired).
    Identify the test by test_id, or by judgment_id (resolves its pending test).
    """
    outcome_map = {"pass": "resolved_pass", "fail": "resolved_fail", "expired": "expired",
                   "resolved_pass": "resolved_pass", "resolved_fail": "resolved_fail"}
    if outcome not in outcome_map:
        raise ValueError("outcome must be pass | fail | expired")
    resolved = outcome_map[outcome]

    tid = test_id
    if not tid and judgment_id:
        # Find the pending test for this judgment
        tests = trace.query(type="Test", judgment_id=judgment_id)
        pending = [t for t in tests if t.get("state") in ("delivered", "pending", None)]
        if not pending:
            raise ValueError(f"no pending test for judgment_id: {judgment_id}")
        tid = pending[-1]["id"] if "id" in pending[-1] else pending[-1].get("test_id", "")
    if not tid:
        raise ValueError("supply test_id or judgment_id")
    return await resolve_test(tid, resolved, evidence)


_server_tool(report_outcome, "report_outcome",
             "Report a real-world outcome to resolve a pending test. "
             "outcome: pass | fail | expired. Identify by test_id or judgment_id. "
             "This is how test completion rate and calibration get real data.")
async def advise(seat_alias: str, question: str, evidence: str,
                 consumer: str = "unspecified") -> JudgmentResult:
    """On-ramp: one question + evidence, routed to a seat by alias.

    Inputs are built from config/question_templates.yaml by mechanical
    substitution, then the seat's governed judge path runs and its
    JudgmentResult is returned unchanged. The structured seat tools stay
    the ceiling; this is the floor.
    """
    from core import advise as _advise
    try:
        seat_id, inputs = _advise.build(seat_alias, question, evidence)
    except ValueError:
        _advise.log_call(seat_alias, False, consumer)
        raise
    _advise.log_call(seat_alias, True, consumer)
    from core.phoenix_trace import traced_judge
    return traced_judge(registry.get_seat(REG, seat_id), inputs, _judge)


_server_tool(advise, "advise",
             "On-ramp to the seats: give seat_alias (e.g. allspaw, qe-ic-advisor), "
             "your question and your evidence; the server builds the seat's "
             "structured inputs from a declarative template and returns its "
             "JudgmentResult unchanged. Unknown alias errors with the known list. "
             "consumer: your caller id (\"test\" for test calls).")
_server_tool(get_calibration, "get_calibration",
             "Per-seat calibration summary: counts, pass rate (n>=30 only), "
             "avg clarification rounds. Empty seat_id returns all seats.")
_server_tool(query_trace, "query_trace",
             "Filter the trace log (Judgment, Turn, MachineRun, GroundingLink, "
             "Intuition, Test). All filters optional.")


async def health(request):
    """Liveness of the MCP server itself. Telemetry is reported, never required."""
    import hashlib
    import os
    from core import phoenix_trace
    key = os.environ.get("MCP_API_KEY", "").strip()
    return JSONResponse({
        "status": "ok",
        "commit": os.environ.get("RENDER_GIT_COMMIT", "")[:7] or None,
        # Fingerprint only (never the key): lets a caller check it holds the
        # same key the server does without the server ever echoing it.
        "auth": {"required": bool(key), "key_len": len(key),
                 "key_sha256_8": hashlib.sha256(key.encode()).hexdigest()[:8] if key else None},
        "tools": len(mcp._tool_manager.list_tools()),
        "telemetry": phoenix_trace.diagnostics(),
    })


def _authorized(scope, api_key: str) -> bool:
    """Bearer check for /mcp. Constant-time compare; header names are lowercase in ASGI."""
    import hmac
    for name, value in scope.get("headers", []):
        if name == b"authorization":
            scheme, _, token = value.decode("latin-1").partition(" ")
            return scheme.lower() == "bearer" and hmac.compare_digest(token.strip(), api_key)
    return False


def build_app(api_key: str | None = None):
    """api_key: when set, every /mcp request needs `Authorization: Bearer <key>`
    or gets 401. /health stays open. Read from MCP_API_KEY when not passed."""
    import os
    if api_key is None:
        api_key = os.environ.get("MCP_API_KEY", "")
    # Dashboard text areas can store a trailing newline; compare the bare key.
    api_key = api_key.strip()
    session_manager = StreamableHTTPSessionManager(app=mcp._mcp_server, stateless=True)

    async def handle_streamable_http(scope, receive, send):
        if api_key and not _authorized(scope, api_key):
            response = JSONResponse(
                {"error": "unauthorized", "detail": "Authorization: Bearer <MCP_API_KEY> required"},
                status_code=401, headers={"WWW-Authenticate": "Bearer"})
            await response(scope, receive, send)
            return
        await session_manager.handle_request(scope, receive, send)

    return Starlette(
        routes=[Route("/health", health), Mount("/mcp", app=handle_streamable_http)],
        lifespan=lambda app: session_manager.run(),
    )


app = build_app()

if __name__ == "__main__":
    import uvicorn
    import os
    if os.environ.get("RENDER") and not os.environ.get("MCP_API_KEY"):
        # Fail closed on a public host: never serve /mcp unauthenticated.
        sys.exit("MCP_API_KEY is not set; refusing to serve /mcp publicly without auth")
    port = int(os.environ.get("PORT", 8901))
    uvicorn.run(app, host="0.0.0.0", port=port)
