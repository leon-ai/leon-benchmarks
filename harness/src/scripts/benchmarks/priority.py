"""Drain an active task before handing execution to a higher-priority suite."""

import argparse
import asyncio
import importlib
import json
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
balanced = importlib.import_module("scripts.benchmarks.balanced")
shared = importlib.import_module("scripts.benchmarks.shared_admission")
image_loader = importlib.import_module("scripts.benchmarks.load-wild-image")

POLL_SECONDS = 0.1
STAGE_POLL_SECONDS = 5
MAIN_SERVICE = "leon-balanced-benchmark.service"
CATCHUP_SERVICE = "leon-wild-openclaw-catchup.service"
HANDOFF_IMAGE = f"{image_loader.IMAGE}-priority-handoff"


def active(service):
    """Distinguish live execution from a saved status left after process exit."""
    return balanced.command(
        ["systemctl", "--user", "is-active", service], check=False
    ) in {"active", "activating"}


async def drain(output, service=MAIN_SERVICE, status_file="status.json"):
    """Stop only at a verified task boundary without canceling a saved trial."""
    while active(service):
        status = json.loads((output / status_file).read_text())
        group = output / "jobs" / f"{status.get('suite')}--{status.get('task')}"
        references_finished = (
            status.get("status") == "oracle validation"
            and len(list(group.glob("oracle/*/result.json")))
            == balanced.ORACLE_ATTEMPTS
        )
        if status.get("status") == "preparing task" or references_finished:
            pid = status["pid"]
            os.kill(pid, signal.SIGSTOP)
            stopped = False
            try:
                frozen = json.loads((output / status_file).read_text())
                unfinished = [
                    path
                    for path in group.glob("*/*/config.json")
                    if not (path.parent / "result.json").exists()
                ]
                if frozen == status and not unfinished:
                    os.kill(pid, signal.SIGTERM)
                    stopped = True
            finally:
                os.kill(pid, signal.SIGCONT)
            if stopped:
                balanced.command(["systemctl", "--user", "stop", service])
                return
        await asyncio.sleep(POLL_SECONDS)


async def reload_catchup(output):
    """Replace a sequential lane only after its active task has fully drained."""
    await drain(output, CATCHUP_SERVICE, "catchup-status.json")
    identity = balanced.command(
        ["docker", "image", "inspect", HANDOFF_IMAGE, "--format", "{{.Id}}"],
        check=False,
    )
    if identity != image_loader.IDENTITY:
        raise RuntimeError("Verified shared image must survive the catch-up handoff")
    balanced.command(["docker", "image", "tag", HANDOFF_IMAGE, image_loader.IMAGE])
    balanced.command(["systemctl", "--user", "start", CATCHUP_SERVICE])
    balanced.command(
        ["systemctl", "--user", "start", "leon-benchmark-priority.service"]
    )


async def run(output, resume_wild=False):
    """Preserve completed work while reserving the later suite for explicit setup."""
    state = {
        "pid": os.getpid(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "draining current task before WildClawBench priority",
    }

    def record(status):
        state.update(status=status, updated_at=datetime.now(timezone.utc).isoformat())
        shared.save_unique(output / "priority-status.json", state)

    record(state["status"])
    if not resume_wild:
        await drain(output)
        record("Terminal-Bench deferred; waiting for OpenClaw catch-up")
    protected_image = False
    while not resume_wild and active(CATCHUP_SERVICE):
        # A second tag retains the same layers when the catch-up lane releases
        # its tag, avoiding another large download between adjacent Wild phases.
        if not protected_image:
            identity = balanced.command(
                [
                    "docker",
                    "image",
                    "inspect",
                    image_loader.IMAGE,
                    "--format",
                    "{{.Id}}",
                ],
                check=False,
            )
            if identity == image_loader.IDENTITY:
                balanced.command(
                    ["docker", "image", "tag", image_loader.IMAGE, HANDOFF_IMAGE]
                )
                protected_image = True
        await asyncio.sleep(STAGE_POLL_SECONDS)
    catchup = json.loads((output / "catchup-status.json").read_text())
    if catchup.get("status") != "finished":
        record("OpenClaw catch-up needs recovery before remaining WildClawBench")
        return
    if protected_image:
        balanced.command(["docker", "image", "tag", HANDOFF_IMAGE, image_loader.IMAGE])
        balanced.command(["docker", "image", "rm", HANDOFF_IMAGE])
    if resume_wild:
        while not balanced.command(["docker", "image", "ls", "-q", image_loader.IMAGE]):
            if not active("leon-hermes-wild-recovery.service"):
                path = output / "hermes-lifecycle-audit.json"
                audit = json.loads(path.read_text()) if path.exists() else {}
                pending = any(
                    row["suite"] == "wild" and not row.get("replacement_trial")
                    for row in audit.get("affected_trials", [])
                )
                if pending:
                    record("Hermes recovery image preparation needs review")
                    return
                # Once recovery is complete, the normal runner owns the image
                # load needed to resume any remaining selected tasks.
                break
            record("waiting for verified shared WildClawBench image")
            await asyncio.sleep(STAGE_POLL_SECONDS)
        identity = balanced.command(
            ["docker", "image", "inspect", image_loader.IMAGE, "--format", "{{.Id}}"],
            check=False,
        )
        if identity and identity != image_loader.IDENTITY:
            raise RuntimeError("Shared WildClawBench image identity changed")
    balanced.command(["systemctl", "--user", "start", MAIN_SERVICE])
    record("remaining WildClawBench running; Terminal-Bench deferred")
    while active(MAIN_SERVICE):
        await asyncio.sleep(STAGE_POLL_SECONDS)
    audit_path = output / "hermes-lifecycle-audit.json"
    while active("leon-hermes-wild-recovery.service"):
        audit = json.loads(audit_path.read_text()) if audit_path.exists() else {}
        if not any(
            row["suite"] == "wild" and not row.get("replacement_trial")
            for row in audit.get("affected_trials", [])
        ):
            break
        record("waiting for approved Hermes WildClawBench replacements")
        await asyncio.sleep(STAGE_POLL_SECONDS)
    if protected_image:
        balanced.command(["docker", "image", "rm", image_loader.IMAGE], check=False)
    result = balanced.report(output, balanced.initialize(output))
    wild_count = sum(
        row["suite"] == "wild" and row["score"] is not None for row in result["trials"]
    )
    selected_count = len(balanced.initialize(output)["wild"]) * 9
    audit = json.loads(audit_path.read_text()) if audit_path.exists() else {}
    quarantined = [
        row
        for row in audit.get("affected_trials", [])
        if row["suite"] == "wild" and not row.get("replacement_trial")
    ]
    if wild_count != selected_count or quarantined:
        record("WildClawBench needs review; PinchBench next; Terminal-Bench deferred")
        return
    record("WildClawBench saved; PinchBench setup next; Terminal-Bench deferred")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reload-catchup", action="store_true")
    parser.add_argument("--resume-wild", action="store_true")
    args = parser.parse_args()
    if args.reload_catchup:
        asyncio.run(reload_catchup(args.output.resolve()))
    else:
        asyncio.run(run(args.output.resolve(), args.resume_wild))
