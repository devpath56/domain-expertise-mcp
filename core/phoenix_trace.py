"""Phoenix token instrumentation for the Domain Expertise MCP server.

Wraps the governed `_judge` path in an OpenTelemetry span and records
emission/billed token counts on the trace record (append-only, atomic).

Design decisions (from hardened proposal + 14 review deltas):
- Phoenix is never on the critical path: BatchSpanProcessor (async),
  no-op tracer when Phoenix unreachable (D13). No try/except in hot path.
- Telemetry is explicit opt-in (phoenix-repair-20261007). With no PHOENIX_*
  env set this module constructs no exporter, starts no thread, opens no
  socket and logs nothing. Opt in with PHOENIX_ENABLED=true plus
  PHOENIX_OTLP_ENDPOINT, or by setting PHOENIX_OTLP_ENDPOINT alone; there is
  no default endpoint. PHOENIX_ENABLED=false wins over a set endpoint.
- _phoenix_live is the result of a reachability probe (an empty OTLP export
  POSTed to the endpoint), never of constructing the exporter. An
  unreachable endpoint gets no exporter, so no background retries.
- "emission tokens" (tiktoken, machine side) vs "billed tokens"
  (caller-reported, the real cost). Never one name for two instruments (D1).
- Missing billed tokens -> null, never 0. 0 = instrument bug -> alert (D9).
- billed_tokens validated here (pulled downward, D14): non-negative ints,
  sane upper bound, else rejected with error.
- Annotation is append-only (new record version, same id) and atomic:
  both fields together or neither (D10, D12).
- tiktoken version pinned in requirements.txt (tokenizer drift, W3).
"""

import json
import logging
import os

# --- tracer setup: explicit opt-in, liveness from a probe (D13) ---

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}
_PROBE_TIMEOUT_S = 1.0


class _NoOpSpan:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def set_attribute(self, *a): pass


class _NoOpTracer:
    def start_as_current_span(self, *a, **k): return _NoOpSpan()


def _settings(env) -> tuple[bool, str | None]:
    """(opted_in, endpoint) from the environment. No default endpoint."""
    flag = (env.get("PHOENIX_ENABLED") or "").strip().lower()
    endpoint = (env.get("PHOENIX_OTLP_ENDPOINT") or "").strip() or None
    if flag in _FALSE:
        return False, endpoint
    return flag in _TRUE or endpoint is not None, endpoint


def probe(endpoint: str, timeout: float = _PROBE_TIMEOUT_S) -> tuple[bool, str]:
    """POST an empty OTLP export to the endpoint. (live, detail).

    An empty body is a valid, empty ExportTraceServiceRequest, so a real
    collector answers 2xx without recording anything."""
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        endpoint, data=b"", method="POST",
        headers={"Content-Type": "application/x-protobuf"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        return False, f"rejected: HTTP {e.code}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        reason = getattr(e, "reason", e)
        return False, f"unreachable: {reason}"


def configure(env=None, probe_fn=None) -> dict:
    """Set the module tracer from env. Returns the diagnostics dict.

    Called once at import; tests call it again with their own env."""
    global _tracer, _phoenix_live, _provider, _diag
    if _provider is not None:
        _provider.shutdown()
    _tracer, _phoenix_live, _provider = _NoOpTracer(), False, None

    opted_in, endpoint = _settings(os.environ if env is None else env)
    _diag = {"enabled": opted_in, "endpoint": endpoint, "exporter": False}
    if not opted_in:
        _diag.update(state="disabled", detail="no PHOENIX_* opt-in set")
        return dict(_diag)
    if endpoint is None:
        _diag.update(state="misconfigured",
                     detail="PHOENIX_ENABLED set but PHOENIX_OTLP_ENDPOINT is not")
        _log.warning("phoenix telemetry: %s", _diag["detail"])
        return dict(_diag)

    live, detail = (probe_fn or probe)(endpoint)
    if not live:
        _diag.update(state="unreachable", detail=detail)
        _log.warning("phoenix telemetry off: %s %s", endpoint, detail)
        return dict(_diag)

    try:
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
    except ImportError as e:
        _diag.update(state="unavailable", detail=f"exporter not installed: {e}")
        _log.warning("phoenix telemetry off: %s", _diag["detail"])
        return dict(_diag)

    # A local provider, not the OTel global: reconfiguring stays possible.
    _provider = TracerProvider()
    _provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
    )
    _tracer = _provider.get_tracer("domain-expertise-mcp")
    _phoenix_live = True
    _diag.update(state="live", detail=detail, exporter=True)
    return dict(_diag)


