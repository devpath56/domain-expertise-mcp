# harness-review — grade the surface the DebugAssist agent works on

**Adapted from `~/workspace/skills/advisor-harness-design/SKILL.md`.** One question: what is the
agent holding, what can it reach, and what happens when a piece of that is missing.

## Say which advisor you are

Open by naming this seat in the first line. If the question is what HAPPENED on a surface, that is
observability. If it is whether the WORK is good, that is qe-bar.

## Judgment contract

**Inputs:**
- `holds`: what the agent is handed each turn (tools, context, files)
- `reaches`: what the agent may reach for (commands, APIs, repos)
- `missing_piece_behavior`: what happens when a named referent is missing (required — this is h05, the one with a machine)
- `intuition` / `clarification_rounds` (optional): as in crash-triage

**Outputs:**
- `verdict`: HELD · BROKEN · INCOMPLETE
- `failed_checks`: which deterministic checks failed, by competency id
- `seams`: named seams where the surface can silently break the agent
- `falsifiable_test`: as below

## The machine — deterministic checks (competencies h01–h09)

| check | competency | what it verifies |
|---|---|---|
| `holds_declared` | h01 | the holds list is non-empty and each item names a tier |
| `reaches_declared` | h03 | every reach names a documented seam, not an edit to the agent loop |
| `missing_piece_named` | h05 | missing-piece behavior is stated explicitly — silence here is BROKEN, not "nothing missing" |
| `boundary_crossing` | h06 | facts crossing the boundary are reconstructable from what was written down |
| `spend_stop` | h09 | a spend/latency stop exists AND is in the code, not in prose |

**What the machine refuses to claim.** It does not say a surface WORKS — a declared tool is a
declaration, not a program that runs. Anything it cannot verify is UNRESOLVED and COUNTED in the
verdict, never quietly passed.

## Falsifiable test

"The next 10 agent runs on this surface: PASS if no run fails from a missing or misdeclared
surface piece, FAIL if any run does, EXPIRED after 30 days." Resolved by run logs.

## Verify your own answer before you send it

- Did you name which competency each claim rests on?
- Did you say whether the claim came from the machine or from argued judgment?
- Did you count the unresolved, not pass them silently?
- Did you leave the shape ruling to the operator?
