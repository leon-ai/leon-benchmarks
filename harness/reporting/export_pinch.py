"""Freeze and publish PinchBench evidence without launching agents or judging."""

import argparse
import collections
import importlib
import json
import math
import random
import re
import shutil
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

from export_public import RECORD_FILES, Sanitizer, digest, export_file, save_json

DOMAINS = {
    "productivity": "Productivity",
    "research": "Research",
    "writing": "Writing",
    "coding": "Coding",
    "analysis": "Analysis",
    "csv_analysis": "CSV analysis",
    "log_analysis": "Log analysis",
    "meeting_analysis": "Meeting analysis",
    "memory": "Memory",
    "skills": "Skills",
    "integrations": "Integrations",
}
AGENTS = ("leon", "hermes", "openclaw")
SEED = 20261015
SAMPLES = 4000


def event_metrics(common, events):
    """Measure only visible native events and explicit execution-status evidence."""
    counts = collections.Counter()
    times = []
    starts = {}
    durations = []
    for event in events:
        message = event.get("message", {})
        when = common.timestamp(event.get("timestamp", message.get("timestamp")))
        if message.get("role") == "assistant":
            if when is not None:
                times.append(when)
            for part in message.get("content", []):
                if isinstance(part, dict) and part.get("type") == "toolCall":
                    starts[part.get("id")] = when
        if message.get("role") in ("tool", "toolResult"):
            status = common.explicit_execution_status(message)
            if status == "unknown" and isinstance(message.get("content"), list):
                for part in message["content"]:
                    if isinstance(part, dict) and part.get("type") == "text":
                        status = common.explicit_execution_status(
                            {**message, "content": part.get("text", "")}
                        )
                        if status != "unknown":
                            break
            counts[status] += 1
            start = starts.get(message.get("toolCallId"))
            if start is not None and when is not None and when >= start:
                durations.append(when - start)
    gaps = [b - a for a, b in zip(times, times[1:]) if b >= a]
    return {
        "transcript_present": True,
        "tool_result_counts": dict(counts),
        "tool_result_count": sum(counts.values()),
        "execution_status_coverage": (counts["success"] + counts["error"])
        / sum(counts.values())
        if counts
        else None,
        "tool_duration_seconds": common.distribution(durations),
        "assistant_event_spacing_seconds": common.distribution(gaps),
        "assistant_event_spacing_samples": gaps,
        "tool_duration_samples": durations,
    }


def effective_input(campaign, trial, receipt):
    """Find the actual accepted native grader input, including recovered captures."""
    if receipt:
        return (campaign / receipt["grade_file"]).parent.parent / "agent"
    return trial / "agent"


