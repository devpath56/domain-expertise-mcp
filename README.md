# Domain Expertise MCP Server

A registry-driven MCP server that serves expert advisor seats to **DebugAssist**
([github.com/ishamishra0408/DebugAssist](https://github.com/ishamishra0408/DebugAssist)).

DebugAssist takes a GitHub bug and returns the fix, why it shipped, and a lasting guard.
Its pipeline stalls at four points that need human judgment — instead of waiting for a
person, DebugAssist queries these seats through MCP:

| Stall | Seat | What it answers |
|---|---|---|
| `NOT A DEFECT` / `NEEDS PERSON` | `defect_triage` | Is this a real defect? Four-state rule: DEFECT / NOT_A_DEFECT / NEEDS_PERSON / DECLINED |
| `CAUSE NOT FOUND` | `cause_locator` | Which file and line range holds the cause? Max 3 ranked candidates |
| `FIX NOT VALIDATED` / `TEST FLAWED` | `qe_ic_debugassist` | Is the fix validated? Is the test sound? No-vibes rule enforced |
| `GUARD NOT WRITTEN` | `allspaw_debugassist` | What conditions allowed it? Guardable conditions with falsifiable tests |

## Registry-driven, never hardcoded

`seats.json` is the roster. The server builds one MCP tool per seat at startup.
To add, remove, or reorder seats: edit `seats.json` plus `seats/<id>/SKILL.md` and
`seats/<id>/machine.py`. No code changes, no redeploy of the server itself.

## Quickstart

```bash
pip install -r requirements.txt
python server.py
# MCP endpoint: http://127.0.0.1:8901/mcp/
```

Claude Code / Claude Desktop MCP config:

```json
{
  "mcpServers": {
    "domain-expertise": {
      "url": "http://127.0.0.1:8901/mcp/"
    }
  }
}
```

## How a query works

1. Caller sends inputs to a seat's tool (e.g. `defect_triage` with issue + repo).
2. The seat's deterministic `machine.py` runs structural checks (required fields,
   decision rules, contract shape) — no LLM, no vibes.
3. The server emits a trace (`Judgment`, `MachineRun`, `Test` records).
4. Every judgment ships a **falsifiable test** per the RPD calibration loop:
   the test resolves PASS/FAIL when the real outcome lands, feeding per-seat calibration.
5. The calling model does the generative judgment guided by the seat's `SKILL.md`.

## Tests

```bash
python tests/test_server.py        # 56 checks: registry, contracts, trace, tools
python tests/test_spec_acceptance.py  # 23 checks derived from the trace spec
```

## Deploy on Render

`render.yaml` is a Blueprint: Render → New → Blueprint → pick this repo.
Render prompts for `MCP_API_KEY` (never committed). Every `/mcp` request then
needs `Authorization: Bearer <MCP_API_KEY>` or gets 401; `/health` stays open
and does not depend on Phoenix. On Render the server refuses to start without
the key. Locally, leaving `MCP_API_KEY` unset serves `/mcp` without auth.

## Spec

Behavior is governed by `../specs/trace-infrastructure-rpd-calibration-spec.md`:
22 metrics across conversational / learning / governance, the RPD preregister-then-score
loop, and the test state machine (`delivered → pending → resolved_pass|resolved_fail|expired`).
