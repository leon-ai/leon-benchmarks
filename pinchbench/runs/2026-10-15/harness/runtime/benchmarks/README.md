# Comparing assistant runtimes

This harness runs Leon, OpenClaw, and Hermes on the same WildClawBench tasks with
OpenAI `gpt-6.1-sol`, high reasoning, and Fast requested (`priority`). It measures the complete
assistant, including its native tools, skills, prompts, and agent loop.

Leon runs through its production ReAct duty; Hermes runs through its public
`AIAgent` constructor so reasoning and the service tier are explicit. OpenClaw
runs through its local CLI with a dedicated configuration. These entry points
retain each assistant's own agent loop and tool execution.

Benchmark-provided skills are copied into each assistant's isolated native skill
directory before its agent initializes. Results record this setup, including
tasks without supplied skills. Reports keep trials without verified task-skill
setup in a separate phase.
Declared metadata aliases make task skill names compatible with native name
validation while preserving their instructions.

Inference uses ChatGPT/Codex subscription access. Leon borrows access from its
selected ChatGPT connection; Hermes and OpenClaw borrow access from Hermes's
configured Codex login. Native stores retain refresh tokens. Containers receive
only short-lived access tokens in a private file outside exported logs. API
billing keys are not forwarded. OpenClaw's Codex transport needs a small request
configuration wrapper to transmit its selected Fast tier.
Inference transports reject API billing routes and model substitutions before
dispatch. Credential provisioning requires the same subscription workspace for
all three assistants.
Transport wrappers apply the declared reasoning and tier to auxiliary inference
such as compaction as well as main agent calls. Native prompts and loops remain
intact.

The fixed [pilot manifest](pilot.json) selects two tasks from each of six
categories before scores are observed. Three independent attempts per agent
produce 108 trials. This is a pilot, not the full benchmark or a memory benchmark.
Cross-session learning requires a separate suite such as PAST-Bench.

## Prerequisites

- Docker and Docker Compose, curl, uv, and pnpm.
- A working Leon installation with its managed binaries and Node.js tool dependencies.
- Installed OpenClaw and Hermes runtimes on the same Linux machine.
- A selected ChatGPT OAuth connection in Leon and a working Codex login in
  Hermes. `LEON_HOME`, `LEON_PROFILE`, and `HERMES_HOME` select their native
  credential stores.
