"""Replace only attempts whose native streams were stopped by the harness."""

import argparse
import asyncio
import json
from pathlib import Path

from scripts.benchmarks import balanced, campaign, task_integrity

TASK = "05_Creative_Synthesis_task_6_clothing_outfit_to_model_image"
MODE = "native-stream-repair"


async def run(output: Path) -> None:
    """Keep original slots and all unaffected native candidates immutable."""
    validation = json.loads((output / "openclaw-stream-lifecycle-validation.json").read_text())
    control = json.loads((output / "native-image-agent-preflight-lifecycle-v4.json").read_text())
    evidence = control["checks"][0]
    completion = evidence["native_evidence"]["agent/openclaw-media-completion.json"]
    if (
        evidence["exception"]
        or not evidence["native_evidence"].get("verifier/image-delivery.json")
        or completion["unsettled_subscription_streams"] != 0
    ):
        raise RuntimeError("Native backend/lifecycle control requires review")

    group = output / "jobs" / f"wild--{TASK}"
    originals = [
        group / "native-repair" / row["trial"]
        for row in validation["offline_controls"]
        if row["pending_streams_at_prior_teardown"] > 0
    ]
    audit_path = output / "environment-repair-audit.json"
    audit = json.loads(audit_path.read_text())
    for trial in originals:
        if any(row["trial"] == str(trial) for row in audit["affected_trials"]):
            continue
        audit["affected_trials"].append(
            {
                "suite": "wild",
                "task": TASK,
                "agent": "openclaw",
                "trial": str(trial),
                "trial_name": trial.name,
                "reason": "Harness teardown occurred with an unsettled native subscription stream.",
                "original_result_sha256": balanced.digest(trial / "result.json"),
                "replacement_directory": str(group / MODE),
            }
        )
    balanced.save(audit_path, audit)
    selection = json.loads((output / "selection.json").read_text())
    campaign.report(output, selection)
    balanced.benchmark.configure_runtime(output)
    item = next(row for row in selection["wild"] if row["name"] == TASK)
    await asyncio.to_thread(
        balanced.download, output, "wild", TASK, balanced.benchmark.MANIFEST["revision"]
    )
    baseline = {row["Id"] for row in balanced.docker_objects("image")}
    results = await balanced.execute(
        output,
        selection,
        "wild",
        item,
        MODE,
        baseline,
        agents={"openclaw": balanced.benchmark.AGENTS["openclaw"]},
        attempts=len(originals),
        release_images=False,
    )
    if len(results) != len(originals):
        raise RuntimeError("Replacement attempt coverage differs from damaged slots")
    audit = json.loads(audit_path.read_text())
    for old, result in zip(
        sorted(originals),
        sorted(results, key=lambda row: row["trial_name"]),
        strict=True,
    ):
        trial = group / MODE / result["trial_name"]
        review = task_integrity.inspect_trial(output, trial)
        if review["requires_review"]:
            raise RuntimeError("Native replacement requires validity review")
        rows = [
            row for row in audit["affected_trials"] if row["trial"] == str(old)
        ]
        rows[0].update(
            replacement_trial=str(trial),
            replacement_result_sha256=balanced.digest(trial / "result.json"),
        )
    balanced.save(audit_path, audit)
    balanced.save(
        group / "stream-repair-complete.json",
        {"replacement_attempts": len(results), "unaffected_attempts_preserved": 7},
    )
    campaign.report(output, selection)
    await asyncio.to_thread(balanced.cleanup_job, output, group / MODE, True, False)


def main() -> None:
    """Run the explicitly audited subscription-stream repair cohort."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(run(args.output.resolve()))


if __name__ == "__main__":
    main()
