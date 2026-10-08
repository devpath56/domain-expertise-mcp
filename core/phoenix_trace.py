"""Phoenix token instrumentation for the Domain Expertise MCP server.

Wraps the governed `_judge` path in an OpenTelemetry span and records
emission/billed token counts on the trace record (append-only, atomic).

Design decisions (from hardened proposal + 14 review deltas):
- Phoenix is never on the critical path: BatchSpanProcessor (async),
  no-op tracer when Phoenix unreachable (D13). No try/except in hot path.
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
import os

# --- tracer setup: degrades to no-op when Phoenix unreachable (D13) ---

def _init_tracer():
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        endpoint = os.environ.get(
            "PHOENIX_OTLP_ENDPOINT", "http://localhost:6006/v1/traces"
        )
        provider = TracerProvider()
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
        )
        trace.set_tracer_provider(provider)
        return trace.get_tracer("domain-expertise-mcp"), True
    except Exception:
        # Phoenix down / not installed: no-op tracer.
        # "Phoenix down" is a configuration state, not a hot-path exception.
        class _NoOpSpan:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def set_attribute(self, *a): pass

        class _NoOpTracer:
            def start_as_current_span(self, *a, **k): return _NoOpSpan()

        return _NoOpTracer(), False


_tracer, _phoenix_live = _init_tracer()

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
    """Whether spans are reaching a live Phoenix (for diagnostics)."""
    return _phoenix_live
