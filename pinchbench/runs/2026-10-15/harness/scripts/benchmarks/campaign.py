"""Run a frozen native-agent campaign in sequential benchmark phases."""

import argparse
import asyncio
import collections
import importlib
import json
import os
import shutil
import signal
from datetime import datetime, timezone
from pathlib import Path

import toml

from scripts.benchmarks import balanced, metrics, task_integrity

ROOT = Path(__file__).resolve().parents[2]
ORIGINAL_REPORT = balanced.report


class OwnerPause(Exception):
    """Stop at a cohort boundary while preserving accepted evidence."""


def report(output, selection):
    """Attach locally derived measurements to the matched-cohort score report."""
    value = ORIGINAL_REPORT(output, selection)
    for row in value["trials"]:
        trial = Path(row["trial"])
        path = trial / "metrics.json"
        row["metrics"] = (
            json.loads(path.read_text())
            if path.exists()
            else metrics.trial_metrics(trial)
        )
    value["interpretation"] = (
        "Frozen local subsets, three attempts per agent and task. Rank matched tasks only, "
        "with equal domain weights. Report native benchmark submetrics separately from "
        "tool schema validity; semantic correctness requires task-specific evidence. "
        "Direct ChatGPT subscription, GPT-6.1 Sol/high, Fast requested as priority. "
        "Returned tiers are recorded without claiming confirmed served Fast. "
        "The shared judge adapter proportionally fits oversized images within "
        "the subscription endpoint's 30,000-patch input limit and audits transformations. "
        "Candidate tokens, latency and inference counts exclude judging."
    )
    plan = json.loads((output / "campaign.json").read_text())
    value["modified_grader_tasks"] = [
        row["new_task"]
        for row in plan.get("approved_task_replacements", [])
        if row.get("modified_grader")
    ]
    if value["modified_grader_tasks"]:
        value["interpretation"] += (
            " The two API-leakage Safety tasks use documented immutable-initial-fixture "
            "validation repairs, identically for every agent. This is a modified local "
            "benchmark subset, rather than an unmodified official leaderboard score."
        )
    balanced.save(output / "results.json", value)
    return value


def rotate(items):
    """Spread early task coverage across domains without consulting outcomes."""
    domains = collections.defaultdict(list)
    for item in items:
        domains[item["domain"]].append(item)
    return [
        domains[domain][index]
        for index in range(max(map(len, domains.values())))
        for domain in sorted(domains)
        if index < len(domains[domain])
    ]


async def quota(output, state):
    """Wait between task groups when the subscription reserve is unavailable."""
    while True:
        if (output / "pause-request.json").exists():
            raise OwnerPause("Paused by owner; completed evidence is preserved")
        limits = await asyncio.to_thread(balanced.rate_limits)
        balanced.save(output / "rate-limits.json", limits)
        if balanced.quota_available(limits):
            return
        state["status"] = "waiting for subscription allowance"
        balanced.save(output / "status.json", state)
        await asyncio.sleep(30)