- The official `wildclawbench-ubuntu:v1.3` image, loaded from the
  [benchmark's image archive](https://huggingface.co/datasets/internlm/WildClawBench/tree/main/Images).

Run from the standalone benchmark directory (`~/Workspace/leon/benchmarks`):

```bash
uv run --python 3.13 scripts/benchmarks/benchmark.py prepare
uv run --python 3.13 scripts/benchmarks/benchmark.py run
uv run --python 3.13 scripts/benchmarks/benchmark.py report
```

Preparation downloads the immutable dataset revision in the manifest and exports
runtime source without repository history or owner profiles. Read-only dependency
and binary mounts reuse installed code. Each trial gets a fresh container and
assistant home. Graders are uploaded after agent execution.

`LEON_BENCHMARK_LEON_ROOT` selects the Leon checkout for a new preparation and
defaults to the sibling `leon/` directory. Existing comparisons use their frozen
source snapshots. Never prepare into an existing comparison directory.

The pilot allows three concurrent trials. Harbor exports each trial's results
and removes its container immediately afterward, including failed trials. The
base image is shared across trials. Delete its downloaded archive after loading
it into Docker; remove the image once all comparisons finish if it is no longer
needed. Preserve result directories for analysis.

`run` consumes subscription usage for inference and judging. To select a task or assistant:

```bash
uv run --python 3.13 scripts/benchmarks/benchmark.py run \
  --agent leon --task 06_Safety_Alignment_task_1_file_overwrite --attempts 1
```

Use `--output PATH` consistently to keep separate preparations. A different image
is permitted only for smoke checks: prepare with `--image IMAGE`, then run and
report with `--label smoke`. Smoke scores are not pilot benchmark results.
Do not prepare into an output directory while its trials are running, because
the source snapshot and task files must stay fixed for the entire comparison.

To continue a stopped job without repeating any saved trial, including failed
or interrupted trials:

```bash
uv run --python 3.13 scripts/benchmarks/benchmark.py resume --job JOB
```

An explicitly recorded authentication transition preserves the original input
fingerprint and provenance alongside the new phase. Results from different
authentication or judge configurations must be reported separately.

## Evidence and interpretation

Grading retries preserve the original agent record, artifacts and verifier
output. A `grading-recovery.json` receipt identifies the original inputs by hash
and links a separately saved verifier result. Reports verify that evidence before
using the recovered score; agent timing and usage remain from the original run.

Results live under `tmp/benchmarks/comparison/` by default:

- `provenance.json`: dataset revision, task selection, runtime versions, image,
  and mechanical environment adjustments. Linux shell line endings are
  normalized; browser setup reuses the image's installation and uses pnpm when
  installation is needed; unavailable warmup entry points are recorded.
- `jobs/JOB/`: Harbor trial results, artifacts, native logs, and ATIF trajectories.
- `agent/requests.jsonl` in each trial: actual transmitted model, reasoning,
  service tier, and HTTP status. Prompts, headers, and credentials are excluded.
- `pilot-results.json`: scores, per-task attempt ranges, time, token totals where
  available, and exceptions/grader errors/unverified inference settings.

Use `report --job JOB` for one comparison. The default report combines jobs with
the selected label, so repeated debugging runs should be reported separately.
Missing usage or cost stays `null`; a missing cost is not a free run. Provider
account billing remains the authority for billed dollars.

Scores retain the upstream rubrics. Tool-dependent criteria can favor a specific
integration: shell-based REST access does not become a native Slack tool call.
Read the per-criterion scores and traces when interpreting such tasks. Report
task coverage and runtime versions alongside any overall score, and keep failed
environment setup or grading visible instead of claiming they measure capability.

Judging uses the same subscription model, reasoning effort, and Fast tier through
Responses. A transport adapter translates the upstream graders' text and image
messages. This changes the published judge configuration, so identify the results
as a subscription variant of WildClawBench rather than a leaderboard submission.

Sources: [WildClawBench](https://github.com/InternLM/WildClawBench),
[Harbor](https://github.com/laude-institute/harbor),
[PAST-Bench](https://github.com/Gen-Verse/PAST-Bench).

## Balanced comparisons

`balanced.py` consumes a frozen `selection.json` under its output directory.
The selection declares dataset revisions, domain quotas, hardware eligibility,
the seed used to resolve sampling ties, and the compatible saved results to reuse.
Task selection must precede new agent scores. Results compare matched tasks with
three attempts per assistant and give domains equal weight within each benchmark.
Report the two benchmark scores separately, with coverage and exclusions visible.

The active comparison keeps scripts, runtime snapshots, task inputs, results and
logs within this standalone directory. Historical result files retain their
original bytes; `relocation.json` maps their old absolute paths for reporting.
Installed Leon dependency directories have independent directory entries with
hardlinked payloads to save space, and are mounted read-only in containers.
Normal package replacement in the owner checkout cannot remove the retained
payloads. Avoid editing installed package files in place while a comparison runs,
because those payloads share file contents. Owner source edits do not affect the
frozen agent sources. Native login stores remain responsible for token refresh.

```bash
uv run --python 3.13 scripts/benchmarks/benchmark.py --help
# Use the same Harbor environment for balanced.py:
uv run --python 3.13 --with harbor==0.22.0 --with huggingface-hub==1.29.0 \
  scripts/benchmarks/balanced.py preflight
```

The runner processes one task group at a time. Terminal-Bench reference solutions
must pass five independent oracle checks before candidate inference starts.
Failed validation holds the selected task without replacing it based on scores.
Runtime installation checks use no inference. Candidate runs retain the task's
published time and resource limits; WildClaw uses the pilot's shared resource settings.
Native agent source snapshots are reused, while a recorded runner overlay supplies
task working directories and the subscription judge transport.

Each trial exports artifacts before its containers are removed. Terminal tasks
use dedicated Docker builders, so task cleanup can remove their caches, images,
networks and volumes without pruning unrelated owner resources. Downloaded fixture
directories are released after the task group; source hashes, instructions, graders,
logs and scores remain available. Disk guards stop new work or cancel in-flight
work before the filesystem fills. Subscription allowance checks leave a reserve
and wait for replenishment without changing the model or buying additional access.

Saved candidate attempts are never implicitly repeated. An interrupted trial with
no final result remains available for review instead of being deleted by Harbor's
default resume behavior. `status.json`, `results.json`, and cleanup receipts expose
execution progress, grading issues, and resource ownership.

The official WildClaw image is loaded only when its phase needs it. Its streaming
loader verifies the original archive checksum and final image identity, compressing
embedded layers to reduce temporary storage on Docker's legacy archive loader.
No complete downloaded image archive is retained.
