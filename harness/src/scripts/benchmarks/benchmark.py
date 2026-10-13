# /// script
# requires-python = ">=3.12"
# dependencies = ["harbor==0.22.0", "huggingface-hub==1.29.0"]
# ///

"""Prepare, execute, and summarize reproducible assistant comparisons with Harbor."""

import argparse
import asyncio
import collections
import concurrent.futures
import hashlib
import json
import math
import os
import shutil
import signal
import statistics
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import toml
from huggingface_hub import hf_hub_download

ROOT = Path(__file__).resolve().parents[2]
LEON_ROOT = Path(os.environ.get("LEON_BENCHMARK_LEON_ROOT", ROOT.parent / "leon"))
MANIFEST = json.loads(Path(__file__).with_name("pilot.json").read_text())
DEFAULT_OUTPUT = ROOT / "tmp" / "benchmarks" / "comparison"
DEFAULT_IMAGE = "wildclawbench-ubuntu:v1.3"
SOURCE_DIRECTORIES = ("schemas", "core/config", "bridges/nodejs/dist")
AGENTS = {
    "leon": "scripts.benchmarks.harbor_agents:Leon",
    "openclaw": "scripts.benchmarks.harbor_agents:BenchmarkOpenClaw",
    "hermes": "scripts.benchmarks.harbor_agents:BenchmarkHermes",
}
GRADER_ERROR_KEYS = ("judge_error", "llm_judge_error", "error", "traceback")
FALLBACK_JUDGE_METHODS = ("regex_fallback", "rule_fallback")
PYTHON_TRACEBACK_HEADER = "Traceback (most recent call last):"
SCORED_AGENT_EXCEPTIONS = ("AgentTimeoutError",)
RETURNED_AGENT_EXCEPTION = "NonZeroAgentExitCodeError"
NATIVE_AGENT_RESULT_FILES = ("leon-result.json", "hermes-result.json")
WARMUP_COMMANDS = {
    "npm install -g agent-browser": (
        "if ! command -v agent-browser >/dev/null 2>&1; then\n"
        "  env PNPM_HOME=/usr/local /tmp/leon-benchmark/bin/pnpm/pnpm add -g agent-browser\n"
        "fi"
    )
}


