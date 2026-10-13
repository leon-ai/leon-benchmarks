"""Catch up a replacement candidate while another benchmark lane stays live."""

import argparse
import asyncio
import importlib
import json
import os
import shutil
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
balanced = importlib.import_module("scripts.benchmarks.balanced")
scheduling = importlib.import_module("scripts.benchmarks.scheduling")
image_loader = importlib.import_module("scripts.benchmarks.load-wild-image")
shared = importlib.import_module("scripts.benchmarks.shared_admission")
balanced.save = shared.save_unique


async def handoff(output, service, state):
    """Upgrade the other lane only between task groups, preserving in-flight work."""
    while True:
        status = json.loads((output / "status.json").read_text())
        if status.get("status") in (
            "stopped",
            "finished",
            "execution finished with held tasks",
        ) or not shared.alive(status.get("pid")):
            return
        pool_path = output / "admission-pool.json"
        pool = json.loads(pool_path.read_text()) if pool_path.exists() else {}
        if str(status.get("pid")) in pool.get("participants", {}):
            return
        if status.get("status") == "preparing task":
            pid = status["pid"]
            # Freeze only a preparation phase so the boundary check cannot race
            # with new trial creation. No agent or verifier budget is running.
            os.kill(pid, signal.SIGSTOP)
            try:
                frozen = json.loads((output / "status.json").read_text())
                interrupted = [
                    path
                    for path in (output / "jobs").glob("*/*/*/config.json")
                    if not (path.parent / "result.json").exists()
                ]
                if frozen != status or interrupted:
                    continue
                state["handoff"] = "safe task boundary"
                balanced.save(output / "catchup-status.json", state)
                os.kill(pid, signal.SIGTERM)
            finally:
                os.kill(pid, signal.SIGCONT)
            process = await asyncio.create_subprocess_exec(
                "systemctl", "--user", "restart", service
            )
            if await process.wait():
                raise RuntimeError("Terminal runner handoff failed")
            return
        if status.get("status") in ("finished", "execution finished with held tasks"):
            return
        await asyncio.sleep(0.05)


async def load_image(output, policy, state, status_file="catchup-status.json"):
    """Load one verified shared image without reserving inference slots."""
    pool = shared.SharedPool(output, policy)
    name = "wildclaw-image-transfer"
    if balanced.command(["docker", "image", "ls", "-q", image_loader.IMAGE]):
        identity = balanced.command(
            ["docker", "image", "inspect", image_loader.IMAGE, "--format", "{{.Id}}"]
        )
        if identity != image_loader.IDENTITY:
            raise RuntimeError("Shared WildClaw image identity changed")
        return identity
    while True:
        reason = pool.acquire(
            name,
            {"cpus": 1, "memory_mb": 512},
            scheduling.resources(output),
            kind="image_transfer",
            disk_bytes=image_loader.ARCHIVE_SIZE,
            workspace_bytes=policy["image_transfer_workspace_gib"] * scheduling.GIB,
        )
        state.update(
            status="waiting for image headroom" if reason else "loading official image",
            wait_reason=reason,
        )
        balanced.save(output / status_file, state)
        if reason is None:
            break
        await asyncio.sleep(policy["admission_poll_seconds"])
    loader = None
    try:
        with (output / "catchup-image-load.log").open("a") as log:
            loader = await asyncio.create_subprocess_exec(
                sys.executable,
                str(ROOT / "scripts/benchmarks/load-wild-image.py"),
                "--temporary-directory",
                str(output / "catchup-image-transfer"),
                stdout=log,
                stderr=log,
            )
            if await loader.wait():
                raise RuntimeError("Catch-up official image load failed")
        return image_loader.IDENTITY
    finally:
        if loader and loader.returncode is None:
            loader.terminate()
            await loader.wait()
        pool.release(name)


async def run_groups(items, limit, worker):
    """Bound task groups and let unrelated work finish if one group fails."""
    semaphore = asyncio.Semaphore(limit)

    async def bounded(item):
        async with semaphore:
            await worker(item)

    tasks = [asyncio.create_task(bounded(item)) for item in items]
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result


async def execute_group(output, selection, item, baseline, state):
    """Keep each task's native attempts, artifacts and cleanup isolated."""
    name = item["name"]
    group = output / "jobs" / f"wild--{name}"

    def stage(value):
        state["active_tasks"][name] = value
        balanced.save(output / "catchup-status.json", state)

    stage("preparing task")
    try:
        await asyncio.to_thread(
            balanced.download,
            output,
            "wild",
            name,
            selection["dataset_revisions"]["wild"],
        )
        stage("native runtime setup validation")
        setup = await balanced.execute(
            output, selection, "wild", item, "setup", baseline
        )
        if len(setup) != 3 or any(row.get("exception_info") for row in setup):
            balanced.save(
                group / "held.json",
                {"reason": "Native runtime setup failed; candidate runs not launched"},
            )
            return
        stage("agent execution")
        results = await balanced.execute(
            output, selection, "wild", item, "native", baseline
        )
        if len(results) != 3 or any(
            row["agent_info"]["name"] != "openclaw" for row in results
        ):
            raise RuntimeError("Catch-up coverage differs from three OpenClaw attempts")
        balanced.save(
            group / "complete.json",
            {
                "agent_runs": 3,
                "lane": "wild-openclaw-catchup",
                "results": {
                    row["trial_name"]: balanced.digest(
                        group / "native" / row["trial_name"] / "result.json"
                    )
                    for row in results
                },
            },
        )
        (group / "held.json").unlink(missing_ok=True)
    finally:
        for mode in ("setup", "native"):
            await asyncio.to_thread(
                balanced.cleanup_job, output, group / mode, True, True
            )
        if (group / "complete.json").exists() or (group / "held.json").exists():
            for folder in ("environment", "solution"):
                shutil.rmtree(
                    output / "tasks/wild" / name / folder,
                    ignore_errors=True,
                )
        state["active_tasks"].pop(name, None)
        balanced.save(output / "catchup-status.json", state)
        await asyncio.to_thread(balanced.report, output, selection)


