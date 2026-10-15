# Working on Leon benchmarks

This repository records comparisons through each agent's native loop, tools and
configured preset. Read `README.md` and `harness/README.md` first. Loading these
instructions does not authorize starting, resuming or publishing a campaign.

## Experiment specification

- Use the selected dated run's `tasks.json`, `conditions.json`, `metrics-plan.json`,
  `reproduction.txt` and provenance as the experiment specification. Do not infer
  active settings from the historical pilot roster or old status files.
- Dated references are `wildclawbench/runs/2026-10-12/` and
  `pinchbench/runs/2026-10-15/`. Terminal-Bench's selection remains a frozen plan.
  PinchBench's run-local harness includes its reviewed adapters and runtime wiring;
  the shared baseline harness remains the WildClawBench reference.
- Keep captured source under `harness/src/` and raw evidence immutable. Reporting
  templates and publication tools live under `harness/reporting/`.
- Reproduction instructions are not a verified fresh-machine installer. Rebuild
  pinned dependencies/inputs and pass native preflights before scored inference.
  PinchBench conversation reuse and fresh-session file persistence are required gates.

## New campaigns and resuming

1. Distinguish reproducing a dated run, comparing new agent versions, and resuming
   unfinished work. Resolve unclear scope before consuming subscription allowance.
2. Create an isolated workspace. `python3 harness/prepare.py --workspace <empty-dir>`
   stages the baseline code/selection only; it does not install or launch agents.
   Use `--run <dated-run-folder>` to select another published reference.
3. For exact reproduction, retain declared versions and verify source bytes. For a
   new-version comparison, resolve each requested release/branch to an exact commit,
   record it in new manifests, and freeze it for the whole campaign. Never modify
   a developer's active checkout or follow a moving branch during execution.
4. Reuse frozen task selection, native budgets, grading and documented repairs unless
   the owner authorizes changes. Validate updated adapters without substituting a
   common agent loop. Record every necessary configuration/adapter difference.
5. A fresh comparison runs all agents on fresh matched slots. Never combine prior
   campaign scores with new versions. A resume skips valid accepted slots and
   reviews interrupted attempts before replacement.
6. Keep working results private until validated/sanitized. Export a new dated folder
   under `<benchmark>/runs/`, using the UTC date of the final accepted attempt;
   append a time or campaign ID if that date already exists. Preserve prior scores,
   accepted records and provenance. Presentation-only changes may update a report,
   with validation receipts and checksums refreshed.

## Inference and execution

- Use the campaign's recorded model, effort and presets consistently for all agents.
  The baseline uses direct ChatGPT/Codex subscription OAuth, `gpt-6.1-sol`, high
  effort and `priority` requested. No OpenRouter, billing API keys or silent model
  substitutions. Record returned model/tier separately; do not claim guaranteed
  served Fast from requested priority. Keep native auxiliary image usage separate.
- Run suites in order: WildClawBench, PinchBench, Terminal-Bench 4.0; one suite at a
  time. Preserve each suite's preflight gates and exact task/domain quotas.
- Process one task cohort at a time, up to nine isolated attempts: three attempts
  for each of three agents. Share only immutable layers. Baseline limits are two
  CPUs and 4,096 MB per attempt; respect resource/quota admission and record actual
  concurrency. Keep published deadlines; never add an arbitrary 30-minute cutoff.
- Export evidence and grade before deleting an attempt's containers, volumes and
  networks. Prepare shared task resources once for all consumers; retire them
  immediately after the final consumer. Keep suite layers only while needed.
  Preserve frozen evidence/sources and never globally prune unrelated Docker objects.

## Fairness and metrics

- Preserve genuine failures, deadline outcomes and all three attempt scores. For
  zero/very low scores, missing outputs or concrete environment/grader errors, use
  quick local checks first. Do not add expensive model diagnostics by default.
- Repair only demonstrated fixture, wiring, capture or grading defects. Preserve
  original evidence and apply replacements/repairs consistently to affected slots.
  Do not rerun low scores selectively. Selection/rubric changes need owner approval.
- Follow `metrics-plan.json`: native criteria, completion, artifact/action/safety
  checks, schema validity/coverage, structured tool outcomes, response and task
  timings, tokens, inference volume, auxiliary/judge costs, variability and shared
  successful-efficiency comparisons. Missing measurements stay unknown.
- Schema validity is not semantic call accuracy; event spacing is not pure model
  latency; full-response throughput is not isolated decoding speed. Cached input
  and reasoning output are subsets. Disclose missing usage and coverage differences.
- Rank only matched cohorts: average three attempts per task, then tasks per domain,
  then equally weight domains. Publish presets, modifications, uncertainty and
  limitations; do not present modified local subsets as official leaderboard scores.

## Publication and privacy

- Keep OAuth credentials, account IDs, private reasoning, owner paths, private
  conversation logs and `.local/` out of Git and Pages. Inspect compressed traces
  as well as plain files. Benchmark fixtures may contain synthetic credentials
  and public third-party data; distinguish them from owner secrets.
- Do not copy private session logs to justify styling or settings. Retain only the
  required non-sensitive values and public provenance. Preserve license notices.
- Raw artifacts are not automatically public because their hashes are included.
  Disclose omitted evidence and any separately reviewed artifact distribution.
- Root `index.html` links to dated reports. Use relative URLs compatible with a
  GitHub Pages project path. `docs/github-pages.md` describes publication; do not
  create a remote, push, commit or enable Pages unless the owner asks.

## Editing and checks

- Keep changes minimal, reuse captured helpers and use pnpm for Node tooling.
  Explain lasting behavior in documentation; put campaign-specific facts in the
  dated manifests. Do not alter accepted numeric results for presentation changes.
- Use `harness/reporting/wild_report.html` or `pinch_report.html` for the matching report and
  `agent-colors.json` for the recorded agent palette. Follow the browser's
  `prefers-color-scheme`; page backgrounds are white in light mode and black in
  dark mode. Keep labels legible, not dependent on color alone.
- Run relevant offline lint/syntax checks and existing tests. For controller edits,
  with locked dependencies installed: `PYTHONPATH=harness/src python3 -m unittest
  scripts.benchmarks.test_scheduling`. Never run inference as a lint/UI check.
- For report changes, check both themes, desktop/mobile, chart text, filters,
  dialogs and evidence links. `harness/validation/browser.mjs` checks a disposable
  Chromium session; do not use a personal browser profile.
- After reviewed public-file changes, regenerate the public manifest/receipt with
  `python3 harness/validation/verify.py --seal`, then verify with the same command
  without `--seal`. Investigate unexpected evidence changes before accepting them;
  resealing must not legitimize altered results.
- Preserve unrelated working-tree/index changes. Report validation and remaining
  limits. Suggest a conventional commit message; do not commit unless asked.

Guidance: [AGENTS.md standard](https://agents.md/),
[Codex instructions](https://learn.chatgpt.com/docs/agent-configuration/agents-md),
[Claude Code memory/imports](https://code.claude.com/docs/en/memory).