_log = logging.getLogger("phoenix_trace")
_tracer, _phoenix_live, _provider, _diag = _NoOpTracer(), False, None, {}
configure()

# --- token counting ---

_ENCODING = None

def _encoding():
    global _ENCODING
    if _ENCODING is None:
        import tiktoken
        _ENCODING = tiktoken.get_encoding("cl100k_base")
    return _ENCODING


def count_emission_tokens(inputs: dict, result: dict) -> dict:
    """Emission tokens: tiktoken count of what the machine seat consumed
    and emitted. Deterministic, exact. Not billed cost."""
    enc = _encoding()
    prompt_text = json.dumps(inputs, sort_keys=True, default=str)
    completion_text = json.dumps(
        {
            "verdict": result.get("verdict"),
            "checks": result.get("checks"),
            "test": result.get("falsifiable_test", {}).get("statement", ""),
        },
        sort_keys=True,
        default=str,
    )
    prompt = len(enc.encode(prompt_text))
    completion = len(enc.encode(completion_text))
    # 0 is an impossible state for a real judgment -> bug, not data (D9)
    if prompt == 0 or completion == 0:
        raise RuntimeError(
            f"token instrument bug: 0-token count (prompt={prompt}, "
            f"completion={completion})"
        )
    return {"prompt": prompt, "completion": completion}


_BILLED_MAX = 10_000_000  # sane upper bound; above this is a bug, not a bill


def validate_billed_tokens(value) -> int | None:
    """Validate caller-reported billed tokens (D4, D14).
    Returns the int, or None when absent. Raises on invalid."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"billed_tokens must be an int or null, got {type(value).__name__}"
        )
    if value < 0:
        raise ValueError(f"billed_tokens must be >= 0, got {value}")
    if value > _BILLED_MAX:
        raise ValueError(
            f"billed_tokens {value} exceeds sane bound {_BILLED_MAX}"
        )
    return value


# --- the wrapped judge path ---

def traced_judge(seat: dict, inputs: dict, _judge_fn) -> dict:
    """Drop-in replacement for _judge with token instrumentation.

    _judge_fn is injected (the real _judge from server.py) to keep this
    module importable without the server.
    """
    # Validate billed tokens BEFORE the judgment runs: fail fast on bad input.
    billed = validate_billed_tokens(inputs.get("billed_tokens"))

    with _tracer.start_as_current_span(f"seat.{seat['id']}") as span:
        span.set_attribute("seat.id", seat["id"])
        result = _judge_fn(seat, inputs)
        emission = count_emission_tokens(inputs, result)
        span.set_attribute("llm.token_count.prompt", emission["prompt"])
        span.set_attribute("llm.token_count.completion", emission["completion"])
        if billed is not None:
            span.set_attribute("llm.token_count.billed", billed)

    # Append-only atomic annotation (D10, D12): both fields or neither,
    # as a new record version with the same judgment id.
    _annotate_tokens(result["judgment_id"], emission, billed)
    # Surface token counts on the result for the caller's convenience.
    result["tokens"] = {
        "emission": emission,
        "billed": billed,
        "schema_version": 1,
    }
    return result


def _annotate_tokens(judgment_id: str, emission: dict, billed: int | None):
    """Write token counts as a new trace record version (append-only)."""
    # Imported lazily to keep this module decoupled from the trace store.
    from core.trace import query, _write, _now

    records = query(type="Judgment", id=judgment_id)
    if not records:
        raise RuntimeError(f"cannot annotate tokens: unknown {judgment_id}")
    latest = records[-1]
    updated = dict(latest)
    # Atomic: both fields set together in the single new record.
    updated["tokens"] = {
        "emission": emission,
        "billed": billed,  # null = not reported, never 0 (D9)
        "schema_version": 1,
    }
    _write({**updated, "ts": _now()})


def phoenix_live() -> bool:
    """Whether the configured endpoint answered the probe at configure time."""
    return _phoenix_live


def diagnostics() -> dict:
    """Telemetry state: disabled | misconfigured | unreachable | unavailable | live."""
    return dict(_diag)


def flush(timeout_ms: int = 5000) -> bool:
    """Export pending spans now (tests, shutdown). No-op when not live."""
    return _provider.force_flush(timeout_ms) if _provider is not None else True
