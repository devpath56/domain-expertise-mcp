"""Registry loader: seats are data, not code.

Reads seats.json and imports each seat's machine module. The server builds
one MCP tool per seat from this registry. To add a seat: add a row to
seats.json and a seats/<id>/ directory with SKILL.md and machine.py.
"""
import importlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_registry(path: Path | None = None) -> dict:
    path = path or (ROOT / "seats.json")
    with open(path) as f:
        data = json.load(f)
    seats = []
    for row in data["seats"]:
        machine = importlib.import_module(row["machine"])
        seats.append({**row, "machine_module": machine})
    return {"version": data.get("version", "0.0.0"), "seats": seats}


def get_seat(registry: dict, seat_id: str) -> dict:
    for s in registry["seats"]:
        if s["id"] == seat_id:
            return s
    raise KeyError(f"unknown seat: {seat_id}")
