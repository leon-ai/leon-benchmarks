# PinchBench — run ending 2026-10-15

[Interactive report](report/index.html) · [Complete metrics](metrics.json) · [Frozen tasks](tasks.json) · [Reproduction prompt](reproduction.txt)

32 tasks across 11 domains; three attempts per agent/task, **288 accepted attempts**.
Each domain has three tasks except Memory, which has two. Scores average the three
attempts per task, then tasks within a domain, then the 11 equally weighted domains.
The final accepted candidate attempt ended October 15 UTC. Grading recovery and
publication followed without rerunning valid candidate attempts.

| Rank | Agent | Score |
| ---: | --- | ---: |
| 1 | Hermes | 84.59% |
| 2 | OpenClaw | 84.56% |
| 3 | Leon | 80.54% |

Hermes and OpenClaw differ by only 0.03 percentage points. Their local bootstrap
intervals overlap substantially; these point estimates do not establish a reliable
winner. The report includes paired selected-task bootstrap intervals and differences.

| Domain | Tasks | Leon | Hermes | OpenClaw |
| --- | ---: | ---: | ---: | ---: |
| Productivity | 3 | 100.00% | 100.00% | 100.00% |
| Research | 3 | 66.89% | 68.92% | 63.39% |
| Writing | 3 | 93.44% | 95.14% | 84.25% |
| Coding | 3 | 91.85% | 100.00% | 100.00% |
| Analysis | 3 | 95.56% | 96.22% | 95.00% |
| CSV analysis | 3 | 74.82% | 86.69% | 94.77% |
| Log analysis | 3 | 58.68% | 66.79% | 77.38% |
| Meeting analysis | 3 | 30.67% | 32.31% | 31.78% |
| Memory | 2 | 90.83% | 99.83% | 97.58% |
| Skills | 3 | 98.61% | 95.99% | 98.82% |
| Integrations | 3 | 84.59% | 88.59% | 87.19% |

## Conditions and methods

- PinchBench revision `819384ae830492365b8363fc26bc2602e73f216d`. The exact selected task text is retained
  under `harness/tasks/`; restore source assets from that pinned revision.
- Leon `111ccc8de5719108eebd4906ed0485cba5de7a50`; Hermes `v0.21.6` /
  `818c13be1dc4fd28987e1e881a9408224afd4535`; OpenClaw `v2026.10.1` /
  `e178c39e9aae3fa440735217f9aef9574f286677`.
- Direct ChatGPT/Codex subscription OAuth; GPT-6.1 Sol, high effort, priority
  requested. Returned service tiers are recorded separately; requested priority
  does not prove confirmed served Fast. No OpenRouter or billing API keys.
- Configured native loops/tools, rather than factory-default presets or a common
  replacement loop. Hermes persistent memory/user-profile is disabled, with 90/50
  main/delegated iteration limits. Native conversation reuse and workspace
  persistence across fresh sessions passed three-agent controls before scoring.
  Memory-domain scores concern the selected file/session tasks; they do not prove
  long-term autobiographical memory or equivalent native memory features.
- One task cohort at a time, up to nine isolated attempts: three per agent.
  Each has two CPUs and 4,096 MB. Native task deadlines remain unchanged; there is
  no arbitrary 30-minute cutoff. Actual provider conditions and concurrency affect
  timing; this is not a controlled concurrency/throttling experiment.
- GWS integration tasks use pinned synthetic fixtures and the benchmark's mock
  service, never an owner's real mailbox. The portable CLI and shared dependencies
  are pinned in `harness/dependencies/`; runtime binaries are not vendored.
- Native task prompts, automated checks and rubric weights are unchanged. Capture
  normalization preserves native names, nested Hermes dispatches and actual GWS
  invocations. CLI invocation intent is separate from model function calls and
  does not establish successful execution or semantic correctness.

## Fairness review and repairs

The [final contextual review](provenance/campaign-snapshots/pinch-contextual-fairness-review.json)
is bound to the accepted evidence fingerprint. Checks covered all 32 task input,
rubric, native budget and resource contracts; all 288 effective transcript inputs;
and all 75 timed-out workspaces. No unresolved setup/grading defect remained in
that reviewed evidence. This is an evidence-based audit, not a guarantee that every
possible semantic issue has been ruled out.

Demonstrated defects were repaired with original evidence retained: native worker
working directories, portable dependencies, expired-auth slots, conversation/tool
capture, Unicode JSONL record boundaries, content-block serialization, and deadline
eligibility. Native graders were replayed from saved evidence where possible;
only audited environment-invalid candidate slots were replaced earlier. The final
post-suite recovery reran no candidate attempts. Original and abandoned judge
requests are included separately from candidate consumption.

All nine Meeting Index attempts read matching valid inputs but did not produce
`meeting_index.md` within the native 180-second budget. Council Votes similarly
missed the requested report in most attempts. These genuine outcomes remain scored.
Recovered HTTP 502/503 events retain their original requests, elapsed time and
native outcomes, including zero scores; recovery is not selected by task score.
Native unrequested Git operations, irrelevant tool calls and deliberate placeholder
endpoint failures are described in native judge notes rather than silently removed.