def derive(workspace, campaign, repository):
    """Reuse recorded telemetry formulas, preserving native scores and missingness."""
    sys.path.insert(0, str(workspace))
    common = importlib.import_module("scripts.benchmarks.wild_archive")
    balanced = importlib.import_module("scripts.benchmarks.balanced")
    fairness = importlib.import_module("scripts.benchmarks.fairness_audit")
    metrics = importlib.import_module("scripts.benchmarks.metrics")
    regrading = importlib.import_module("scripts.benchmarks.regrading")
    pinch = importlib.import_module("scripts.benchmarks.pinch")
    balanced.benchmark.configure_runtime(campaign)
    selection = json.loads((campaign / "selection.json").read_text())
    fairness.require_review(campaign, selection, "pinch")
    report = json.loads((campaign / "results.json").read_text())
    audit = json.loads((campaign / "pinch-fairness-audit.json").read_text())
    rows = [r for r in report["trials"] if r["suite"] == "pinch"]
    lookup = {item["name"]: item for item in selection["pinch"]}
    assert len(rows) == 288
    assert collections.Counter((r["task"], r["agent"]) for r in rows) == (
        collections.Counter({(task, agent): 3 for task in lookup for agent in AGENTS})
    )
    records = []
    for row in sorted(rows, key=lambda r: (r["task"], r["agent"], r["trial"])):
        trial = Path(row["trial"])
        receipt = regrading.verified_receipt(trial)
        record = common.trial_record(campaign, row, lookup[row["task"]]["domain"])
        result = json.loads((trial / "result.json").read_text())
        original_exception = result.get("exception_info") or {}
        record["original_harness_exception"] = record["exception"]
        if (
            original_exception.get("exception_type") == "ValidationError"
            and "VerifierResult" in original_exception.get("exception_message", "")
            and receipt
            and receipt.get("candidate_execution_preserved")
            and all(
                turn.get("completed")
                for turn in json.loads((trial / "agent/pinch-turns.json").read_text())[
                    "turns"
                ]
            )
        ):
            # Recovered verifier failures are distinct from native agent completion.
            record["exception"] = None
            record["runtime_finished_without_exception"] = True
        grade = (
            receipt["rewards"]
            if receipt
            else json.loads((trial / "verifier/pinch_score.json").read_text())
        )
        record["native_grade"] = common.numeric_grade(grade)
        record["judge_notes"] = grade.get("notes", "")
        record["diagnosis"] = {
            "category": "Published deadline"
            if record["exception"] == "AgentTimeoutError"
            else "Full native score"
            if record["score"] == 1
            else "Partial native rubric credit",
            "explanation": grade.get("notes")
            or "Inspect the native criteria and saved outputs.",
            "confidence": "Native grader verdict; causal attribution is limited to the documented fairness review",
        }
        source = effective_input(campaign, trial, receipt)
        events = [
            json.loads(line)
            for line in (source / "pinch-transcript.jsonl").read_text().split("\n")
            if line.strip()
        ]
        record["native_events"] = event_metrics(common, events)
        record["session_turns"] = json.loads(
            (trial / "agent/pinch-turns.json").read_text()
        )
        record["grading_type"] = lookup[row["task"]]["grading_type"]
        record["native_budget_seconds"] = lookup[row["task"]]["timeout_seconds"]
        recovery_files = []
        if receipt:
            recovery_files = list(
                dict.fromkeys(
                    [
                        receipt["requests_file"],
                        *receipt.get("prior_recovery_requests_files", []),
                    ]
                )
            )
        record["judge_recovery_calls"] = [
            {**common.call_record(call), "recovery_ledger": str(path)}
            for path in recovery_files
            for call in metrics.lines(campaign / path)
        ]
        record["judge_recovery_attempts"] = [
            {
                "requests_file": path,
                "metrics": metrics.inference_metrics((campaign / path).parent),
            }
            for path in recovery_files
        ]
        if recovery_files:
            record["judge_recovery"] = {
                **record["judge_recovery"],
                "attempted_inference_requests": sum(
                    r["metrics"]["attempted_inference_requests"]
                    for r in record["judge_recovery_attempts"]
                ),
            }
        records.append(record)
    task_specs = pinch.load_tasks()
    tasks = []
    for item in selection["pinch"]:
        task = {
            "id": item["name"],
            "domain": item["domain"],
            "title": task_specs[item["name"]].name,
            "agents": {},
        }
        for agent in AGENTS:
            runs = [
                r for r in records if r["task"] == task["id"] and r["agent"] == agent
            ]
            scores = [r["score"] for r in runs]
            task["agents"][agent] = {
                "score": statistics.mean(scores),
                "scores": scores,
                "attempts": [r["id"] for r in runs],
                "score_summary": common.distribution(scores),
                "metrics": common.aggregate(runs),
            }
        tasks.append(task)
    strict = [
        t["id"]
        for t in tasks
        if all(
            r["score"] == 1 and r["exception"] is None
            for r in records
            if r["task"] == t["id"]
        )
    ]
    rng = random.Random(SEED)
    samples = {a: [] for a in AGENTS}
    paired = {(a, b): [] for i, a in enumerate(AGENTS) for b in AGENTS[i + 1 :]}
    groups = [[t for t in tasks if t["domain"] == d] for d in DOMAINS]
    for _ in range(SAMPLES):
        drawn = [rng.choices(group, k=len(group)) for group in groups]
        scores = {
            a: statistics.mean(
                statistics.mean(t["agents"][a]["score"] for t in group)
                for group in drawn
            )
            for a in AGENTS
        }
        for a in AGENTS:
            samples[a].append(scores[a])
        for (a, b), values in paired.items():
            values.append(scores[a] - scores[b])
    agents = {}
    for agent in AGENTS:
        runs = [r for r in records if r["agent"] == agent]
        domains = {
            d: statistics.mean(
                t["agents"][agent]["score"] for t in tasks if t["domain"] == d
            )
            for d in DOMAINS
        }
        score = statistics.mean(domains.values())
        assert math.isclose(
            score,
            report["summaries"]["pinch"]["agents"][agent]["domain_macro_score"],
            abs_tol=1e-12,
        )
        agents[agent] = {
            "name": common.NAMES[agent],
            "score": score,
            "domains": domains,
            "bootstrap_interval": [
                common.quantile(samples[agent], p) for p in (0.025, 0.975)
            ],
            "all": common.aggregate(runs),
            "images": common.aggregate(runs, "image_calls", "image"),
            "judging": common.aggregate(runs, "judge_calls", "judge"),
            "recovery_judging": common.aggregate(
                runs, "judge_recovery_calls", "judge_recovery"
            ),
            "strict_shared_success": common.aggregate(
                [r for r in runs if r["task"] in strict]
            ),
        }
    conditions = json.loads((campaign / "campaign.json").read_text())
    conditions.update(
        status="complete",
        campaign_task_counts=conditions["tasks"],
        tasks={"pinch": 32},
        total_agent_runs=288,
        approved_task_replacements=[],
    )
    final_date = max(
        json.loads((Path(r["trial"]) / "result.json").read_text())["finished_at"]
        for r in rows
    )[:10]
    plan = json.loads(
        (repository / "wildclawbench/runs/2026-10-12/metrics-plan.json").read_text()
    )
    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "campaign": campaign.name,
        "benchmark": "PinchBench",
        "scope": "Frozen local subset; unchanged native task rubrics",
        "tasks": tasks,
        "domains": DOMAINS,
        "agents": agents,
        "records": records,
        "ranking": sorted(AGENTS, key=lambda a: agents[a]["score"], reverse=True),
        "strict_shared_success_tasks": strict,
        "metrics_plan": plan,
        "conditions": conditions,
        "evidence_fingerprint": audit["evidence_fingerprint"],
        "limitations": json.loads(
            (campaign / "pinch-contextual-fairness-review.json").read_text()
        )["limits"]
        + [
            "32 selected tasks: three per domain except Memory, which has two; equal domain weighting.",
            "Native task budgets are retained. Timed-out attempts can earn partial or full credit from eligible saved work.",
            "Schema validity is not semantic tool accuracy. Structured tool-result coverage differs by native export.",
            "Unreported token usage and unclosed requests remain unknown; observed totals may be lower bounds.",
            "Between-event spacing and response throughput include tools, reasoning and waiting; isolated model decoding speed is unknown.",
            "Historical peak memory/disk were not measured. Limits and cleanup provenance are recorded.",
            "Original, superseded and abandoned grading requests are retained separately from candidate usage.",
            "Large generated artifacts, private reasoning, credentials, owner paths and embedded payloads are omitted from the public package.",
        ],
        "provenance": {
            k: [
                {
                    field: row.get(field)
                    for field in ("suite", "task", "agent", "trial", "score")
                }
                for row in report.get(k, [])
                if row.get("suite") == "pinch"
            ]
            for k in ("superseded_trials", "withdrawn_trials")
        },
        "bootstrap": {
            "seed": SEED,
            "samples": SAMPLES,
            "method": "Paired task resampling within each domain, preserving three-attempt means and equal domain weights; conditional on selected tasks.",
            "differences": [
                {
                    "a": a,
                    "b": b,
                    "interval": [common.quantile(v, 0.025), common.quantile(v, 0.975)],
                }
                for (a, b), v in paired.items()
            ],
        },
    }
    data["domain_metrics"] = {
        a: {
            d: common.aggregate(
                [r for r in records if r["agent"] == a and r["domain"] == d]
            )
            for d in DOMAINS
        }
        for a in AGENTS
    }

    def score(agent, task):
        return next(t for t in tasks if t["id"] == task)["agents"][agent]["score"] * 100

    data["improvements"] = {
        "leon": [
            [
                "1. Save a minimum complete deliverable early",
                f"{agents['leon']['all']['exceptions'].get('AgentTimeoutError', 0)} of 96 attempts hit native deadlines. All three Meeting Index and all three Council Votes attempts lacked the required report at 180 seconds. Checkpoint a usable file before further analysis.",
                "task_meeting_",
            ],
            [
                "2. Verify analysis outputs against required fields",
                f"Syslog averaged {score('leon', 'task_log_syslog_boot'):.2f}% and HDFS {score('leon', 'task_log_hdfs_failures'):.2f}%. Inspect native criteria and complete the requested report before optional follow-up work.",
                "task_log_",
            ],
            [
                "3. Remove irrelevant startup work",
                "Native Blog and Workflow judge notes identify unrelated weather calls, duplicate discovery and setup commentary. Prefer task-relevant tools and avoid consuming short native budgets on unrelated work.",
                "task_blog",
            ],
        ],
        "hermes": [
            [
                "1. Finish meeting deliverables within the budget",
                "Meeting Index scored zero in all three attempts; Council Votes mostly lacked the requested report. Extract the required fields and write a first report before expanding analysis or delegation.",
                "task_meeting_",
            ],
            [
                "2. Checkpoint log-analysis results",
                f"Apache Error Analysis averaged {score('hermes', 'task_log_apache_top_errors'):.2f}%, with large attempt variability and deadline outcomes. Persist the required report and verify its counted errors early.",
                "task_log_apache",
            ],
            [
                "3. Validate finance-report requirements",
                f"Finance Report averaged {score('hermes', 'task_csv_finance_report'):.2f}%. Use the native criterion breakdown to verify all requested calculations and report sections; avoid treating successful tool execution as complete task accuracy.",
                "task_csv_finance",
            ],
        ],
        "openclaw": [
            [
                "1. Deliver a report before research expires",
                "All three IT Procurement attempts scored zero at the native five-minute deadline. All Meeting Index and Council Votes attempts also lacked their requested reports. Save an evidence-backed partial deliverable before further searching.",
                "task_it_procurement",
            ],
            [
                "2. Honor writing constraints more consistently",
                f"Humanizer averaged {score('openclaw', 'task_humanizer'):.2f}%, below its other writing tasks. Verify the task's requested style and content against the native rubric before finalizing.",
                "task_humanizer",
            ],
            [
                "3. Avoid unsolicited repository work",
                "Blog and Workflow judge notes identify unnecessary repository inspection and commit attempts, including recovery from missing Git identity. Keep the native workflow focused on the requested deliverables.",
                "task_workflow",
            ],
        ],
    }
    data["repairs"] = json.loads(
        (campaign / "pinch-contextual-fairness-review.json").read_text()
    )["resolved_findings"]
    return data, selection, final_date


