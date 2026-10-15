"""Run an authorized Leon revision diagnostic without mixing candidate versions."""

import argparse
import asyncio
import fcntl
import importlib
import json
import os
import shutil
import signal
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

import toml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
balanced = importlib.import_module("scripts.benchmarks.balanced")
catchup = importlib.import_module("scripts.benchmarks.catchup")
shared = importlib.import_module("scripts.benchmarks.shared_admission")
scheduling = importlib.import_module("scripts.benchmarks.scheduling")
image_loader = importlib.import_module("scripts.benchmarks.load-wild-image")
balanced.save = shared.save_unique
ORIGINAL_ADMISSION = scheduling.Admission
STATUS_FILE = "variant-status.json"


def variant_report(output, selection):
    """Keep task diagnostics separate from the frozen comparison's ranking."""
    with (output / "report.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return build_variant_report(output, selection)


def build_variant_report(output, selection):
    """Audit subscription settings and retain every diagnostic trial outcome."""
    plan = json.loads((output / "variant-plan.json").read_text())
    baseline = json.loads((output / "comparison-baseline.json").read_text())
    rows = []
    for path in sorted((output / "jobs").glob("*/native/*/result.json")):
        result = json.loads(path.read_text())
        requests_path = path.parent / "agent/requests.jsonl"
        requests = (
            [json.loads(line) for line in requests_path.read_text().splitlines()]
            if requests_path.exists()
            else []
        )
        settings = (
            bool(requests)
            and all(
                request.get("model")
                == balanced.benchmark.MANIFEST["model"].split("/")[1]
                and (request.get("reasoning") or {}).get("effort")
                == balanced.benchmark.MANIFEST["reasoning"]
                and request.get("serviceTier")
                == balanced.benchmark.MANIFEST["service_tier"]
                and request.get("authMode") == "chatgpt_subscription"
                and request.get("endpoint", "").startswith(
                    "https://chatgpt.com/backend-api/codex/"
                )
                for request in requests
            )
            and any(200 <= request.get("status", 0) < 300 for request in requests)
        )
        grade_path = path.parent / "verifier/wildclaw_score.json"
        grade = json.loads(grade_path.read_text()) if grade_path.exists() else {}
        issues = {
            key: value
            for key, value in grade.items()
            if (key in balanced.benchmark.GRADER_ERROR_KEYS and value)
            or (
                key.endswith("judge_method")
                and value in balanced.benchmark.FALLBACK_JUDGE_METHODS
            )
        }
        exception = result.get("exception_info")
        scored = (
            not exception
            or exception["exception_type"] in balanced.benchmark.SCORED_AGENT_EXCEPTIONS
            or (
                exception["exception_type"]
                == balanced.benchmark.RETURNED_AGENT_EXCEPTION
                and (path.parent / "agent/leon-result.json").exists()
            )
        )
        score = ((result.get("verifier_result") or {}).get("rewards") or {}).get(
            "overall_score"
        )
        rows.append(
            {
                "suite": "wild",
                "task": path.parents[2].name.removeprefix("wild--"),
                "agent": result["agent_info"]["name"],
                "trial": str(path.parent),
                "score": score if settings and not issues and scored else None,
                "raw_score": score,
                "settings_verified": settings,
                "grader_error": issues,
                "exception": exception,
                "candidate_commit": plan["leon_commit"],
            }
        )
    if len(rows) > plan["total_attempts"] or any(
        row["agent"] != "leon" or row["task"] not in plan["tasks"] for row in rows
    ):
        raise RuntimeError("Diagnostic contains attempts outside the authorized scope")
    comparisons = {}
    for name in plan["tasks"]:
        task_rows = [row for row in rows if row["task"] == name]
        scores = [row["score"] for row in task_rows if row["score"] is not None]
        comparisons[name] = {
            "saved_attempts": len(task_rows),
            "scored_attempts": len(scores),
            "develop_scores": scores,
            "develop_mean": statistics.mean(scores) if len(scores) == 3 else None,
            "previous_means": {
                agent: statistics.mean(
                    row["score"]
                    for row in baseline["trials"]
                    if row["task"] == name and row["agent"] == agent
                )
                for agent in balanced.benchmark.AGENTS
            },
        }
    report = dict(
        updated_at=datetime.now(timezone.utc).isoformat(),
        completed_agent_runs=len(rows),
        total_agent_runs=plan["total_attempts"],
        remaining_agent_runs=plan["total_attempts"] - len(rows),
        comparable_scored_runs=sum(row["score"] is not None for row in rows),
        trials=rows,
        candidate_commit=plan["leon_commit"],
        comparisons=comparisons,
        summaries={},
        interpretation=(
            "Owner-selected two-task Leon revision diagnostic, three attempts each. "
            "Original results and the frozen overall ranking remain separate. "
            "All outcomes are retained; no outcome-based reruns. "
            "Same subscription model, reasoning, requested tier, judge and task budgets."
        ),
    )
    for name in ("results.json", "validated-results.json"):
        balanced.save(output / name, report)
    return report


async def run(output):
    """Execute only the saved authorized coverage and clean its owned resources."""
    plan = json.loads((output / "variant-plan.json").read_text())
    if plan["status"] != "authorized":
        raise RuntimeError("A saved owner-authorized revision plan is required")
    for path, expected in plan["baseline_result_sha256"].items():
        if balanced.digest(Path(path)) != expected:
            raise RuntimeError("Original comparison evidence changed")
    for path, expected in plan["frozen_files"].items():
        if balanced.digest(Path(path)) != expected:
            raise RuntimeError("Frozen diagnostic source or configuration changed")
    selection = json.loads((output / "selection.json").read_text())
    pool_directory = Path(plan["shared_admission_directory"])
    policy = json.loads((output / "scheduling-policy.json").read_text())

    class VariantAdmission(ORIGINAL_ADMISSION):
        """Share the existing host ceiling while retaining separate job evidence."""

        def __init__(
            self, directory, output, task, policy, read_quota, quota_available, ceiling
        ):
            super().__init__(
                directory,
                output,
                task,
                {**policy, "shared_admission": False},
                read_quota,
                quota_available,
                ceiling,
            )
            self.policy = policy
            self.pool = shared.SharedPool(pool_directory, policy)

    scheduling.Admission = VariantAdmission
    balanced.report = variant_report
    balanced.benchmark.configure_runtime(output)
    for key in (*balanced.PROXY_NAMES, "BUILDX_BUILDER"):
        os.environ.pop(key, None)
    state = {
        "pid": os.getpid(),
        "status": "starting diagnostic",
        "commit": plan["leon_commit"],
        "started_at": datetime.now(timezone.utc).isoformat(),
        "active_tasks": {},
        "planned_attempts": plan["total_attempts"],
    }
    balanced.save(output / STATUS_FILE, state)
    current = asyncio.current_task()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, current.cancel)
    image = f"{image_loader.IMAGE}-leon-{plan['leon_commit'][:8]}"
    leased = False
    try:
        await catchup.load_image(
            pool_directory,
            policy,
            state,
            status_file="leon-variant-image-status.json",
        )
        balanced.command(["docker", "image", "tag", image_loader.IMAGE, image])
        leased = True
        baseline = {row["Id"] for row in balanced.docker_objects("image")}

        async def execute_group(item):
            name = item["name"]
            directory = output / "jobs" / f"wild--{name}" / "native"
            state["active_tasks"][name] = "preparing immutable fixtures"
            balanced.save(output / STATUS_FILE, state)
            try:
                await asyncio.to_thread(
                    balanced.download,
                    output,
                    "wild",
                    name,
                    selection["dataset_revisions"]["wild"],
                )
                task = output / "tasks/wild" / name / "task.toml"
                config = toml.load(task)
                config["environment"]["docker_image"] = image
                config["environment"].setdefault("env", {})[
                    "LEON_BENCHMARK_VERSION"
                ] = plan["leon_commit"]
                task.write_text(toml.dumps(config))
                state["active_tasks"][name] = "Leon develop execution"
                state["status"] = "running diagnostic"
                balanced.save(output / STATUS_FILE, state)
                results = await balanced.execute(
                    output,
                    selection,
                    "wild",
                    item,
                    "native",
                    baseline,
                    agents={"leon": balanced.benchmark.AGENTS["leon"]},
                    attempts=plan["attempts_per_task"],
                )
                if len(results) != plan["attempts_per_task"]:
                    raise RuntimeError("Diagnostic coverage differs from the plan")
                balanced.save(
                    directory / "complete.json",
                    {"attempts": len(results), "commit": plan["leon_commit"]},
                )
            finally:
                await asyncio.to_thread(
                    balanced.cleanup_job, output, directory, True, True
                )
                if (directory / "complete.json").exists():
                    for folder in ("environment", "solution"):
                        shutil.rmtree(
                            output / "tasks/wild" / name / folder, ignore_errors=True
                        )
                state["active_tasks"].pop(name, None)
                balanced.save(output / STATUS_FILE, state)
                await asyncio.to_thread(variant_report, output, selection)

        await catchup.run_groups(selection["wild"], 2, execute_group)
        state["status"] = "diagnostic finished"
    except BaseException:
        state["status"] = "diagnostic stopped; saved evidence retained"
        raise
    finally:
        if leased:
            balanced.command(["docker", "image", "rm", image], check=False)
        balanced.save(output / STATUS_FILE, state)


def main():
    """Resume a diagnostic from its isolated, immutable execution plan."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(run(args.output.resolve()))


if __name__ == "__main__":
    main()