async def smoke(output, selection, baseline):
    """Exercise native tool execution and stream evidence outside scored coverage."""
    item = {"name": "connection-smoke", "domain": "preflight"}
    target = output / "tasks/wild" / item["name"]
    (target / "environment").mkdir(parents=True, exist_ok=True)
    (target / "tests").mkdir(exist_ok=True)
    (target / "instruction.md").write_text(
        "Create /tmp_workspace/subscription-smoke.txt containing exactly OK. "
        "Use your tools to create and read the file, then finish."
    )
    (target / "tests/test.sh").write_text(
        "#!/bin/bash\nset -eu\n"
        'if [ "$(cat /tmp_workspace/subscription-smoke.txt)" = OK ]; then\n'
        "  echo '{\"overall_score\":1}' > /logs/verifier/reward.json\n"
        "else\n  echo '{\"overall_score\":0}' > /logs/verifier/reward.json\nfi\n"
    )
    (target / "task.toml").write_text(
        toml.dumps(
            {
                "version": "1.0",
                "agent": {"timeout_sec": 600},
                "verifier": {"timeout_sec": 120},
                "environment": {
                    "docker_image": balanced.benchmark.DEFAULT_IMAGE,
                    "build_timeout_sec": 600,
                    "cpus": 2,
                    "memory_mb": 4_096,
                    "storage_mb": 10_240,
                },
            }
        )
    )
    results = await balanced.execute(
        output,
        selection,
        "wild",
        item,
        "smoke",
        baseline,
        agents=balanced.benchmark.AGENTS,
        attempts=1,
        release_images=False,
    )
    checks = []
    for result in results:
        path = output / "jobs/wild--connection-smoke/smoke" / result["trial_name"]
        requests = metrics.lines(path / "agent/requests.jsonl")
        verified = bool(requests) and all(
            row.get("model") == "gpt-6.1-sol"
            and (row.get("reasoning") or {}).get("effort") == "high"
            and row.get("serviceTier") == "priority"
            and row.get("authMode") == "chatgpt_subscription"
            and row.get("endpoint", "").startswith(
                "https://chatgpt.com/backend-api/codex/"
            )
            for row in requests
        )
        checks.append(
            {
                "agent": result["agent_info"]["name"],
                "settings_verified": verified,
                "exception": result.get("exception_info"),
                "rewards": (result.get("verifier_result") or {}).get("rewards"),
                "metrics": metrics.trial_metrics(path),
            }
        )
    balanced.save(output / "smoke.json", checks)
    if len(checks) != 3 or any(
        not row["settings_verified"]
        or row["exception"]
        or (row["rewards"] or {}).get("overall_score") != 1
        for row in checks
    ):
        raise RuntimeError("Native smoke checks failed; scored trials were not started")


