# PinchBench

Frozen comparisons using each agent's configured native loop and tools, with
unchanged PinchBench task rubrics and documented environment/capture repairs.

| Run | Coverage | Results |
| --- | --- | --- |
| 2026-10-15 | 32 tasks · 11 domains · 288 attempts | [Report](runs/2026-10-15/report/index.html) · [Methods and findings](runs/2026-10-15/README.md) · [Metrics](runs/2026-10-15/metrics.json) |

The selection has three tasks in each of ten domains and two in Memory, which
has two eligible frozen tasks. Every task has three attempts per agent.
Overall scores weight domains equally. Native session reuse and persistence
controls passed before scoring; the final cross-agent fairness review passed
after repairing demonstrated capture/grading defects.

See the dated run for exact versions, task IDs, presets, limitations and the
reproduction prompt. This local subset is not an official leaderboard result.
The original [frozen plan](frozen-plan.json) remains available as provenance.