async def run(output, service):
    """Reuse retained candidates and run only the replacement's missing groups."""
    selection = balanced.initialize(output)
    balanced.benchmark.configure_runtime(output)
    runtime_receipt = json.loads((output / "openclaw-runtime-files.json").read_text())
    for path, expected in runtime_receipt["files"].items():
        if balanced.digest(Path(path)) != expected:
            raise RuntimeError(f"Frozen OpenClaw runtime changed: {path}")
    for key in (*balanced.PROXY_NAMES, "BUILDX_BUILDER"):
        os.environ.pop(key, None)
    state = {
        "status": "starting",
        "pid": os.getpid(),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    balanced.save(output / "catchup-status.json", state)
    policy = json.loads((output / "scheduling-policy.json").read_text())
    selected = [item for item in selection["wild"] if item["retained"]]
    if len(selected) != 12:
        raise RuntimeError("Catch-up must cover the frozen twelve retained tasks")
    pending = []
    for item in selected:
        group = output / "jobs" / f"wild--{item['name']}"
        if (group / "complete.json").exists():
            continue
        candidates = balanced.pending_agents(selection, "wild", item["name"])
        if set(candidates) != {"openclaw"}:
            raise RuntimeError("Catch-up would replay a retained candidate")
        held = group / "held.json"
        if held.exists() and not json.loads(held.read_text()).get("catchup_owner"):
            continue
        balanced.save(
            held,
            {
                "reason": "Scheduled in parallel catch-up lane",
                "catchup_owner": "wild-openclaw-catchup",
                "agent": "openclaw",
            },
        )
        pending.append(item)
    loop = asyncio.get_running_loop()
    current = asyncio.current_task()
    loop.add_signal_handler(signal.SIGTERM, current.cancel)
    owned_image = False
    loaders = []
    try:
        if pending:
            existed = bool(
                balanced.command(["docker", "image", "ls", "-q", image_loader.IMAGE])
            )
            owned_image = not existed
            loaders = [
                asyncio.create_task(load_image(output, policy, state)),
                asyncio.create_task(handoff(output, service, state)),
            ]
            identity, _ = await asyncio.gather(*loaders)
            baseline = {row["Id"] for row in balanced.docker_objects("image")}
            baseline.add(identity)
            group_limit = policy.get("catchup_task_groups", 1)
            if not 1 <= group_limit <= policy["host_maximum_trials"] // 3:
                raise RuntimeError("Catch-up groups exceed the shared trial ceiling")
            state.update(
                status="parallel task execution",
                active_tasks={},
                task_group_limit=group_limit,
                candidate_trial_ceiling=group_limit * 3,
            )

            async def worker(item):
                await execute_group(output, selection, item, baseline, state)

            await run_groups(pending, group_limit, worker)
        state["status"] = "finished"
        # The catch-up cap is temporary; later task groups can use all host slots.
        policy = json.loads((output / "scheduling-policy.json").read_text())
        policy["maximum_trials"] = policy["host_maximum_trials"]
        balanced.save(output / "scheduling-policy.json", policy)
        provenance_path = output / "provenance.json"
        provenance = json.loads(provenance_path.read_text())
        provenance["frozen_files"][str(output / "scheduling-policy.json")] = (
            balanced.digest(output / "scheduling-policy.json")
        )
        provenance["scheduling_adjustments"].append(
            {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "reason": "Catch-up queue drained; restore the approved host concurrency ceiling for remaining task groups.",
                "maximum_trials": policy["maximum_trials"],
            }
        )
        balanced.save(provenance_path, provenance)
    except BaseException as error:
        state.update(
            status="stopped", error_type=type(error).__name__, error=str(error)
        )
        raise
    finally:
        for task in loaders:
            if not task.done():
                task.cancel()
        await asyncio.gather(*loaders, return_exceptions=True)
        if owned_image:
            balanced.command(["docker", "image", "rm", image_loader.IMAGE], check=False)
        loop.remove_signal_handler(signal.SIGTERM)
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        balanced.save(output / "catchup-status.json", state)
        await asyncio.to_thread(balanced.report, output, selection)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--terminal-service", required=True)
    args = parser.parse_args()
    asyncio.run(run(args.output.resolve(), args.terminal_service))