def freeze(campaign, workspace, target, data):
    """Hash original records in place and copy only small mutable metadata/source."""
    if target.exists():
        raise RuntimeError(
            "A frozen export already exists; use a distinct campaign identity"
        )
    target.mkdir()
    snapshots = target / "snapshots"
    snapshots.mkdir()
    for name in (
        "selection.json",
        "campaign.json",
        "runtime.json",
        "runtime-provenance.json",
        "mounts.json",
        "scheduling-policy.json",
        "pinch-runtime.json",
        "pinch-preparation.json",
        "pinch-fairness-audit.json",
        "pinch-fairness-review.json",
        "pinch-contextual-fairness-review.json",
        "pinch-complete.json",
        "environment-repair-audit.json",
        "diagnostic-policy.json",
        "grader-issues.json",
    ):
        if (campaign / name).exists():
            shutil.copy2(campaign / name, snapshots / name)
    shutil.copytree(
        workspace / "scripts/benchmarks",
        snapshots / "harness",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copytree(
        campaign / "pinch-runtime/benchmarks",
        snapshots / "runtime",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    save_json(target / "pinchbench-data.json", data)
    roots = [p for p in (campaign / "jobs").iterdir() if p.name.startswith("pinch--")]
    roots += [
        campaign / "tasks/pinch",
        campaign / "pinch-repairs-20261014",
        campaign / "pinch-repairs-20261015",
        snapshots,
    ]
    files = sorted(
        {
            p
            for root in roots
            for p in root.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts
        }
    )
    cache = {}
    inventory = []
    for path in files:
        stat = path.stat()
        key = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
        if key not in cache:
            cache[key] = digest(path.read_bytes())
        inventory.append(
            {
                "path": str(path.relative_to(campaign)),
                "bytes": stat.st_size,
                "sha256": cache[key],
            }
        )
    save_json(
        target / "evidence-manifest.json",
        {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "accepted_attempts": 288,
            "task_count": 32,
            "evidence_fingerprint": data["evidence_fingerprint"],
            "policy": "Raw originals hashed in place; only metadata and source copied. This inventory does not publish raw artifacts.",
            "files": inventory,
        },
    )
    save_json(
        target / "freeze-verification.json",
        {
            "passed": True,
            "files_verified": len(inventory),
            "manifest_sha256": digest((target / "evidence-manifest.json").read_bytes()),
            "large_artifacts_duplicated": False,
            "accepted_attempts": 288,
        },
    )
    save_json(
        campaign / "pinch-evidence-freeze.json",
        {
            "status": "frozen",
            "directory": str(target.relative_to(campaign)),
            "evidence_fingerprint": data["evidence_fingerprint"],
            "manifest_sha256": digest((target / "evidence-manifest.json").read_bytes()),
        },
    )


def publish_wiring(campaign, repository, run, archive):
    """Preserve small reproducibility inputs without vendoring runtime binaries."""
    sanitizer = Sanitizer(archive)
    mapping = []
    dependencies = campaign / "pinch-dependencies"
    sources = [
        (dependencies / name, run / "harness/dependencies" / name)
        for name in (
            "package.json",
            "pnpm-lock.yaml",
            "pnpm-workspace.yaml",
            "gws-portable.json",
            "python/dependency-manifest.json",
            "bin/gws",
            "pinch-scripts/lib_fws.py",
        )
    ]
    sources += [
        (
            campaign / "preflight/pinch-session-spec/pinch-native-session-smoke.md",
            run / "harness/preflight/pinch-native-session-smoke.md",
        ),
        (
            campaign / "preflight/pinch-prerequisites/mock-environment.json",
            run / "harness/preflight/mock-environment.json",
        ),
    ]
    sources += [
        (source, run / "harness/tasks" / source.name)
        for source in sorted((campaign / "tasks/pinch").glob("*/tests/tasks/*.md"))
    ]
    for source, destination in sources:
        export_file(source, destination, sanitizer, mapping)
        destination.chmod(source.stat().st_mode & 0o777)
    license_source = campaign.parent / "environments/pinchbench/LICENSE"
    export_file(
        license_source,
        run / "provenance/licenses/PinchBench-LICENSE",
        sanitizer,
        mapping,
    )
    manifest_path = archive / "evidence-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    existing = {row["path"] for row in manifest["files"]}
    for source in [*(row[0] for row in sources), dependencies / "bin/gws.native"]:
        relative = str(source.relative_to(campaign))
        if relative not in existing:
            manifest["files"].append(
                {
                    "path": relative,
                    "bytes": source.stat().st_size,
                    "sha256": digest(source.read_bytes()),
                }
            )
    manifest["derived_metrics_sha256"] = digest(
        (
            archive / manifest.get("derived_metrics_file", "pinchbench-data.json")
        ).read_bytes()
    )
    save_json(manifest_path, manifest)
    verification = json.loads((archive / "freeze-verification.json").read_text())
    verification.update(
        files_verified=len(manifest["files"]),
        manifest_sha256=digest(manifest_path.read_bytes()),
    )
    save_json(archive / "freeze-verification.json", verification)
    for source, destination, compress in (
        (manifest_path, run / "provenance/private-evidence-inventory.json", True),
        (
            archive / "freeze-verification.json",
            run / "provenance/freeze-verification.json",
            False,
        ),
    ):
        export_file(source, destination, sanitizer, mapping, compress=compress)
    freeze_path = campaign / "pinch-evidence-freeze.json"
    frozen = json.loads(freeze_path.read_text())
    frozen["manifest_sha256"] = verification["manifest_sha256"]
    save_json(freeze_path, frozen)
    publication_path = run / "provenance/publication-map.json"
    publication = json.loads(publication_path.read_text())
    by_destination = {row["destination"]: row for row in publication["files"]}
    for row in mapping:
        row["destination"] = str(Path(row["destination"]).relative_to(repository))
        by_destination[row["destination"]] = row
    publication["files"] = list(by_destination.values())
    publication["transformations"] = dict(
        collections.Counter(publication["transformations"]) + sanitizer.counts
    )
    publication["private_manifest_sha256"] = verification["manifest_sha256"]
    save_json(publication_path, publication)


def publish(workspace, campaign, repository, archive=None):
    """Export sanitized attempts, metrics, pinned methods and report presentation."""
    if archive:
        manifest = json.loads((archive / "evidence-manifest.json").read_text())
        for entry in manifest["files"]:
            if digest((campaign / entry["path"]).read_bytes()) != entry["sha256"]:
                raise RuntimeError("Frozen evidence changed: " + entry["path"])
        data = json.loads(
            (
                archive / manifest.get("derived_metrics_file", "pinchbench-data.json")
            ).read_text()
        )
        selection = json.loads((archive / "snapshots/selection.json").read_text())
        date = max(
            json.loads((campaign / r["trial"] / "result.json").read_text())[
                "finished_at"
            ]
            for r in data["records"]
        )[:10]
    else:
        data, selection, date = derive(workspace, campaign, repository)
        archive = campaign / ("pinchbench-frozen-" + date.replace("-", ""))
        freeze(campaign, workspace, archive, data)
    run = repository / "pinchbench/runs" / date
    if (run / "metrics.json").exists():
        raise RuntimeError("Preserve existing published run folders")
    run.mkdir(parents=True, exist_ok=True)
    sanitizer = Sanitizer(archive)
    mapping = []
    public = sanitizer.transform(data)
    extra = [
        "agent/pinch-turns.json",
        "agent/worker-cwd.jsonl",
        "agent/gws-invocations.jsonl",
        "verifier/pinch_score.json",
    ]
    for raw, record in zip(data["records"], public["records"], strict=True):
        trial = campaign / raw["trial"]
        relative = Path("attempts") / record["agent"] / record["task"] / record["id"]
        record["source_trial"] = record["trial"]
        record["trial"] = str(relative)
        target = run / relative
        for name in [*RECORD_FILES, *extra]:
            if (trial / name).exists():
                export_file(
                    trial / name,
                    target / name,
                    sanitizer,
                    mapping,
                    compress=Path(name).suffix == ".jsonl",
                )
        receipt = (
            json.loads((trial / "verifier/artifact-regrade.json").read_text())
            if (trial / "verifier/artifact-regrade.json").exists()
            else None
        )
        agent = effective_input(campaign, trial, receipt)
        trace = export_file(
            agent / "pinch-transcript.jsonl",
            target / "agent/pinch-transcript.jsonl",
            sanitizer,
            mapping,
            compress=True,
        )
        record["public_transcript"] = str(trace.relative_to(run))
        record["public_transcript_note"] = (
            "Exact accepted native grader input after documented capture recovery; private reasoning omitted. CLI invocation observations are separate from model function calls."
        )
        for source in sorted((agent / "turns").rglob("*")):
            if source.is_file() and source.name in (
                "deadline.json",
                "hermes-session.json",
                "nested-tool-calls.jsonl",
            ):
                export_file(
                    source,
                    target / "agent/turns" / source.relative_to(agent / "turns"),
                    sanitizer,
                    mapping,
                    compress=source.suffix == ".jsonl",
                )
        for index, ledger in enumerate(raw["judge_recovery_attempts"]):
            source = campaign / ledger["requests_file"]
            for name in (
                "requests.jsonl",
                "inference-events.jsonl",
                "pinch_score.json",
                "reward.json",
                "judge-reliability.json",
            ):
                if (source.parent / name).exists():
                    export_file(
                        source.parent / name,
                        target / "verifier/recovery" / str(index + 1) / name,
                        sanitizer,
                        mapping,
                        compress=Path(name).suffix == ".jsonl",
                    )
        save_json(
            target / "verifier/effective-grade.json",
            {
                "accepted_score": record["score"],
                "native_criteria": record["native_grade"],
                "judge_notes": record["judge_notes"],
                "note": "Accepted native grade; original grader records and recovery ledgers remain separate.",
            },
        )
        save_json(
            target / "accepted-score.json",
            {k: record[k] for k in ("id", "agent", "task", "score", "diagnosis")},
        )
    save_json(run / "metrics.json", public)
    save_json(
        run / "accepted-attempts.json",
        [
            {k: r[k] for k in ("id", "agent", "task", "domain", "score", "trial")}
            for r in public["records"]
        ],
    )
    save_json(
        run / "tasks.json",
        {
            "revision": selection["dataset_revisions"]["pinch"],
            "selection": sanitizer.transform(selection["pinch"]),
            "scored_tasks": [
                {"id": t["id"], "domain": t["domain"]} for t in public["tasks"]
            ],
        },
    )
    save_json(run / "conditions.json", public["conditions"])
    save_json(run / "metrics-plan.json", public["metrics_plan"])
    for source in sorted((archive / "snapshots").glob("*.json")):
        export_file(
            source,
            run / "provenance/campaign-snapshots" / source.name,
            sanitizer,
            mapping,
        )
    for name in ("freeze-verification.json", "evidence-manifest.json"):
        export_file(
            archive / name,
            run
            / "provenance"
            / (
                "private-evidence-inventory.json"
                if name == "evidence-manifest.json"
                else name
            ),
            sanitizer,
            mapping,
            compress=name == "evidence-manifest.json",
        )
    export_file(
        campaign / "wildclawbench-frozen-20261013/frozen-agent-sources.json",
        run / "provenance/frozen-agent-sources.json",
        sanitizer,
        mapping,
    )
    for folder in ("pinch-repairs-20261014", "pinch-repairs-20261015", "preflight"):
        for source in sorted((campaign / folder).glob("*.json")):
            export_file(
                source, run / "provenance" / folder / source.name, sanitizer, mapping
            )
    source_map = []
    for folder, destination in (
        (archive / "snapshots/harness", run / "harness/scripts/benchmarks"),
        (archive / "snapshots/runtime", run / "harness/runtime/benchmarks"),
    ):
        for source in sorted(folder.iterdir()):
            if not source.is_file():
                continue
            before = source.read_bytes()
            payload = re.sub(
                rb"/home/[^/]+/\.hermes/tools/[^/]+/bin/python3",
                b"/opt/hermes-python/bin/python3",
                before,
            )
            target = destination / source.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(sanitizer.text(payload.decode()).encode())
            source_map.append(
                {
                    "path": str(target.relative_to(run)),
                    "original_sha256": digest(before),
                    "packaged_sha256": digest(target.read_bytes()),
                    "packaging": "Portable Hermes fallback and explicit private-path templates; native loop/rubric behavior unchanged",
                }
            )
    save_json(run / "provenance/harness-source-map.json", source_map)
    save_json(
        run / "provenance/publication-map.json",
        {
            "policy": "Credentials, private reasoning, owner paths and inline payloads omitted; numeric evidence retained.",
            "transformations": dict(sanitizer.counts),
            "files": [
                {
                    **r,
                    "destination": str(Path(r["destination"]).relative_to(repository)),
                }
                for r in mapping
            ],
        },
    )
    (run / "report").mkdir()
    template = (repository / "harness/reporting/pinch_report.html").read_text()
    palette = json.loads(
        (repository / "harness/reporting/agent-colors.json").read_text()
    )
    template = template.replace(
        "__AGENT_COLORS__", json.dumps({a: palette[a] for a in AGENTS})
    )
    template = template.replace(
        "__REPORT_DATA__",
        json.dumps(public, ensure_ascii=False).replace("<", "\\u003c"),
    )
    (run / "report/index.html").write_text(template)
    publish_wiring(campaign, repository, run, archive)
    print(
        json.dumps(
            {
                "run": str(run),
                "archive": str(archive),
                "attempts": len(public["records"]),
                "ranking": [
                    [a, public["agents"][a]["score"]] for a in public["ranking"]
                ],
                "sanitization": dict(sanitizer.counts),
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument(
        "--archive",
        type=Path,
        help="Reuse a verified private freeze after an interrupted export",
    )
    args = parser.parse_args()
    publish(
        args.workspace.resolve(),
        args.campaign.resolve(),
        args.repository.resolve(),
        args.archive.resolve() if args.archive else None,
    )
