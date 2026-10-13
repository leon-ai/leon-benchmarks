"""Replace audited Hermes lifecycle failures without replaying other candidates."""

import argparse
import asyncio
import fcntl
import importlib
import json
import os
import shutil
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

import toml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
balanced = importlib.import_module("scripts.benchmarks.balanced")
catchup = importlib.import_module("scripts.benchmarks.catchup")
shared = importlib.import_module("scripts.benchmarks.shared_admission")
image_loader = importlib.import_module("scripts.benchmarks.load-wild-image")
balanced.save = shared.save_unique
RECOVERY_MODE = "hermes-recovery"
LEASE_IMAGE = f"{image_loader.IMAGE}-hermes-recovery"
POLL_SECONDS = 5


def main_running():
    """Keep audited snapshots current while another benchmark lane is live."""
    return balanced.command(
        ["systemctl", "--user", "is-active", "leon-balanced-benchmark.service"],
        check=False,
    ) in {"active", "activating"}


def assign_replacements(output, issues, results):
    """Assign every replacement once using identifiers, never grades or outcomes."""
    originals = sorted(issues, key=lambda row: row["trial_name"])
    replacements = sorted(results, key=lambda row: row["trial_name"])
    if len(originals) != len(replacements):
        raise RuntimeError("Replacement coverage does not match the audited slots")
    with (output / "hermes-lifecycle-audit.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = output / "hermes-lifecycle-audit.json"
        audit = json.loads(path.read_text())
        by_name = {row["trial_name"]: row for row in audit["affected_trials"]}
        for original, replacement in zip(originals, replacements, strict=True):
            if replacement["agent_info"]["name"] != "hermes":
                raise RuntimeError("Recovery attempted to replace another candidate")
            row = by_name[original["trial_name"]]
            trial = Path(row["replacement_directory"]) / replacement["trial_name"]
            row.update(
                status="replaced_with_corrected_wrapper",
                replacement_trial=str(trial),
                replacement_result_sha256=balanced.digest(trial / "result.json"),
                replaced_at=datetime.now(timezone.utc).isoformat(),
            )
        audit["status"] = "approved_hermes_recovery_in_progress"
        balanced.save(path, audit)


async def execute_group(output, selection, item, issues, suite, baseline, state):
    """Reuse verified references and release only this group's owned resources."""
    name = item["name"]
    group = output / "jobs" / f"{suite}--{name}"
    directory = group / RECOVERY_MODE
    state["active_tasks"][name] = "preparing replacement"
    balanced.save(output / "hermes-recovery-status.json", state)
    builder = None
    try:
        await asyncio.to_thread(
            balanced.download,
            output,
            suite,
            name,
            selection["dataset_revisions"][suite],
        )
        if suite == "wild":
            # The alias holds the verified image while an independent lane
            # releases its own tag. Both tags refer to identical image layers.
            task_path = output / "tasks" / suite / name / "task.toml"
            config = toml.load(task_path)
            config["environment"]["docker_image"] = LEASE_IMAGE
            task_path.write_text(toml.dumps(config))
        else:
            builder = "leon-hermes-recovery-" + name
            balanced.command(
                [
                    "docker",
                    "buildx",
                    "create",
                    "--name",
                    builder,
                    "--driver",
                    "docker-container",
                    "--driver-opt",
                    "network=host",
                    "--driver-opt",
                    "default-load=true",
                ]
            )
            os.environ["BUILDX_BUILDER"] = builder
        state["active_tasks"][name] = "corrected Hermes execution"
        balanced.save(output / "hermes-recovery-status.json", state)
        results = await balanced.execute(
            output,
            selection,
            suite,
            item,
            RECOVERY_MODE,
            baseline,
            agents={"hermes": balanced.benchmark.AGENTS["hermes"]},
            attempts=len(issues),
        )
        for result in results:
            trajectory = directory / result["trial_name"] / "agent/trajectory.json"
            if trajectory.exists():
                steps = json.loads(trajectory.read_text()).get("steps", [])
                for step in steps:
                    calls = {
                        call["tool_call_id"]: call["function_name"]
                        for call in step.get("tool_calls", [])
                    }
                    for observation in (step.get("observation") or {}).get(
                        "results", []
                    ):
                        if (
                            calls.get(observation.get("source_call_id"))
                            != "delegate_task"
                        ):
                            continue
                        try:
                            content = json.loads(observation.get("content", ""))
                        except (json.JSONDecodeError, TypeError):
                            continue
                        if (
                            isinstance(content, dict)
                            and content.get("mode") == "background"
                        ):
                            raise RuntimeError(
                                "Corrected Hermes still used detached delegation"
                            )
        assign_replacements(output, issues, results)
        balanced.save(
            directory / "complete.json",
            {"replaced_attempts": len(issues), "agent": "hermes"},
        )
    finally:
        await asyncio.to_thread(balanced.cleanup_job, output, directory, True, True)
        if builder:
            balanced.command(["docker", "buildx", "rm", builder], check=False)
            os.environ.pop("BUILDX_BUILDER", None)
            # Failed builds can leave a content tag before any trial container
            # exists. Remove that task-owned image without touching baseline images.
            for folder in ("environment", "tests"):
                context = output / "tasks" / suite / name / folder
                if context.is_dir():
                    tag = "hb__" + balanced.environment_content_hash(
                        context, docker_image=None
                    )
                    identity = balanced.command(
                        ["docker", "image", "inspect", tag, "--format", "{{.Id}}"],
                        check=False,
                    )
                    if identity and identity not in baseline:
                        balanced.command(["docker", "image", "rm", tag], check=False)
        if (directory / "complete.json").exists():
            for folder in ("environment", "solution"):
                shutil.rmtree(
                    output / "tasks" / suite / name / folder, ignore_errors=True
                )
        state["active_tasks"].pop(name, None)
        balanced.save(output / "hermes-recovery-status.json", state)
        await asyncio.to_thread(balanced.report, output, selection)


async def refresh_reports(output, selection):
    """Publish lifecycle exclusions while independent lanes append new results."""
    while True:
        await asyncio.to_thread(balanced.report, output, selection)
        await asyncio.sleep(POLL_SECONDS)


async def run(output, suite):
    """Resume an explicitly approved recovery queue with shared host admission."""
    selection = balanced.initialize(output)
    audit = json.loads((output / "hermes-lifecycle-audit.json").read_text())
    if audit.get("recovery_approval", {}).get("status") != "approved":
        raise RuntimeError(
            "Saved Hermes attempts require explicit recovery authorization"
        )
    for row in audit["affected_trials"]:
        if (
            balanced.digest(Path(row["trial"]) / "result.json")
            != row["original_result_sha256"]
        ):
            raise RuntimeError("Original Hermes evidence changed")
    wrapper = output / "runtime/benchmarks/run-hermes.py"
    if balanced.digest(wrapper) != audit["corrected_wrapper_sha256"]:
        raise RuntimeError("Corrected Hermes wrapper changed")
    balanced.benchmark.configure_runtime(output)
    for key in (*balanced.PROXY_NAMES, "BUILDX_BUILDER"):
        os.environ.pop(key, None)
    state = {
        "pid": os.getpid(),
        "suite": suite,
        "status": "starting",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "active_tasks": {},
    }
    balanced.save(output / "hermes-recovery-status.json", state)
    pending = {}
    for row in audit["affected_trials"]:
        if row["suite"] == suite and not row.get("replacement_trial"):
            pending.setdefault(row["task"], []).append(row)
    items = [item for item in selection[suite] if item["name"] in pending]
    loop = asyncio.get_running_loop()
    current = asyncio.current_task()
    loop.add_signal_handler(signal.SIGTERM, current.cancel)
    reporter = asyncio.create_task(refresh_reports(output, selection))
    leased = False
    try:
        if suite == "wild" and items:
            policy = json.loads((output / "scheduling-policy.json").read_text())
            await catchup.load_image(
                output, policy, state, status_file="hermes-recovery-status.json"
            )
            balanced.command(
                ["docker", "image", "tag", image_loader.IMAGE, LEASE_IMAGE]
            )
            leased = True
        baseline = {row["Id"] for row in balanced.docker_objects("image")}
        group_limit = 3 if suite == "wild" else 1
        state.update(status="running replacements", task_group_limit=group_limit)
        balanced.save(output / "hermes-recovery-status.json", state)

        async def worker(item):
            await execute_group(
                output, selection, item, pending[item["name"]], suite, baseline, state
            )

        await catchup.run_groups(items, group_limit, worker)
        if leased:
            balanced.command(["docker", "image", "rm", LEASE_IMAGE], check=False)
            leased = False
            if not main_running():
                balanced.command(
                    ["docker", "image", "rm", image_loader.IMAGE], check=False
                )
        state["status"] = "replacements finished; maintaining audited reports"
        balanced.save(output / "hermes-recovery-status.json", state)
        while main_running():
            await asyncio.sleep(POLL_SECONDS)
        state["status"] = "finished"
    except BaseException as error:
        state.update(
            status="stopped", error_type=type(error).__name__, error=str(error)
        )
        raise
    finally:
        reporter.cancel()
        await asyncio.gather(reporter, return_exceptions=True)
        if leased:
            balanced.command(["docker", "image", "rm", LEASE_IMAGE], check=False)
            if not main_running():
                balanced.command(
                    ["docker", "image", "rm", image_loader.IMAGE], check=False
                )
        balanced.save(output / "hermes-recovery-status.json", state)
        await asyncio.to_thread(balanced.report, output, selection)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--suite", choices=("wild", "terminal"), default="wild")
    args = parser.parse_args()
    asyncio.run(run(args.output.resolve(), args.suite))
