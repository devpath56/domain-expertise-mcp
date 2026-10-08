"""advise on-ramp: one tool instead of learning every seat's schema.

Resolves an alias to a seat id and builds the seat's structured inputs from
config/question_templates.yaml by mechanical substitution of {question},
{evidence} and {context.<key>}. No interpretation logic and no per-seat code
live here: the yaml is the whole mapping. A value that is exactly
"{context.<key>}" is replaced by the context value as-is (a list stays a
list); "{context.<key>?}" marks the key optional and drops it when the caller
gave none. A required context key that is missing or empty raises, naming it.
Every call appends one line to telemetry/advise_calls.jsonl (ts, seat_alias,
matched, consumer).
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_PATH = ROOT / "config" / "question_templates.yaml"
TELEMETRY_PATH = ROOT / "telemetry" / "advise_calls.jsonl"

# One pass over the template string, so text substituted in is never re-read
# as a placeholder (evidence that contains "{context.x}" stays literal).
_PLACEHOLDER = re.compile(r"\{(question|evidence|context\.([A-Za-z0-9_]+)(\?)?)\}")
_DROP = object()


def load_templates(path: Path | None = None) -> dict:
    with open(path or TEMPLATES_PATH) as f:
        return yaml.safe_load(f)


def _empty(val) -> bool:
    return val is None or (isinstance(val, (str, list, dict)) and not val)


def _substitute(node, values: dict, context: dict, missing: list):
    if isinstance(node, str):
        whole = _PLACEHOLDER.fullmatch(node)
        if whole and whole.group(2):
            key, optional = whole.group(2), bool(whole.group(3))
            val = context.get(key)
            if _empty(val):
                if optional:
                    return _DROP
                missing.append(f"context.{key}")
                return node
            return val

        def one(m):
            if not m.group(2):
                return values[m.group(1)]
            val = context.get(m.group(2))
            if _empty(val):
                if not m.group(3):
                    missing.append(f"context.{m.group(2)}")
                return ""
            return str(val)
        return _PLACEHOLDER.sub(one, node)
    if isinstance(node, dict):
        out = {k: _substitute(v, values, context, missing) for k, v in node.items()}
        return {k: v for k, v in out.items() if v is not _DROP}
    if isinstance(node, list):
        out = [_substitute(v, values, context, missing) for v in node]
        return [v for v in out if v is not _DROP]
    return node


def build(seat_alias: str, question: str, evidence: str, config: dict | None = None,
          context: dict | None = None) -> tuple[str, dict]:
    """(seat_id, inputs) for an alias, or ValueError naming the known aliases
    or the context keys the alias's template needs and did not get."""
    config = config or load_templates()
    aliases = config.get("aliases", {})
    if seat_alias not in aliases:
        raise ValueError(
            f"unknown seat_alias '{seat_alias}'; known aliases: {sorted(aliases)}")
    template = next((t for t in config.get("templates", []) if t["alias"] == seat_alias), None)
    if template is None:
        raise ValueError(f"seat_alias '{seat_alias}' resolves to '{aliases[seat_alias]}' "
                         f"but has no template in {TEMPLATES_PATH.name}")
    if context is not None and not isinstance(context, dict):
        raise ValueError(f"context must be an object, got {type(context).__name__}")
    missing: list = []
    inputs = _substitute(template["build_inputs"], {"question": question, "evidence": evidence},
                         context or {}, missing)
    if missing:
        need = sorted(set(missing))
        raise ValueError(f"seat_alias '{seat_alias}' needs {need} and the call did not supply it; "
                         f"pass context={{{', '.join(repr(m.split('.', 1)[1]) + ': ...' for m in need)}}}")
    return aliases[seat_alias], inputs


def describe(config: dict | None = None) -> str:
    """The aliases and the context keys each template reads, from the yaml,
    for the tool description: e.g. 'x (context: repo_name)'."""
    config = config or load_templates()
    parts = []
    for t in config.get("templates", []):
        keys = []
        for m in _PLACEHOLDER.finditer(json.dumps(t["build_inputs"])):
            if m.group(2):
                keys.append(m.group(2) + (" optional" if m.group(3) else ""))
        parts.append(t["alias"] + (f" (context: {', '.join(keys)})" if keys else ""))
    return "; ".join(parts)


def log_call(seat_alias: str, matched: bool, consumer: str) -> None:
    TELEMETRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": datetime.now(timezone.utc).isoformat(), "seat_alias": seat_alias,
           "matched": matched, "consumer": consumer}
    with open(TELEMETRY_PATH, "a") as f:
        f.write(json.dumps(row) + "\n")
