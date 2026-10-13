# Leon benchmarks

📊 A collection of Personal AI benchmarks with detailed results, sanitized traces,
and reproduction guides to help grow the space together.

The goal is to understand how personal AI agents perform in practice, share
evidence that others can inspect, and identify concrete ways to improve them.
Comparisons include Leon, Hermes and OpenClaw, with room for more agents and
benchmarks over time.

[Browse the interactive reports](https://benchmarks.getleon.ai/).

## What you'll find

- **Results:** overall, domain and task scores, with repeated attempts and
  uncertainty where available.
- **Performance metrics:** task completion time, response latency, token usage,
  inference counts, tool-call checks and measurement coverage.
- **Evidence:** sanitized action traces, grading records, failure analysis and
  practical improvement priorities for each agent.
- **Reproduction guides:** frozen tasks, exact agent versions, model settings,
  environment conditions, documented repairs and the captured harness.

## Benchmarks

Each benchmark has its own folder. Completed campaigns are published in dated
run folders; planned selections are kept separately from scored results.

| Benchmark | Status | Records |
| --- | --- | --- |
| [WildClawBench](wildclawbench/README.md) | First campaign complete | [2026-10-12 results and methods](wildclawbench/runs/2026-10-12/README.md) · [Interactive report](wildclawbench/runs/2026-10-12/report/index.html) |
| [PinchBench](pinchbench/README.md) | Frozen plan; session validation pending | [Task selection](pinchbench/frozen-plan.json) |
| [Terminal-Bench 4.0](terminal-bench-4.0/README.md) | Frozen plan; execution pending | [Task selection](terminal-bench-4.0/frozen-plan.json) |

## Comparison principles

Compare agents on matched tasks and recorded conditions while preserving each
agent's native loop, tools and configured preset. Freeze versions for each
campaign, retain genuine failures, and document environment or grader repairs
consistently across affected agents.

Report missing measurements and uncertainty explicitly. Read a run's methods
before quoting its scores: published campaigns may use modified benchmark
subsets, and close scores do not establish a statistically reliable winner.

## Reproduce or contribute

Start with the selected run's methods and reproduction prompt, then read the
[harness guide](harness/README.md) and [agent instructions](AGENTS.md).
Reproduction requires the recorded dependencies, inputs and preflight checks;
the captured harness is not a verified one-command installer.

For comparisons with new agent versions, create a separate campaign and preserve
existing records. Suggestions for additional benchmarks, agents, measurement
improvements and evidence-backed fixes are welcome.

```sh
python3 harness/validation/verify.py
```

This offline check verifies the published inventory and score invariants without
starting agents or consuming inference allowance.

## Repository layout

```text
<benchmark>/runs/<date>/   Results, reports, traces, methods and provenance
<benchmark>/              Benchmark overview and frozen selections
harness/                  Captured controller, adapters and publication tools
docs/                     Repository and hosting guides
```

Run dates use the UTC date of the final accepted attempt. New campaigns receive
separate folders, with a time or campaign identifier if the date already exists.

## Public evidence and licensing

Published records are sanitized to remove credentials, private reasoning, owner
paths and embedded image payloads. Large generated artifacts and raw private
captures are not included; each run documents available evidence and omissions.
Hashes establish provenance but do not make omitted artifacts downloadable.

The harness is [MIT-licensed](LICENSE). Upstream benchmarks, agent code and quoted
record material retain their own licensing conditions.

Reports support light and dark themes according to the browser's preference.
See the [GitHub Pages guide](docs/github-pages.md) for static hosting instructions.
