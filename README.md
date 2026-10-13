# Leon benchmarks

Recorded comparisons of Leon, Hermes and OpenClaw, with frozen task selections,
agent versions, native score criteria and performance telemetry.

**These are configured, modified local benchmark subsets, not official leaderboard
submissions or factory-default product scores.** Read each dated run's methods
and repairs before quoting its results. Close scores are not evidence of a
statistically established winner.

| Benchmark | Recorded status | Tasks | Scored attempts |
| --- | --- | ---: | ---: |
| [WildClawBench](wildclawbench/runs/2026-10-12/README.md) | Complete | 36: six per domain | 324 |
| [PinchBench](pinchbench/README.md) | Paused, native session validation required | 32 | 0 |
| [Terminal-Bench 4.0](terminal-bench-4.0/README.md) | Planned, paused | 21 | 0 |

[Open the interactive WildClawBench report](wildclawbench/runs/2026-10-12/report/index.html).
It includes overall/domain scores, all three repetitions per task, token and
inference usage, latency, native failures, tool-schema checks, efficiency and
critical improvement areas. Missing measurements remain unknown.

The final WildClawBench score is the mean of six equally weighted domain scores;
each domain averages six tasks, each task averages three independent attempts.
Hermes **79.40%**, OpenClaw **78.51%**, Leon **78.25%**. The dashboard reports
selected-task bootstrap intervals and pairwise uncertainty.

## Repository layout

```text
harness/                       Captured controller, native adapters and export/verification tools
wildclawbench/runs/2026-10-12/   Dated report, measurements, action traces, repairs and provenance
pinchbench/                     Frozen task plan; no scored results yet
terminal-bench-4.0/             Frozen task plan; no scored results yet
.local/                        Ignored access to the owner's private workspace
```

Run folders use the UTC date of the last accepted attempt, rather than the report
export date. This run finished on October 12 and was frozen/exported October 13.
Future campaigns get separate immutable folders; if a date repeats, append a UTC
time or campaign identifier. Do not replace prior evidence with updated versions.

## Publication and private evidence

Public records remove OAuth credentials, private reasoning, owner paths and
embedded image payloads. Scores and telemetry are retained. Compressed traces
can be opened with `gzip -dc <file.jsonl.gz>`. The publication map records both
original and exported hashes; the public manifest verifies the exported files.

Large generated artifacts, binaries, dependency caches and raw private captures
are not included in Git. Their original hash inventory is supplied, but a hash
alone does not make the original bytes publicly downloadable. Full raw artifacts
currently require a separate reviewed evidence distribution. The owner retains
the complete original archive through the ignored `.local/workspace` link.
Never publish that symlink, private credentials or raw reasoning captures.

The source exports had no Git metadata. Declared commit identities come from
recorded provenance; byte inventories establish the actual source snapshots.
See [the harness](harness/README.md) for dependencies and the reproduction boundary.

```sh
python3 harness/validation/verify.py
```

The root `index.html` and `.nojekyll` support static hosting, including GitHub
Pages. This repository has not been published, and no benchmark is started by
opening a report or running the offline verifier.

The harness is MIT-licensed. Upstream benchmark inputs, rubrics and agent code
remain subject to their own licenses and are referenced rather than vendored as
complete source distributions. Records may quote task instructions and tool
outputs; inspect those materials and benchmark terms before redistribution.
