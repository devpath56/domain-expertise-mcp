"""advise on-ramp: one tool instead of learning every seat's schema.

Resolves an alias to a seat id and builds the seat's structured inputs from
config/question_templates.yaml by mechanical substitution of {question} and
{evidence}. No interpretation logic and no per-seat code live here: the yaml
is the whole mapping. Every call appends one line to
telemetry/advise_calls.jsonl (ts, seat_alias, matched, consumer).
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_PATH = ROOT / "config" / "question_templates.yaml"
TELEMETRY_PATH = ROOT / "telemetry" / "advise_calls.jsonl"


def load_templates(path: Path | None = None) -> dict:
    with open(path or TEMPLATES_PATH) as f:
        return yaml.safe_load(f)


def _substitute(node, values: dict):
    if isinstance(node, str):
        for key, val in values.items():
            node = node.replace("{" + key + "}", val)
        return node
    if isinstance(node, dict):
        return {k: _substitute(v, values) for k, v in node.items()}
    if isinstance(node, list):
        return [_substitute(v, values) for v in node]
    return node


def build(seat_alias: str, question: str, evidence: str, config: dict | None = None) -> tuple[str, dict]:
    """(seat_id, inputs) for an alias, or ValueError naming the known aliases."""
    config = config or load_templates()
    aliases = config.get("aliases", {})
    if seat_alias not in aliases:
        raise ValueError(
            f"unknown seat_alias '{seat_alias}'; known aliases: {sorted(aliases)}")
    template = next((t for t in config.get("templates", []) if t["alias"] == seat_alias), None)
    if template is None:
        raise ValueError(f"seat_alias '{seat_alias}' resolves to '{aliases[seat_alias]}' "
                         f"but has no template in {TEMPLATES_PATH.name}")
    inputs = _substitute(template["build_inputs"], {"question": question, "evidence": evidence})
    return aliases[seat_alias], inputs


def log_call(seat_alias: str, matched: bool, consumer: str) -> None:
    TELEMETRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": datetime.now(timezone.utc).isoformat(), "seat_alias": seat_alias,
           "matched": matched, "consumer": consumer}
    with open(TELEMETRY_PATH, "a") as f:
        f.write(json.dumps(row) + "\n")
