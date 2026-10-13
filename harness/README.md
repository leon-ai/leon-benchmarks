# Benchmark harness

This directory preserves the controller and adapters used for the dated local
comparison. It runs each product's native agent loop with a documented preset;
it does not replace those loops with a common agent implementation.

`src/scripts/benchmarks/` retains the captured Python import namespace. Its
`README.md` is historical documentation, not the repository's setup entry point.
`reference-source-manifest.json` identifies original and packaged source hashes.
The only runtime source packaging change is a portable Hermes Python fallback;
controller logic and recorded inference settings are preserved.

| Responsibility | Captured modules |
| --- | --- |
| Scheduling, cohorts, admission and cleanup | `campaign.py`, `balanced.py`, `scheduling.py`, `shared_admission.py` |
| Native agent adapters | `harbor_agents.py`, `run-leon.ts`, `run-hermes.py`, `configure-openclaw.mjs`, `pinch_agents.py` |
| Subscription authentication and inference observers | `export_subscription.py`, `export-subscription.ts`, `audit_openai.py`, `audit-openai.mjs`, `inference-observer.mjs` |
| Fixtures, services and grading | `benchmark.py`, `service_readiness.py`, `social_service_recovery.py`, `safety_fixture_grader.py`, `pinch_grader.py`, `judge_images.py` |
| Transcripts, telemetry and reports | `native_transcript.py`, `metrics.py`, `wild_archive.py`, `wild_report.html` |
| Fairness, integrity and evidence recovery | `task_integrity.py`, `fairness_audit.py`, modules ending in `_recovery.py` |

`reporting/export_public.py` creates sanitized publication records. It removes
credentials, private reasoning, owner paths and embedded images, preserving
numeric measurements. Large action traces and ledgers are compressed as JSONL
or JSON, with source/export hashes in the publication map. It never runs agents.

`validation/verify.py` checks the public repository, scores, hashes and links
without contacting any inference service. `dependencies/controller-requirements.lock`
records the controller environment. Native agent dependencies and historical
container recipes remain identified by the frozen evidence inventory and runtime
receipts; binaries, wheel caches and upstream datasets are not vendored here.

## Offline inspection

```sh
python3 harness/validation/verify.py
python3 harness/prepare.py --help
```

The dashboard can be opened as a file. For companion records and clean browser
navigation, serve the repository locally:

```sh
python3 -m http.server 8000 --bind 127.0.0.1
```

Open `http://127.0.0.1:8000/`. This starts no benchmark runs.

## Reproduction boundary

Use the dated run's `reproduction.txt`, source inventory, task revisions and
adaptation receipts. `prepare.py` stages source code and the frozen selection into
a **new, empty** workspace; it does not install dependencies, authenticate, build
containers, validate regenerated inputs or start inference. Host paths must be
rendered explicitly from the recorded runtime/mount templates. Prepare isolated
agent sources rather than modifying a developer's active checkout.

Install pinned native dependencies, rebuild retired task environments and restore
canonical Social inputs before smoke checks. Stage the subscription helper into
the isolated Leon source where its native authentication code expects it. Verify
fixture hashes, tools, published deadlines, native outputs and authentication
before invoking `python -m scripts.benchmarks.campaign --output <campaign>` from
the staged workspace. That command consumes subscription allowance and is not
part of publication validation. Never invoke it against this repository's result
folder or copy historical completion markers into a new experiment.

A fresh-machine end-to-end reproduction has **not** been executed from this
public package. It is an evidence-backed recipe and captured implementation, not
a verified one-command installer. PinchBench remains gated on native session
controls; its preparation is not proof of successful scored execution.

`validation/browser.mjs <disposable-chromium-profile>` checks a report already
opened in headless Chromium with remote debugging enabled. It checks sections,
filters, dialogs and mobile width, writes screenshots into that temporary profile,
and closes that test browser. Node.js must provide its built-in WebSocket API.
It neither accesses a personal browser profile nor starts model inference.

Public report presentation uses `reporting/wild_report.html` and
`reporting/agent-colors.json`; the captured reference template under `src/` stays
unchanged. The exporter embeds metrics and colors into a self-contained HTML
report. Presentation changes must preserve all numeric evidence and be checked
in both browser color schemes before refreshing the publication manifest.