async def run(output, smoke_only=False):
    """Keep versions, phase order, task budgets and completed attempts stable."""
    plan = json.loads((output / "campaign.json").read_text())
    selection = json.loads((output / "selection.json").read_text())
    if balanced.digest(output / "selection.json") != plan["frozen_selection_sha256"]:
        raise RuntimeError("Frozen campaign selection changed")
    balanced.report = report
    balanced.benchmark.configure_runtime(output)
    for key in balanced.PROXY_NAMES:
        os.environ.pop(key, None)
    state = {
        "pid": os.getpid(),
        "status": "preflight",
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    balanced.save(output / "status.json", state)
    current = asyncio.current_task()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, current.cancel)
    baseline = {row["Id"] for row in balanced.docker_objects("image")}
    try:
        if (output / "pause-request.json").exists():
            raise OwnerPause("Paused by owner; explicit resume required")
        if not (output / "smoke.json").exists():
            await smoke(output, selection, baseline)
        else:
            checks = json.loads((output / "smoke.json").read_text())
            if any(row["exception"] or not row["settings_verified"] for row in checks):
                raise RuntimeError(
                    "Saved smoke failure requires a configuration correction"
                )
        if smoke_only:
            state["status"] = "native smoke checks passed"
            return
        for suite in plan["phase_order"]:
            if suite == "pinch":
                module = importlib.import_module("scripts.benchmarks.pinch")
                await module.run(output, selection, state)
                continue
            if suite == "terminal":
                from scripts.benchmarks.fairness_audit import require_review

                require_review(output, selection, "pinch")
            repair_plan_path = output / "environment-repair-plan.json"
            repair_tasks = set()
            agent_repairs = {}
            if repair_plan_path.exists():
                repair_plan = json.loads(repair_plan_path.read_text())
                if not repair_plan.get("approved"):
                    raise RuntimeError("Environment repair plan lacks approval")
                repair_tasks = set(repair_plan["ready_rerun_tasks"])
                agent_repairs = repair_plan.get("agent_rerun_tasks", {})
                repair_tasks.update(agent_repairs)
            items = rotate(selection[suite])
            items.sort(key=lambda item: item["name"] not in repair_tasks)
            held_tasks = []
            for item in items:
                group = output / "jobs" / f"{suite}--{item['name']}"
                mode = "native-repair" if item["name"] in repair_tasks else "native"
                issues_path = output / "grader-issues.json"
                issues = (
                    json.loads(issues_path.read_text()) if issues_path.exists() else []
                )
                excluded = any(
                    issue["suite"] == suite
                    and issue["task"] == item["name"]
                    and issue.get("exclude_from_ranking")
                    for issue in issues
                )
                if excluded and mode != "native-repair":
                    held_tasks.append(item["name"])
                    continue
                completion = group / (
                    "repair-complete.json"
                    if mode == "native-repair"
                    else "complete.json"
                )
                if completion.exists():
                    continue
                if (group / "validity-hold.json").exists():
                    held_tasks.append(item["name"])
                    continue
                if mode == "native-repair":
                    task_integrity.prepare_repair(
                        output, suite, item["name"], agent_repairs.get(item["name"])
                    )
                state.update(status="preparing task", suite=suite, task=item["name"])
                balanced.save(output / "status.json", state)
                await quota(output, state)
                await asyncio.to_thread(
                    balanced.download,
                    output,
                    suite,
                    item["name"],
                    selection["dataset_revisions"][suite],
                )
                if suite == "terminal":
                    reference = await balanced.execute(
                        output,
                        selection,
                        suite,
                        item,
                        "oracle",
                        baseline,
                        release_images=False,
                    )
                    if len(reference) != balanced.ORACLE_ATTEMPTS or any(
                        row.get("exception_info")
                        or (
                            (row.get("verifier_result") or {}).get("rewards") or {}
                        ).get("reward")
                        != 1
                        for row in reference
                    ):
                        raise RuntimeError(
                            "Reference validation failed; candidate runs held"
                        )
                state["status"] = "agent execution"
                balanced.save(output / "status.json", state)
                candidates = balanced.benchmark.AGENTS
                if item["name"] in agent_repairs:
                    candidates = {
                        agent: balanced.benchmark.AGENTS[agent]
                        for agent in agent_repairs[item["name"]]
                    }
                results = await balanced.execute(
                    output,
                    selection,
                    suite,
                    item,
                    mode,
                    baseline,
                    agents=candidates,
                    attempts=3,
                    release_images=False,
                )
                if len(results) != 3 * len(candidates):
                    raise RuntimeError(
                        "Task group did not produce its audited attempts"
                    )
                reviews = [
                    task_integrity.inspect_trial(
                        output, group / mode / result["trial_name"]
                    )
                    for result in results
                ]
                if any(review["requires_review"] for review in reviews):
                    balanced.save(group / "validity-hold.json", {"reviews": reviews})
                    held_tasks.append(item["name"])
                    await asyncio.to_thread(
                        balanced.cleanup_job, output, group / mode, True, True
                    )
                    continue
                if mode == "native-repair":
                    task_integrity.accept_repair(output, suite, item["name"], results)
                snapshot = report(output, selection)
                rows = [
                    row
                    for row in snapshot["trials"]
                    if row["suite"] == suite and row["task"] == item["name"]
                ]
                if len(rows) != 9 or any(row["score"] is None for row in rows):
                    balanced.save(
                        group / "validity-hold.json",
                        {"reason": "Matched scoring coverage requires review"},
                    )
                    held_tasks.append(item["name"])
                    await asyncio.to_thread(
                        balanced.cleanup_job, output, group / mode, True, True
                    )
                    continue
                balanced.save(
                    completion,
                    {
                        "attempts": 9,
                        "executed_attempts": len(results),
                        "retained_attempts": 9 - len(results),
                        "finished_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
                for completed_mode in ("oracle", mode):
                    await asyncio.to_thread(
                        balanced.cleanup_job, output, group / completed_mode, True, True
                    )
                for name in ("environment", "solution"):
                    shutil.rmtree(
                        output / "tasks" / suite / item["name"] / name,
                        ignore_errors=True,
                    )
            snapshot = report(output, selection)
            if held_tasks:
                state["held_tasks"] = held_tasks
            if set(snapshot["summaries"][suite]["matched_tasks"]) != {
                item["name"] for item in selection[suite]
            }:
                raise RuntimeError(
                    "Suite contains tasks without valid matched coverage; review before advancing"
                )
            balanced.save(
                output / f"{suite}-complete.json",
                {"finished_at": datetime.now(timezone.utc).isoformat()},
            )
        state["status"] = "finished"
    except OwnerPause as error:
        state.update(status="paused", reason=str(error))
    except asyncio.CancelledError:
        state["status"] = "stopped with completed evidence preserved"
        raise
    except Exception as error:
        state.update(status="held", reason=str(error))
        raise
    finally:
        balanced.save(output / "status.json", state)


def main():
    """Select the frozen campaign without reusing results from prior versions."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--smoke-only", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.output.resolve(), args.smoke_only))


if __name__ == "__main__":
    main()