def output_json(path: Path, value: object) -> None:
    """Save reviewable configuration and results with stable formatting."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def git_snapshot(source: Path, target: Path) -> None:
    """Export source files without owner state, repository history, or task graders."""
    files = (
        subprocess.check_output(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=source,
        )
        .decode()
        .split("\0")
    )
    if target.exists():
        shutil.rmtree(target)
    for name in files:
        if not name or name.startswith("test/") or not (source / name).is_file():
            continue
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, destination)


def download_task(task: str, output: Path) -> None:
    """Download a task and its fixtures at the declared immutable dataset revision."""
    repository = MANIFEST["dataset"]
    revision = MANIFEST["revision"]
    url = f"https://huggingface.co/api/datasets/{repository}/tree/{revision}/{task}?recursive=true&limit=1000"
    files = []
    while url:
        with urllib.request.urlopen(url, timeout=60) as response:
            files.extend(json.load(response))
            link = response.headers.get("Link", "")
            url = next(
                (
                    part.split("<", 1)[1].split(">", 1)[0]
                    for part in link.split(",")
                    if 'rel="next"' in part
                ),
                "",
            )

    for item in files:
        if item["type"] != "file":
            continue
        destination = output / "original" / item["path"]
        if destination.exists() and destination.stat().st_size == item["size"]:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        hf_hub_download(
            repo_id=repository,
            repo_type="dataset",
            revision=revision,
            filename=item["path"],
            local_dir=output / "original",
        )
        if destination.stat().st_size != item["size"]:
            raise ValueError(f"Incomplete download: {item['path']}")
    print(f"Downloaded {task}", flush=True)


def mount(source: Path, target: str) -> dict:
    """Expose runtime code read-only without sharing profile or conversation state."""
    return {
        "type": "bind",
        "source": str(source.resolve()),
        "target": target,
        "read_only": True,
    }


def source_digest(directory: Path) -> str:
    """Identify the actual exported source, including local uncommitted work."""
    digest = hashlib.sha256()
    for source in sorted(directory.rglob("*")):
        if source.is_file():
            digest.update(str(source.relative_to(directory)).encode())
            digest.update(b"\0")
            digest.update(source.read_bytes())
    return digest.hexdigest()


def prepare(args: argparse.Namespace) -> None:
    """Prepare native runtime mounts and identical task environments for every agent."""
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    snapshot = output / "source" / "leon"
    if (snapshot / "test").is_dir():
        shutil.rmtree(snapshot / "test")
    git_snapshot(LEON_ROOT, snapshot)
    for name in SOURCE_DIRECTORIES:
        if (LEON_ROOT / name).is_dir():
            shutil.copytree(LEON_ROOT / name, snapshot / name, dirs_exist_ok=True)

    shutil.copytree(
        Path(__file__).parent, snapshot / "scripts/benchmarks", dirs_exist_ok=True
    )

    mounts = [
        mount(snapshot, "/opt/leon"),
        mount(LEON_ROOT / "node_modules", "/opt/leon/node_modules"),
    ]
    for source in (LEON_ROOT / "tools").glob("*/*/src/nodejs/node_modules"):
        mounts.append(mount(source, "/opt/leon/" + str(source.relative_to(LEON_ROOT))))
    bridge_dependencies = LEON_ROOT / "bridges/nodejs/node_modules"
    if bridge_dependencies.is_dir():
        mounts.append(
            mount(bridge_dependencies, "/opt/leon/bridges/nodejs/node_modules")
        )
    python_environments = list((LEON_ROOT / "tools").glob("*/*/src/*/.venv"))
    python_bridge = LEON_ROOT / "bridges/python/src/.venv"
    if python_bridge.is_dir():
        python_environments.append(python_bridge)
    for source in python_environments:
        mounts.append(mount(source, "/opt/leon/" + str(source.relative_to(LEON_ROOT))))

    # Docker cannot create child mount points under a read-only source mount.
    for dependency_mount in mounts[1:]:
        relative = Path(dependency_mount["target"]).relative_to("/opt/leon")
        (snapshot / relative).mkdir(parents=True, exist_ok=True)

    leon_home = Path(os.environ.get("LEON_HOME", Path.home() / ".leon"))
    binary_directory = leon_home / "bin"
    if binary_directory.is_dir():
        mounts.append(mount(binary_directory, "/tmp/leon-benchmark/bin"))
        # Installed Python environments retain absolute interpreter symlinks.
        # Expose only that binary payload at its native location, never profiles.
        mounts.append(
            mount(binary_directory / "python", str(binary_directory / "python"))
        )

    openclaw_command = Path(shutil.which("openclaw") or "").resolve()
    # Volta launchers resolve through the package manager's public package root.
    candidates = [openclaw_command.parent.parent]
    volta = Path(os.environ.get("VOLTA_HOME", Path.home() / ".volta"))
    candidates.append(volta / "tools/image/packages/openclaw/lib/node_modules/openclaw")
    global_root = subprocess.run(["pnpm", "root", "-g"], text=True, capture_output=True)
    if global_root.returncode == 0:
        candidates.append(Path(global_root.stdout.strip()) / "openclaw")
    openclaw_source = next(
        (p for p in candidates if (p / "openclaw.mjs").is_file()), None
    )
    if not openclaw_source:
        raise FileNotFoundError("Could not locate the installed OpenClaw package")
    mounts.append(mount(openclaw_source, "/opt/openclaw"))

    hermes_command = json.loads(
        subprocess.check_output(["hermes", "--print-runtime-command"], text=True)
    )
    python_runtime = Path(hermes_command[0]).resolve().parent.parent
    hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    hermes_source = hermes_home / "hermes-agent"
    hermes_snapshot = output / "source" / "hermes"
    git_snapshot(hermes_source, hermes_snapshot)
    mounts.append(mount(hermes_snapshot, str(hermes_source)))
    mounts.append(mount(python_runtime, str(python_runtime)))
    site_packages = list(
        (hermes_home / "installs").glob(
            "*/environments/*/venv/lib/python*/site-packages"
        )
    )
    if not site_packages:
        raise FileNotFoundError("Could not locate Hermes runtime dependencies")
    for directory in site_packages:
        mounts.append(mount(directory, str(directory)))
    import_paths = [str(hermes_source)] + [str(p) for p in site_packages]
    hermes_command = [
        hermes_command[0],
        "-I",
        "-c",
        "import sys,runpy,os; sys.dont_write_bytecode=True; "
        f"sys.path[:0]={import_paths!r}; "
        "runpy.run_path('/opt/leon/scripts/benchmarks/audit_openai.py'); "
        "runpy.run_path('/opt/leon/scripts/benchmarks/run-hermes.py',run_name='__main__') "
        "if os.environ.get('LEON_BENCHMARK_HERMES_RUNNER') else "
        "runpy.run_module('hermes_cli.main',run_name='__main__',alter_sys=True)",
    ]

    tasks = args.task or MANIFEST["tasks"]
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda task: download_task(task, output), tasks))

    adaptations = []
    if (output / "tasks").exists():
        shutil.rmtree(output / "tasks")
    for task in tasks:
        source = output / "original" / task
        target = output / "tasks" / task
        shutil.copytree(source, target, dirs_exist_ok=True)
        # Harbor requires this directory even when a task uses no fixture files.
        (target / "environment").mkdir(exist_ok=True)
        for skill_path in (target / "environment").rglob("SKILL.md"):
            content = skill_path.read_text()
            for original_name, alias in MANIFEST["task_skill_name_aliases"].items():
                content = content.replace(f"name: {original_name}", f"name: {alias}")
            skill_path.write_text(content)
        for shell_script in target.rglob("*.sh"):
            content = shell_script.read_bytes()
            if b"\r\n" in content:
                shell_script.write_bytes(content.replace(b"\r\n", b"\n"))
                adaptations.append(
                    {
                        "task": task,
                        "normalized_shell_line_endings": str(
                            shell_script.relative_to(target)
                        ),
                    }
                )
            if shell_script.name == "run-warmup.sh":
                text = shell_script.read_text()
                for original, replacement in WARMUP_COMMANDS.items():
                    if original in text:
                        text = text.replace(original, replacement)
                        adaptations.append({"task": task, "package_manager": "pnpm"})
                shell_script.write_text(text)
        config = toml.loads((target / "task.toml").read_text())
        config["environment"]["docker_image"] = args.image
        environment_variables = config["environment"].get("env", {})
        for name in tuple(environment_variables):
            if name.startswith("OPENROUTER_"):
                del environment_variables[name]
        environment_variables["JUDGE_MODEL"] = MANIFEST["model"].split("/", 1)[1]
        for grader_source in (target / "tests").rglob("*.py"):
            content = grader_source.read_text()
            grader_source.write_text(
                content.replace("OPENROUTER", "CODEX_SUBSCRIPTION")
            )
        test_entry = target / "tests/test.sh"
        test_entry.write_text(
            test_entry.read_text().replace(
                "python3 /tests/grader.py",
                "python3 /opt/leon/scripts/benchmarks/run-judge.py",
            )
        )
        adaptations.append(
            {
                "task": task,
                "judge_transport": "chatgpt_subscription_responses",
                "judge_model": MANIFEST["model"],
            }
        )
        healthcheck = config["environment"].get("healthcheck")
        if (
            healthcheck
            and not (target / "environment/.wildclaw/run-warmup.sh").exists()
        ):
            # Standalone fixtures need no service warmup. Record the upstream
            # missing entry point rather than claiming an exact reproduction.
            del config["environment"]["healthcheck"]
            adaptations.append({"task": task, "missing_healthcheck": healthcheck})
        (target / "task.toml").write_text(toml.dumps(config))

    job = {
        "job_name": "wildclaw-pilot",
        "jobs_dir": str(output / "jobs"),
        "n_attempts": MANIFEST["attempts"],
        "n_concurrent_trials": MANIFEST["concurrency"],
        "environment": {
            "type": "docker",
            "delete": True,
            "mounts": mounts,
            "override_cpus": 2,
            "override_memory_mb": 4_096,
        },
        "agents": [
            {"import_path": agent, "model_name": MANIFEST["model"]}
            for agent in AGENTS.values()
        ],
        "datasets": [{"path": str(output / "tasks")}],
    }
    output_json(output / "job.json", job)
    output_json(
        output / "runtime.json",
        {"hermes_command": hermes_command, "hermes_import_paths": import_paths},
    )
    output_json(
        output / "provenance.json",
        {
            "manifest": MANIFEST,
            "prepared_tasks": tasks,
            "image": args.image,
            "image_identity": json.loads(
                subprocess.check_output(
                    [
                        "docker",
                        "image",
                        "inspect",
                        args.image,
                        "--format",
                        "{{json .Id}}",
                    ],
                    text=True,
                )
            ),
            "adaptations": adaptations,
            "leon_commit": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=LEON_ROOT, text=True
            ).strip(),
            "leon_source_sha256": source_digest(snapshot),
            "openclaw_version": subprocess.check_output(
                ["openclaw", "--version"], text=True
            ).strip(),
            "hermes_commit": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=hermes_source, text=True
            ).strip(),
            "hermes_source_sha256": source_digest(hermes_snapshot),
            "leon_node_version": subprocess.check_output(
                [str(binary_directory / "node/bin/node"), "--version"], text=True
            ).strip(),
            "leon_pnpm_version": subprocess.check_output(
                [str(binary_directory / "pnpm/pnpm"), "--version"], text=True
            ).strip(),
        },
    )
    print(
        f"Prepared {len(tasks)} tasks; {len(tasks) * len(AGENTS) * MANIFEST['attempts']} trials"
    )


def run(args: argparse.Namespace) -> None:
    """Execute the prepared comparison using subscription access and fresh containers."""
    from harbor.models.job.config import JobConfig

    output = args.output.resolve()
    config = json.loads((output / "job.json").read_text())
    provenance = json.loads((output / "provenance.json").read_text())
    config["n_concurrent_trials"] = MANIFEST["concurrency"]
    config["environment"]["delete"] = True
    if args.label == "pilot" and provenance["image"] != DEFAULT_IMAGE:
        raise ValueError(
            "Scored pilots require the official image; use --label smoke for startup checks"
        )
    configure_runtime(output)
    config["job_name"] = f"wildclaw-{args.label}-{args.agent or 'all'}-{time.time_ns()}"
    if args.agent:
        config["agents"] = [
            agent
            for agent in config["agents"]
            if agent["import_path"] == AGENTS[args.agent]
        ]
    if args.task:
        config["datasets"][0]["task_names"] = args.task
    if args.attempts:
        config["n_attempts"] = args.attempts
    JobConfig.model_validate(config)
    run_config = output / "runs" / (config["job_name"] + ".json")
    output_json(run_config, config)
    try:
        subprocess.run(
            ["harbor", "run", "--config", str(run_config), "--yes"],
            cwd=ROOT,
            check=True,
        )
    finally:
        job_directory = Path(config["jobs_dir"]) / config["job_name"]
        if job_directory.is_dir():
            output_json(
                job_directory / "provenance.json", provenance | {"label": args.label}
            )


def configure_runtime(output: Path) -> None:
    """Use configured subscription logins without forwarding API billing keys."""
    from scripts.benchmarks.healthcheck_observer import install

    install()
    for name in (
        "OPENAI_API_KEY",
        "LEON_OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "LEON_OPENROUTER_API_KEY",
    ):
        os.environ.pop(name, None)
    os.environ["LEON_BENCHMARK_RUNTIME"] = str(output / "runtime.json")
    os.environ["LEON_BENCHMARK_HERMES_COMMAND"] = json.dumps(
        json.loads((output / "runtime.json").read_text())["hermes_command"]
    )
    os.environ["PYTHONPATH"] = str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", "")
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))


def resume(args: argparse.Namespace) -> None:
    """Continue only unsaved trials, preserving completed and interrupted records."""
    from harbor.job import Job
    from harbor.models.job.config import JobConfig

    if not args.job:
        raise ValueError("Resuming requires --job JOB")
    output = args.output.resolve()
    job_directory = output / "jobs" / args.job
    configure_runtime(output)
    config_path = job_directory / "config.json"
    config = JobConfig.model_validate_json(config_path.read_text())
    config.n_concurrent_trials = MANIFEST["concurrency"]
    config.environment.delete = True
    config_path.write_text(config.model_dump_json(indent=2))
    provenance = json.loads((job_directory / "provenance.json").read_text())
    original_lock = job_directory / "lock-before-subscription.json"
    lock_path = job_directory / "lock.json"
    if provenance.get("credential_transition") and not original_lock.exists():
        # A changed authentication phase needs a new input fingerprint. Retain
        # the original fingerprint alongside its saved trial records.
        shutil.copy2(lock_path, original_lock)
        lock_path.unlink()

    async def continue_job() -> None:
        job = await Job.create(config)
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        if task is not None:
            loop.add_signal_handler(signal.SIGTERM, task.cancel)
        # The CLI's default resume deletes cancellation records. Retaining them
        # prevents interrupted or completed work from being repeated implicitly.
        try:
            await job.run()
        finally:
            loop.remove_signal_handler(signal.SIGTERM)

    asyncio.run(continue_job())


def report(args: argparse.Namespace) -> None:
    """Keep grader/infrastructure errors separate from genuine task scores."""
    rows = []
    pattern = f"wildclaw-{args.label}-*/*/result.json"
    if args.job:
        pattern = f"{args.job}/*/result.json"
    for result_path in (args.output / "jobs").glob(pattern):
        result = json.loads(result_path.read_text())
        preserved_path = result_path.parent.parent / "preserved-trials.json"
        preserved = (
            json.loads(preserved_path.read_text()) if preserved_path.exists() else {}
        )
        legacy = result_path.parent.name in preserved
        provenance_path = result_path.parent.parent / "provenance.json"
        provenance = (
            json.loads(provenance_path.read_text()) if provenance_path.exists() else {}
        )
        skills_path = result_path.parent / "agent/task-skills.json"
        skills_evidence = (
            json.loads(skills_path.read_text()) if skills_path.exists() else {}
        )
        running_before_registration = {
            trial
            for update in provenance.get("task_skill_registration_updates", [])
            for trial in update["already_running_trials"]
        }
        skills_setup_verified = skills_path.exists() and (
            result_path.parent.name not in running_before_registration
        )
        skills_registered = skills_setup_verified and bool(
            skills_evidence.get("registered")
        )
        verifier_directory = result_path.parent / "verifier"
        grading_recovery_path = result_path.parent / "grading-recovery.json"
        grading_recovery = (
            json.loads(grading_recovery_path.read_text())
            if grading_recovery_path.exists()
            else None
        )
        rewards = (result.get("verifier_result") or {}).get("rewards")
        if grading_recovery:
            # Regrading must retain the completed agent record and make its
            # replacement score traceable to those exact saved outputs.
            if (
                grading_recovery["source_result_sha256"]
                != hashlib.sha256(result_path.read_bytes()).hexdigest()
            ):
                raise ValueError(f"Grading recovery source changed: {result_path}")
            for relative, expected in grading_recovery[
                "source_evidence_sha256"
            ].items():
                source = result_path.parent / relative
                if hashlib.sha256(source.read_bytes()).hexdigest() != expected:
                    raise ValueError(f"Grading recovery evidence changed: {source}")
            verifier_directory = (
                result_path.parent / grading_recovery["verifier_directory"]
            )
            rewards = json.loads((verifier_directory / "reward.json").read_text())
        grader_path = verifier_directory / "wildclaw_score.json"
        grader = json.loads(grader_path.read_text()) if grader_path.exists() else {}
        evidence_path = result_path.parent / "agent/requests.jsonl"
        evidence = (
            [json.loads(line) for line in evidence_path.read_text().splitlines()]
            if evidence_path.exists()
            else []
        )
        configured = bool(evidence) and all(
            item.get("model") == MANIFEST["model"].split("/", 1)[1]
            and (item.get("reasoning") or {}).get("effort") == MANIFEST["reasoning"]
            and item.get("serviceTier") in {"fast", "priority"}
            for item in evidence
        )
        inference_succeeded = any(
            200 <= item.get("status", 0) < 300 for item in evidence
        )
        auth_mode = "api_key" if legacy else "chatgpt_subscription"
        phase = (
            "api_key"
            if legacy
            else "subscription_native_skills"
            if skills_setup_verified
            else "subscription_unregistered_skills"
        )
        auth_verified = legacy or all(
            item.get("authMode") == auth_mode for item in evidence
        )
        grader_issues = {
            key: value
            for key, value in grader.items()
            if (key in GRADER_ERROR_KEYS and value)
            or (key.endswith("judge_method") and value in FALLBACK_JUDGE_METHODS)
        }
        verifier_logs = [
            verifier_directory / name for name in ("test-stderr.txt", "test-stdout.txt")
        ]
        # Missing required artifacts are scored failures. The upstream wrapper
        # prints a Python traceback when grading itself raises an exception.
        if grader.get("error") and any(
            log.exists() and PYTHON_TRACEBACK_HEADER in log.read_text()
            for log in verifier_logs
        ):
            grader_issues["error"] = grader["error"]
        timing = result.get("agent_execution") or {}
        duration = None
        if timing.get("started_at") and timing.get("finished_at"):
            duration = (
                datetime.fromisoformat(timing["finished_at"])
                - datetime.fromisoformat(timing["started_at"])
            ).total_seconds()
        rows.append(
            {
                "trial": str(result_path.parent),
                "task": result.get("task_name"),
                "agent": result.get("agent_info", {}).get("name"),
                "auth_mode": auth_mode,
                "comparison_phase": phase,
                "task_skills_registered": skills_registered,
                "task_skill_setup_verified": skills_setup_verified,
                "exception": result.get("exception_info"),
                "native_turn_returned": any(
                    (result_path.parent / "agent" / name).exists()
                    for name in NATIVE_AGENT_RESULT_FILES
                ),
                "grader_error": grader_issues or None,
                "grading_recovery": grading_recovery,
                "rewards": rewards,
                "usage": result.get("agent_result"),
                "seconds": duration,
                "inference_settings_verified": configured
                and inference_succeeded
                and auth_verified,
                "request_evidence": evidence,
                "reported_service_tiers": sorted(
                    {
                        item["returnedServiceTier"]
                        for item in evidence
                        if item.get("returnedServiceTier")
                    }
                ),
            }
        )
    groups = collections.defaultdict(list)
    for row in rows:
        groups[(row["agent"], row["comparison_phase"])].append(row)
    summary = {}
    for (agent, phase), trials in sorted(groups.items()):
        valid = [
            row
            for row in trials
            if (
                not row["exception"]
                or row["exception"]["exception_type"] in SCORED_AGENT_EXCEPTIONS
                or (
                    row["exception"]["exception_type"] == RETURNED_AGENT_EXCEPTION
                    and row["native_turn_returned"]
                )
            )
            and not row["grader_error"]
            and row["inference_settings_verified"]
            and (row["rewards"] or {}).get("overall_score") is not None
        ]
        scores = [row["rewards"]["overall_score"] for row in valid]
        times = [row["seconds"] for row in valid if row["seconds"] is not None]
        key = f"{agent} / {phase}"
        summary[key] = {
            "agent": agent,
            "auth_mode": trials[0]["auth_mode"],
            "comparison_phase": phase,
            "task_skill_setup_verified": trials[0]["task_skill_setup_verified"],
            "registered_task_skill_trials": sum(
                row["task_skills_registered"] for row in trials
            ),
            "reported_service_tiers": sorted(
                {tier for row in trials for tier in row["reported_service_tiers"]}
            ),
            "trials": len(trials),
            "valid_scored_trials": len(valid),
            "usage_scope": "valid_scored_trials",
            "exceptions": sum(bool(row["exception"]) for row in trials),
            "grader_errors": sum(bool(row["grader_error"]) for row in trials),
            "unverified_settings": sum(
                not row["inference_settings_verified"] for row in trials
            ),
            "mean_score": statistics.mean(scores) if scores else None,
            "median_seconds": statistics.median(times) if times else None,
            "p95_seconds": sorted(times)[math.ceil(len(times) * 0.95) - 1]
            if times
            else None,
            "fully_satisfied_attempts": sum(score >= 1.0 for score in scores),
            "fully_satisfied_rate": sum(score >= 1.0 for score in scores) / len(scores)
            if scores
            else None,
            "runtime_completion_rate": sum(not row["exception"] for row in trials)
            / len(trials),
        }
        for metric in (
            "n_input_tokens",
            "n_cache_tokens",
            "n_output_tokens",
            "cost_usd",
        ):
            values = [(row["usage"] or {}).get(metric) for row in valid]
            summary[key][metric] = (
                sum(values)
                if values and all(value is not None for value in values)
                else None
            )
        per_task = collections.defaultdict(list)
        for row in valid:
            per_task[row["task"]].append(row["rewards"]["overall_score"])
        summary[key]["per_task"] = {
            task: {
                "attempts": len(values),
                "mean_score": statistics.mean(values),
                "min_score": min(values),
                "max_score": max(values),
            }
            for task, values in sorted(per_task.items())
        }
    destination = args.output / f"{args.label}-results.json"
    phases = sorted({item["comparison_phase"] for item in summary.values()})
    matched_tasks = {
        phase: [
            task
            for task in MANIFEST["tasks"]
            if all(
                task in summary.get(f"{agent} / {phase}", {}).get("per_task", {})
                for agent in AGENTS
            )
        ]
        for phase in phases
    }
    for item in summary.values():
        matched = matched_tasks[item["comparison_phase"]]
        item["matched_task_mean_score"] = (
            statistics.mean(item["per_task"][task]["mean_score"] for task in matched)
            if matched
            else None
        )
    output_json(
        destination,
        {
            "label": args.label,
            "matched_available_tasks_by_phase": matched_tasks,
            "summary": summary,
            "trials": rows,
        },
    )
    lines = [
        f"# WildClawBench {args.label}",
        "",
        f"Model: `{MANIFEST['model']}`; reasoning: `{MANIFEST['reasoning']}`; requested service: Fast.",
        "",
        f"Completed trial records: {len(rows)}. This report includes only the selected jobs.",
        "",
        "| Agent | Mean score | Full-score attempts | Valid scored trials | Exceptions | Grading issues | Median seconds |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for agent, item in summary.items():
        score = (
            f"{item['mean_score'] * 100:.1f}%"
            if item["mean_score"] is not None
            else "N/A"
        )
        duration = (
            f"{item['median_seconds']:.1f}"
            if item["median_seconds"] is not None
            else "N/A"
        )
        lines.append(
            f"| {agent} | {score} | {item['fully_satisfied_attempts']} | "
            f"{item['valid_scored_trials']}/{item['trials']} | {item['exceptions']} | "
            f"{item['grader_errors']} | {duration} |"
        )
    lines.extend(
        [
            "",
            "Mean scores retain missing-output failures, native turn failures, and upstream scores for agent timeouts. "
            "They exclude infrastructure exceptions, grading issues, and unverified inference settings. "
            "Inspect those counts and task coverage before comparing agents. Full score means an upstream overall score of 1.0.",
            "",
            "Token and dollar totals cover valid scored trials only. Missing accounting remains null in the JSON report. "
            "Native cost estimates are not billed costs.",
            "",
            "Fast is a requested inference setting. Available returned service tiers are preserved in the JSON report; "
            "a successful request alone does not confirm Fast scheduling.",
            "",
            "Task prompts and grading criteria are unchanged. Each job's provenance records its dataset revision, "
            "runtime sources, image identity, and environment adjustments.",
        ]
    )
    lines.extend(
        [
            "",
            "Matched means use the same tasks for all three agents within each configuration phase. "
            "They use available valid attempts; consult per-task attempt counts in the JSON report. "
            "Authentication, judge, and task-skill registration changes make cross-phase scores unsuitable for a single ranking.",
            "",
            "| Agent | Mean over matched tasks |",
            "| --- | ---: |",
        ]
    )
    for agent, item in summary.items():
        score = item["matched_task_mean_score"]
        formatted = f"{score * 100:.1f}%" if score is not None else "N/A"
        lines.append(f"| {agent} | {formatted} |")
    for phase, tasks in matched_tasks.items():
        lines.extend(["", f"Matched tasks for {phase}: {len(tasks)}", ""])
        lines.extend(f"- `{task}`" for task in tasks)
    (args.output / f"{args.label}-report.md").write_text("\n".join(lines) + "\n")
    print(json.dumps(summary, indent=2))
    print(f"Detailed results: {destination}")


def main() -> None:
    """Expose separate preparation, paid execution, and read-only reporting steps."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "run", "resume", "report"))
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--agent", choices=tuple(AGENTS))
    parser.add_argument("--task", action="append")
    parser.add_argument("--attempts", type=int)
    parser.add_argument("--label", choices=("smoke", "pilot"), default="pilot")
    parser.add_argument(
        "--job", help="Report a single job without mixing repeated runs"
    )
    args = parser.parse_args()
    {"prepare": prepare, "run": run, "resume": resume, "report": report}[args.action](
        args
    )


if __name__ == "__main__":
    main()
