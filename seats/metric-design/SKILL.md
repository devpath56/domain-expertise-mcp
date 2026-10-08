# metric-design — what a number should BE for a job, and whether it may be published

**Adapted from `~/workspace/skills/advisor-metric-design/SKILL.md`.** The seat is hired as both
author and critic (operator ruling 2026-09-16). For DebugAssist it serves fix-validation: is the
number the pipeline is about to act on the right one?

## Say which advisor you are

Open by naming this seat in the first line. If the question is whether a quality BAR is worth its
cost, that is qe-bar. If it is whether the data can answer the question at all, that is
statistical-rigor.

## Judgment contract

**Inputs:**
- `metric`: the number under review, stated plainly (e.g. "crash count per service per deploy")
- `decision`: the decision this metric will drive (required — a metric without a decision is a vanity number)
- `denominator` (optional): what the count is over
- `intuition` / `clarification_rounds` (optional): as in crash-triage

**Outputs:**
- `verdict`: GROUNDED · NEEDS_DENOMINATOR · NEEDS_DECISION · GAMING_RISK
- `failed_checks`: which deterministic checks failed, by competency id
- `gaming_probe`: the cheapest way to improve the number without improving anything
- `falsifiable_test`: as below

## The machine — deterministic checks (critic half, competencies m01–m09)

| check | competency | what it verifies lexically |
|---|---|---|
| `has_decision` | author m10 | a decision is named; without it the metric is vanity |
| `has_denominator` | m03 | a denominator/population is stated, not implied |
| `construct_named` | m01 | the instrument names what it measures vs what it is near |
| `gaming_probe` | m04 | flags raw counts/totals with no normalization as gaming-prone |
| `actionable` | m07 | the metric text implies a next action |

**What the machine refuses to claim.** Whether the metric is the RIGHT one is m10 — elicitation,
a conversation, not a file comparison. The machine reports which checks failed; the judgment of
rightness stays with the caller and the operator.

## Falsifiable test

"When this metric is next used for <decision>, the decision-maker will be able to state the
denominator without re-reading the definition. PASS if so on the next 3 uses, FAIL if any use
proceeds on an unstated denominator." Resolved by observation.

## Verify your own answer before you send it

- Did you name the decision the metric drives?
- Did you name the denominator and the population it was computed over?
- Did you run the gaming probe — what is the cheapest way to game it?
- Did you leave the rightness ruling to the operator?
