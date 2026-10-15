"""Recover audited outage slots without replaying unaffected candidate work."""

import argparse
import concurrent.futures
import json
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import yaml
from harbor.job import Job
from harbor.models.job.config import JobConfig

from scripts.benchmarks import balanced, campaign, metrics
from scripts.benchmarks.regrading import verified_receipt

ROOT = Path(__file__).resolve().parents[2]
GRADING_TIMEOUT_SECONDS = 650


def regrade(
    output,
    trial,
    credentials,
    recovery_name="network-regrade",
    dependencies=(),
    output_only=False,
    finalize_completed=False,
    history_directory=None,
):
    """Replay the unchanged native grader against read-only exported outputs."""
    if verified_receipt(trial):
        return

    if history_directory is not None and not (history_directory / "trajectory.json").is_file():
        raise RuntimeError("Restored native history requires a saved trajectory")

    history_sources = []
    if history_directory is not None:
        restoration = json.loads((history_directory / "history-restoration.json").read_text())
        for filename, expected in restoration["source_sha256"].items():
            source = Path(filename)
            if balanced.digest(source) != expected:
                raise RuntimeError("Restored native history source changed")
            history_sources.append(source)
        for restored in restoration.get("restored_workspace_files", []):
            if balanced.digest(Path(restored["restored_path"])) != restored["original_native_sha256"]:
                raise RuntimeError("Restored workspace file differs from native evidence")

    group = trial.parent.parent.name
    suite, task = group.split("--", 1)
    tests = output / "tasks" / suite / task / "tests"
    destination = trial / "verifier" / recovery_name
    destination.mkdir(exist_ok=True)
    adapter = destination / "adapter"
    adapter.mkdir(exist_ok=True)
    for filename in (
        "run-judge.py",
        "judge_images.py",
        "pilot.json",
        "grader-dependencies.json",
        "native_transcript.py",
        "audit_openai.py",
    ):
        source = output / "runtime/benchmarks" / filename
        snapshot = adapter / filename
        if finalize_completed:
            if not snapshot.exists() or balanced.digest(source) != balanced.digest(snapshot):
                raise RuntimeError("Completed replay adapter differs from current grading adapter")
        else:
            shutil.copy2(source, snapshot)
    name = "leon-grader-recovery-" + trial.name.lower()
    command = [
        "docker",
        "run",
        "--rm",
        "--name",
        name,
        "--read-only",
        "--label",
        "leon.benchmark.recovery=" + output.name,
        "--cpus",
        "2",
        "--memory",
        "2g",
        "--tmpfs",
        "/tmp_workspace:rw,exec,size=512m",
        "--tmpfs",
        "/logs/agent:rw,size=64m",
        "--tmpfs",
        "/root:rw,size=64m",
        "--tmpfs",
        "/tmp:rw,exec,size=256m",
        "-e",
        "PYTHONDONTWRITEBYTECODE=1",
    ]
    network = yaml.safe_load((output / "network.yaml").read_text())["services"]["main"]
    for dns in network.get("dns", []):
        command.extend(["--dns", dns])
    for key in balanced.PROXY_NAMES:
        command.extend(["-e", key + "="])
    if output_only:
        command.extend(["-e", "LEON_BENCHMARK_GRADER_HISTORY=output_only"])
    saved_output = trial / "artifacts/tmp_workspace"
    if history_directory is not None and (history_directory / "workspace").is_dir():
        saved_output = history_directory / "workspace"
    if not saved_output.exists():
        saved_output = destination / "empty-original-output"
        saved_output.mkdir(exist_ok=True)
    mounts = json.loads((output / "mounts.json").read_text())
    mounts.extend(
        [
            {"source": str(tests), "target": "/tests", "read_only": True},
            {
                "source": str(saved_output),
                "target": "/saved-output",
                "read_only": True,
            },
            {
                "source": str(trial / "agent"),
                "target": "/saved-agent",
                "read_only": True,
            },
            {
                "source": str(credentials),
                "target": "/run/leon-benchmark-subscription.json",
                "read_only": True,
            },
            {
                "source": str(destination),
                "target": "/logs/verifier",
                "read_only": False,
            },
        ]
    )
    for mount in mounts:
        suffix = ",readonly" if mount["read_only"] else ""
        command.extend(
            [
                "--mount",
                f"type=bind,source={mount['source']},target={mount['target']}{suffix}",
            ]
        )

    if history_directory is not None:
        command.extend(
            [
                "--mount",
                f"type=bind,source={history_directory},target=/restored-history,readonly",
            ]
        )
    preparation = ""
    if dependencies:
        installation = [
            "-m",
            "pip",
            "install",
            "--no-cache-dir",
            "--no-deps",
            "--target",
            "/tmp_workspace/.verifier-deps",
            *dependencies,
        ]
        preparation = (
            f"subprocess.run([sys.executable,*{installation!r}],check=True);"
            "sys.path.insert(0,'/tmp_workspace/.verifier-deps');"
            "import fitz;from PIL import Image;"
            "print('Replay dependencies verified:',fitz.__version__,Image.__version__,flush=True);"
        )
    command.extend(
        [
            balanced.benchmark.DEFAULT_IMAGE,
            "python3",
            "-c",
            "import shutil,runpy,sys,subprocess;from pathlib import Path;"
            + preparation
            + "sources=list(Path('/saved-output').iterdir());"
            "[(shutil.copytree(source,Path('/tmp_workspace')/source.name) "
            "if source.is_dir() else shutil.copy2(source,Path('/tmp_workspace')/source.name)) "
            "for source in sources];"
            + (
                ""
                if output_only
                else "shutil.copy2('/restored-history/trajectory.json','/logs/agent/trajectory.json');"
                if history_directory is not None
                else "sources=[Path('/saved-agent')/name for name in "
                "['hermes-result.json','trajectory.json','trajectory-events.jsonl','native-transcript.jsonl']];"
                "[shutil.copy2(path,Path('/logs/agent')/path.name) for path in sources if path.exists()];"
            )
            + "sys.path.insert(0,'/opt/leon/scripts/benchmarks');"
            "runpy.run_path('/opt/leon/scripts/benchmarks/run-judge.py',run_name='__main__')",
        ]
    )
    if not finalize_completed:
        try:
            with (destination / "test-stdout.txt").open("w") as stdout:
                with (destination / "test-stderr.txt").open("w") as stderr:
                    subprocess.run(
                        command,
                        check=True,
                        stdout=stdout,
                        stderr=stderr,
                        timeout=GRADING_TIMEOUT_SECONDS,
                    )
        finally:
            subprocess.run(
                ["docker", "rm", "-f", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )

    grade_file = destination / "wildclaw_score.json"
    requests_file = destination / "requests.jsonl"
    if not requests_file.exists():
        if finalize_completed:
            raise RuntimeError("Completed replay lacks its inference audit")
        requests_file.touch()
    grade = json.loads(grade_file.read_text())
    if history_directory is not None and any(
        grade.get(key) != value
        for key, value in restoration.get("preserved_native_gates", {}).items()
    ):
        raise RuntimeError("Native replay changed a required preserved artifact gate")
    reliability = json.loads((destination / "judge-reliability.json").read_text())
    if reliability.get("failed_calls") or any(
        grade.get(key) for key in balanced.benchmark.GRADER_ERROR_KEYS
    ):
        raise RuntimeError("Native replay needs review; original grade remains active")
    requests = [json.loads(line) for line in requests_file.read_text().splitlines()]
    if any(
        not row.get("completed")
        or row.get("errorType")
        or row.get("model") != "gpt-6.1-sol"
        or (row.get("reasoning") or {}).get("effort") != "high"
        or row.get("serviceTier") != "priority"
        or row.get("authMode") != "chatgpt_subscription"
        or not row.get("endpoint", "").startswith(
            "https://chatgpt.com/backend-api/codex/"
        )
        for row in requests
    ):
        raise RuntimeError("Replay inference settings failed verification")
    evidence = [
        trial / "result.json",
        tests / "checks.py",
        tests / "grader.py",
        grade_file,
        requests_file,
        destination / "judge-reliability.json",
        *adapter.iterdir(),
        *history_sources,
        *(
            [path for path in history_directory.rglob("*") if path.is_file()]
            if history_directory is not None
            else []
        ),
        *[
            trial / "agent" / name
            for name in ("trajectory.json", "leon-result.json")
            if (trial / "agent" / name).is_file()
        ],
        tests / "transcript_loader.py",
        *[
            p for p in (output / "runtime/benchmarks/grader-wheels").glob("*.whl")
        ],
        *[
            p
            for p in (trial / "artifacts/tmp_workspace/results").rglob("*")
            if p.is_file()
        ],
    ]
    healthcheck = trial / "agent/environment-healthcheck.jsonl"
    if dependencies and healthcheck.exists():
        evidence.append(healthcheck)
    normalization = destination / "judge-image-normalizations.jsonl"
    if normalization.exists():
        evidence.append(normalization)
    receipt = {
        "method_type": "native_grader_replay",
        "method": "Native grader and rubric on exported outputs with unchanged subscription settings. The shared judge adapter proportionally fits oversized images to the endpoint input limit; any normalization is audited. Original candidate and grading records remain intact.",
        "additional_candidate_attempts": 0,
        "replay_dependencies": list(dependencies),
        "output_only": output_only,
        "deterministic_no_inference": requests_file.stat().st_size == 0,
        "grade_file": str(grade_file.relative_to(output)),
        "requests_file": str(requests_file.relative_to(output)),
        "rewards": grade,
        "evidence_sha256": {
            str(path.relative_to(output)): balanced.digest(path) for path in evidence
        },
    }
    balanced.save(trial / "verifier/artifact-regrade.json", receipt)
    verified_receipt(trial)
    metrics.trial_metrics(trial)


async def remaining_slots(group):
    """Inspect Harbor's exact saved configuration before permitting a resume."""
    config = JobConfig.model_validate_json((group / "config.json").read_text())
    job = await Job.create(config)
    return len(job._remaining_trial_configs)


def main():
    """Finish grading-only recovery, then resume the audited candidate slots."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    output = parser.parse_args().output.resolve()
    incident = json.loads((output / "network-incident.json").read_text())
    directories = {
        output / item["preserved_directory"] for item in incident["affected_candidates"]
    }
    groups = {
        output / "jobs" / path.parent.parent.name / "native" for path in directories
    }
    if len(groups) != 1:
        raise RuntimeError("Recovery must preserve one task cohort at a time")
    group = groups.pop()
    runtime = json.loads((output / "runtime.json").read_text())
    incident["recovery_status"] = "replaying native grades only"
    balanced.save(output / "network-incident.json", incident)
    with tempfile.TemporaryDirectory(prefix=".grader-auth-", dir=output) as temporary:
        credentials = Path(temporary) / "subscription.json"
        subprocess.run(
            [
                runtime["hermes_host_python"],
                "-I",
                "-c",
                "import sys,runpy;"
                f"sys.path[:0]={runtime['hermes_host_import_paths']!r};"
                f"runpy.run_path({str(ROOT / 'scripts/benchmarks/export_subscription.py')!r},run_name='__main__')",
                str(credentials),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(regrade, output, group / item["trial"], credentials)
                for item in incident["grading_only_trials"]
            ]
            for future in futures:
                future.result()

    import asyncio

    remaining = asyncio.run(remaining_slots(group))
    if remaining != len(incident["affected_candidates"]):
        raise RuntimeError("Harbor resume does not match audited interrupted slots")
    report = campaign.report(
        output, json.loads((output / "selection.json").read_text())
    )
    rows = [row for row in report["trials"] if Path(row["trial"]).parent == group]
    if len(rows) != len(incident["grading_only_trials"]) + len(
        incident["unaffected_trials"]
    ) or any(row["score"] is None for row in rows):
        raise RuntimeError("Preserved candidate attempts lack valid grades")
    incident.update(
        recovery_status="resuming interrupted candidate slots",
        candidate_slots_to_resume=remaining,
        recovered_at=datetime.now(timezone.utc).isoformat(),
    )
    balanced.save(output / "network-incident.json", incident)
    subprocess.run(
        ["systemctl", "--user", "restart", "leon-benchmark-campaign.service"],
        check=True,
    )


if __name__ == "__main__":
    main()
