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
Pages. Opening a report, publishing the static site or running the offline verifier
does not start benchmark execution.

The harness is MIT-licensed. Upstream benchmark inputs, rubrics and agent code
remain subject to their own licenses and are referenced rather than vendored as
complete source distributions. Records may quote task instructions and tool
outputs; inspect those materials and benchmark terms before redistribution.

## Agent instructions and static hosting

Repository-wide benchmark guidance lives in [AGENTS.md](AGENTS.md); `CLAUDE.md`
imports that file. Use a dated run's reproduction prompt for its exact settings.
A new-version campaign uses new manifests and fresh matched attempts; existing
results remain preserved.

The report and landing page follow the browser's light/dark preference, with
white/black page backgrounds. Charts use the previously corrected comparison
palette: Leon `#1C75DB`, Hermes `#0000F2`, OpenClaw `#FF4D4D`. Labels remain
readable in both themes. Private session logs are not included.

See [GitHub Pages setup](docs/github-pages.md) to publish this static repository
from `main` at `/(root)`. Preparing these files does not publish the site.

The custom-domain target is [benchmarks.getleon.ai](https://benchmarks.getleon.ai/).
GitHub Pages settings and the Namecheap `benchmarks` CNAME record must both be
configured before that address serves the report. Keep the root and `www` DNS
records serving the main Leon website.