## Improvement priorities

| Agent | Evidence-backed priorities |
| --- | --- |
| Leon | 1. Save a minimum complete deliverable early: 31 of 96 attempts hit native deadlines. All three Meeting Index and all three Council Votes attempts lacked the required report at 180 seconds. Checkpoint a usable file before further analysis. 2. Verify analysis outputs against required fields: Syslog averaged 34.53% and HDFS 48.50%. Inspect native criteria and complete the requested report before optional follow-up work. 3. Remove irrelevant startup work: Native Blog and Workflow judge notes identify unrelated weather calls, duplicate discovery and setup commentary. Prefer task-relevant tools and avoid consuming short native budgets on unrelated work. |
| Hermes | 1. Finish meeting deliverables within the budget: Meeting Index scored zero in all three attempts; Council Votes mostly lacked the requested report. Extract the required fields and write a first report before expanding analysis or delegation. 2. Checkpoint log-analysis results: Apache Error Analysis averaged 35.00%, with large attempt variability and deadline outcomes. Persist the required report and verify its counted errors early. 3. Validate finance-report requirements: Finance Report averaged 63.75%. Use the native criterion breakdown to verify all requested calculations and report sections; avoid treating successful tool execution as complete task accuracy. |
| OpenClaw | 1. Deliver a report before research expires: All three IT Procurement attempts scored zero at the native five-minute deadline. All Meeting Index and Council Votes attempts also lacked their requested reports. Save an evidence-backed partial deliverable before further searching. 2. Honor writing constraints more consistently: Humanizer averaged 65.42%, below its other writing tasks. Verify the task's requested style and content against the native rubric before finalizing. 3. Avoid unsolicited repository work: Blog and Workflow judge notes identify unnecessary repository inspection and commit attempts, including recovery from missing Git identity. Keep the native workflow focused on the requested deliverables. |

These priorities describe the pinned versions and configured presets on these
selected tasks. Native judge notes are evidence of the verdict, not independently
verified root-cause diagnoses of every product behavior.

## Metrics and coverage

The [recovered metrics contract](metrics-plan.json) is the same as the first
WildClawBench report. The HTML exposes overall/domain/task scores, repeated-attempt
variation, task/lifecycle time, p50/p95 request latency, first text/tool timing,
observed input/output/cached/reasoning tokens, attempted/completed/unclosed inference
counts, schema validity and coverage, explicit tool outcomes, repeated signatures,
assistant-event spacing, effective response throughput and 8 strict shared-success
tasks for successful-efficiency comparisons. Per-domain aggregates are also present
in the machine-readable metrics.

Candidate, image, original judge, accepted recovery and prior abandoned/superseded
judge usage are separated. Missing usage remains unknown; observed totals may be
lower bounds. Cached input and reasoning output are subsets, not extra tokens.
Four recovered verifier validation errors are retained as original harness
exceptions, and excluded from agent runtime-failure counts because the native
turns demonstrably completed. See `provenance/metric-derivation-review.json`.
Schema validity is not semantic per-call accuracy. Tool statuses have different
native coverage; event spacing includes tools/thinking, throughput includes waiting
and reasoning, and historical peak memory/disk and universal false-completion rates
were not captured. The dashboard labels these limits rather than inventing values.

## Evidence and reproduction

- `attempts/<agent>/<task>/<trial>/`: sanitized original result/config, telemetry,
  accepted score, native criteria, per-turn metadata and actual accepted grader input.
  Every accepted attempt has an available compressed action trace. Recovered and
  abandoned judge request ledgers remain separately labeled.
- `provenance/`: source/export hashes, private raw inventory, environment controls,
  original/recovered grading receipts, source map and final fairness reviews.
- `harness/scripts/benchmarks/`: captured controller/helpers. `harness/runtime/benchmarks/`:
  actual phase-specific runtime wrappers. The shared top-level harness remains the
  older WildClawBench reference; use this run's code for PinchBench.
- `harness/dependencies/`: pnpm locks, portable GWS binary source/checksums,
  actual CLI observer, mock helper, and PyYAML 6.0.3 pure-Python manifest.
  `provenance/licenses/PinchBench-LICENSE` retains the upstream MIT notice.
- `public-manifest.json` and `publication-validation.json`: public checksums and
  offline score/privacy verification. Raw private captures are frozen in place
  without duplicating large artifacts. Hash inventories do not publish omitted bytes.

[Reproduction instructions](reproduction.txt) describe fresh runs and resuming.
`python3 harness/prepare.py --run pinchbench/runs/2026-10-15 --workspace <empty-dir>`
from the repository root stages reference code/conditions only. It does not install,
authenticate, launch, or prove fresh-machine reproducibility. Render portable paths,
restore pinned dependencies/runtime and pass the native preflights before inference.
No fresh-machine end-to-end run has been executed from this public package.

The public package omits credentials, account IDs, private reasoning, owner paths,
inline image payloads and large generated binaries. Public task fixtures may contain
synthetic credentials and third-party data; those are distinct from owner secrets.
The harness is MIT-licensed; upstream and quoted material retain their own notices.
