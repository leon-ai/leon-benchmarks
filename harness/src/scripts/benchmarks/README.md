# Comparing assistant runtimes

The harness compares Leon, OpenClaw, and Hermes through their native agent loops,
prompts, tools, and skills. Campaign manifests freeze agent versions, dependency
snapshots, task selection, and inference settings before scoring. Private source
copies keep owner development isolated from candidate runs.

The active campaign is recorded in `tmp/benchmarks/active-comparison.json`.
Its `campaign.json`, `selection.json`, and `runtime-provenance.json` describe the
conditions; `status.json`, `results.json`, and `controller.log` show progress.

## Inference and measurement

Inference uses direct ChatGPT/Codex OAuth subscription access with GPT-6.1 Sol,
high reasoning, and Fast requested as `priority`. Requested and returned tiers
are separate measurements. Containers receive access tokens; host native stores
retain refresh credentials. API billing, OpenRouter, and substitute model routes
are blocked, including auxiliary calls.

Passive observers record request counts, available token usage, response latency,
tool schemas, and tool calls. Local schema checks add no model requests. Native
execution histories are exported for unchanged benchmark graders. Candidate and
judge usage are separate. Schema validity does not establish semantic correctness;
unverified semantic accuracy remains unknown.

Native tool events are journaled so published deadlines do not erase completed
tool results. An interrupted call remains unfinished in the exported history.
Artifact-only grading can recover a missing grade when the unchanged verifier
reproduces existing native scores. Hash-validated receipts identify the original
outputs and grading evidence; original timeout records remain intact. Grader
replays add no candidate attempts and do not extend the agent's task budget.

## Execution and cleanup

Benchmark phases run sequentially: WildClawBench, PinchBench, Terminal-Bench.
Phase adapters and setup checks must be validated before their candidate scores.
Three attempts per agent use independent containers and profiles. Admission allows
up to nine concurrent attempts for one task, subject to host resources and
subscription allowance. Published task timeouts remain in force.

Trials export evidence and receive grades before container deletion. Shared images
and immutable fixtures serve the complete nine-attempt cohort. Task-specific
resources are removed after that cohort; suite-wide resources remain until no
remaining suite tasks need them. Cleanup follows resource ownership records and
preserves unrelated Docker resources.

Reports rank matched tasks with three valid attempts for every agent and equal
domain weights. Publish subset selection, repetition counts, and judge settings
with results; local comparisons are not official leaderboard submissions.
Interrupted attempts require review rather than silent repetition.

The persistent controller is `leon-benchmark-campaign.service`. Use
`systemctl --user status leon-benchmark-campaign.service` to inspect it.
Local benchmark execution requires the computer to remain powered on.

## PinchBench adapter

PinchBench uses its pinned task parser, declared fixtures, automated checks,
hybrid weights, judge rubrics, and execution budgets. Graders and private answer
keys become visible only after candidate execution. Integration tasks use an
independent seeded FWS service in each trial; Google Workspace commands use mock
credentials while subscription inference connects directly to OpenAI.

Multi-session tasks share one task budget. Native conversations retain history
between ordinary turns; `new_session` clears conversation history and preserves
workspace files. Prompts are delivered in sequence, so future turns are not exposed
early. Native snapshots preserve each delivered turn before a fresh conversation.
Tool names remain unchanged in grading evidence.

Phase-specific wrappers are staged separately and require preflight validation
before execution. Dependency lockfiles and release checksums identify mock-service
tools. Shared immutable image layers serve both suites that need them; task images
and trial containers are retired as their cohorts finish.
