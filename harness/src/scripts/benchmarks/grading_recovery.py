"""Recover native grading from saved outputs without repeating candidate execution."""

import argparse
import concurrent.futures
import json
import subprocess
import tempfile
from pathlib import Path

from scripts.benchmarks import balanced, campaign
from scripts.benchmarks.network_recovery import regrade
from scripts.benchmarks.task_integrity import accept_repair, inspect_trial


def main():
    """Replay reviewed output-only graders and retain every original candidate."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--finalize-completed", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    contracts = json.loads((output / "output-only-grader-contracts.json").read_text())
    tests = output / "tasks/wild" / args.task / "tests"
    if balanced.digest(tests / "checks.py") != contracts[args.task]["checks_sha256"]:
        raise RuntimeError("Reviewed grading contract changed")

    group = output / "jobs" / f"wild--{args.task}"
    results = list((group / "native-repair").glob("*/result.json"))
    if len(results) != 9:
        raise RuntimeError("Grading recovery requires the complete task cohort")
    trials = []
    for result in results:
        grade_path = result.parent / "verifier/wildclaw_score.json"
        if not grade_path.exists():
            continue
        grade = json.loads(grade_path.read_text())
        if grade.get("judge_error"):
            trials.append(result.parent)

    balanced.benchmark.configure_runtime(output)
    runtime = json.loads((output / "runtime.json").read_text())
    exporter = Path(__file__).with_name("export_subscription.py")
    with tempfile.TemporaryDirectory(prefix=".grader-auth-", dir=output) as temporary:
        credentials = Path(temporary) / "subscription.json"
        subprocess.run(
            [
                runtime["hermes_host_python"],
                "-I",
                "-c",
                "import sys,runpy;"
                f"sys.path[:0]={runtime['hermes_host_import_paths']!r};"
                f"runpy.run_path({str(exporter)!r},run_name='__main__')",
                str(credentials),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(
                    regrade,
                    output,
                    trial,
                    credentials,
                    recovery_name="image-dependency-regrade",
                    output_only=True,
                    finalize_completed=args.finalize_completed,
                )
                for trial in trials
            ]
            for future in futures:
                future.result()

    reviews = [inspect_trial(output, result.parent) for result in results]
    balanced.save(group / "grading-recovery-review.json", {"reviews": reviews})
    if not any(review["requires_review"] for review in reviews):
        accept_repair(
            output,
            "wild",
            args.task,
            [json.loads(result.read_text()) for result in results],
        )
        balanced.save(
            group / "repair-complete.json",
            {"attempts": 9, "grading_only_recovery": len(trials)},
        )
        hold = group / "validity-hold.json"
        if hold.exists():
            hold.rename(group / "resolved-validity-hold.json")
    campaign.report(output, json.loads((output / "selection.json").read_text()))


if __name__ == "__main__":
    main()
