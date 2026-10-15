"""Resume audited pre-agent setup slots after their healthy cohort has finished."""

import argparse
import asyncio
import json
import shutil
import subprocess
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from scripts.benchmarks import balanced, campaign
from scripts.benchmarks.network_recovery import remaining_slots

UNIT = "leon-benchmark-campaign.service"


def main():
    """Keep successful candidates and retry only environments with zero inference."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--suite", required=True)
    parser.add_argument("--task", required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    group = output / "jobs" / f"{args.suite}--{args.task}" / "native"
    receipt = output / "setup-recovery.json"
    if receipt.exists():
        raise RuntimeError(
            "Setup recovery already recorded; repeated failures require review"
        )
    while True:
        active = subprocess.run(
            ["systemctl", "--user", "is-active", UNIT], capture_output=True, text=True
        )
        if active.stdout.strip() != "active":
            break
        time.sleep(15)

    state = json.loads((output / "status.json").read_text())
    if (
        state.get("status") != "held"
        or state.get("suite") != args.suite
        or state.get("task") != args.task
    ):
        raise RuntimeError("Controller did not hold the expected setup-failure cohort")
    report = campaign.report(
        output, json.loads((output / "selection.json").read_text())
    )
    rows = [row for row in report["trials"] if Path(row["trial"]).parent == group]
    if len(rows) != 9:
        raise RuntimeError("Healthy cohort has not fully preserved its results")
    failed = []
    for row in rows:
        if row["score"] is not None:
            continue
        trial = Path(row["trial"])
        result = json.loads((trial / "result.json").read_text())
        requests = trial / "agent/requests.jsonl"
        if (
            (result.get("exception_info") or {}).get("exception_type")
            != "HealthcheckError"
            or result.get("agent_execution")
            or (requests.exists() and requests.stat().st_size)
        ):
            raise RuntimeError(
                "A failed slot contains candidate execution or another error"
            )
        failed.append(row)
    if Counter(row["agent"] for row in failed) != Counter({"hermes": 1, "openclaw": 1}):
        raise RuntimeError("Setup failures do not match the two audited empty slots")
    destination = output / "setup-interrupted" / group.parent.name / "native"
    destination.mkdir(parents=True, exist_ok=True)
    affected = []
    for row in sorted(failed, key=lambda row: row["trial"]):
        trial = Path(row["trial"])
        preserved = destination / trial.name
        if preserved.exists():
            raise RuntimeError("Setup failure archive already exists")
        affected.append(
            {
                "agent": row["agent"],
                "trial": trial.name,
                "preserved_directory": str(preserved.relative_to(output)),
                "original_result_sha256": balanced.digest(trial / "result.json"),
                "candidate_inference_calls": 0,
            }
        )
        shutil.move(str(trial), preserved)
    remaining = asyncio.run(remaining_slots(group))
    if remaining != 2:
        raise RuntimeError(
            "Harbor resume must contain exactly the two empty setup slots"
        )
    balanced.save(
        receipt,
        {
            "suite": args.suite,
            "task": args.task,
            "status": "resuming two pre-agent setup slots",
            "affected_environments": affected,
            "healthy_candidates_repeated": 0,
            "setup_commands": "Unchanged native prerequisite commands; passive diagnostics enabled",
            "resumed_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    campaign.report(output, json.loads((output / "selection.json").read_text()))
    subprocess.run(["systemctl", "--user", "restart", UNIT], check=True)


if __name__ == "__main__":
    main()
