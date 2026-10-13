"""Preserve PinchBench fixtures, budgets and grading in isolated native trials."""

import asyncio
import importlib
import json
import shutil
import sys
from uuid import uuid4
from datetime import datetime, timezone
from pathlib import Path

import toml
from harbor.models.task.config import TaskConfig

from scripts.benchmarks import balanced

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "environments/pinchbench"
WORKSPACE = "/task_workspace"
IMAGE = "leon-comparison-pinch:819384ae"
DEPENDENCIES = "/opt/pinch-dependencies"
AGENTS = {
    "leon": "scripts.benchmarks.pinch_agents:PinchLeon",
    "openclaw": "scripts.benchmarks.pinch_agents:PinchOpenClaw",
    "hermes": "scripts.benchmarks.pinch_agents:PinchHermes",
}


def load_tasks():
    """Load the pinned manifest through the benchmark's own parser."""
    sys.path.insert(0, str(SOURCE / "scripts"))
    loader = importlib.import_module("lib_tasks").TaskLoader(SOURCE / "tasks")
    return {task.task_id: task for task in loader.load_all_tasks()}


def task_for_environment(environment):
    """Read private host-side prompts without mounting future turns in a trial."""
    if environment.task_env_config.env.get("PINCHBENCH_PREFLIGHT"):
        loader = importlib.import_module("lib_tasks").TaskLoader(
            environment.environment_dir.parent
        )
        return loader.load_task(
            environment.environment_dir.parent / "pinch-native-session-smoke.md"
        )
    return load_tasks()[environment.task_env_config.env["PINCHBENCH_TASK"]]


def prepare(output: Path, item: dict, task) -> Path:
    """Expose declared inputs only; Harbor uploads graders after agent execution."""
    if balanced.digest(task.file_path) != item["sha256"]:
        raise RuntimeError(f"Frozen PinchBench task changed: {item['name']}")
    if task.timeout_seconds != item["timeout_seconds"]:
        raise RuntimeError("PinchBench's native execution budget changed")
    target = output / "tasks/pinch" / item["name"]
    receipt = target / "source.json"
    if receipt.exists():
        for name, digest in json.loads(receipt.read_text())["prepared_files"].items():
            if balanced.digest(target / name) != digest:
                raise RuntimeError(f"Prepared PinchBench fixture changed: {name}")
        return target

    environment = target / "environment"
    workspace = environment / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    fixtures = {}
    for spec in task.workspace_files:
        name = spec["path"] if "content" in spec else spec["dest"]
        destination = workspace / name
        if not destination.resolve().is_relative_to(workspace.resolve()):
            raise RuntimeError("PinchBench fixture escapes its workspace")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if "content" in spec:
            destination.write_text(spec["content"])
        else:
            shutil.copyfile(SOURCE / "assets" / spec["source"], destination)
        fixtures[name] = balanced.digest(destination)

    environment.joinpath("Dockerfile").write_text(
        f"FROM {IMAGE}\nWORKDIR {WORKSPACE}\nCOPY workspace/ {WORKSPACE}/\n"
    )
    sessions = task.frontmatter.get("sessions") or [{"prompt": task.prompt}]
    first = sessions[0]
    target.joinpath("instruction.md").write_text(
        first if isinstance(first, str) else first["prompt"]
    )
    tests = target / "tests"
    tests.mkdir(exist_ok=True)
    (tests / "tasks").mkdir(exist_ok=True)
    shutil.copyfile(task.file_path, tests / "tasks" / task.file_path.name)
    shutil.copytree(
        SOURCE / "scripts",
        tests / "scripts",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    private_key = SOURCE / "assets/image_classification_answer_key.json"
    if private_key.exists():
        (tests / "assets").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(private_key, tests / "assets" / private_key.name)
    (tests / "test.sh").write_text(
        "#!/bin/bash\nset -eu\n"
        "export LEON_BENCHMARK_AUDIT_PATH=/logs/verifier/requests.jsonl\n"
        "python3 /opt/leon/scripts/benchmarks/pinch_grader.py\n"
    )
    config = {
        "version": "1.0",
        "agent": {"timeout_sec": task.timeout_seconds},
        "verifier": {"timeout_sec": 660},
        "environment": {
            "build_timeout_sec": 600,
            "cpus": 2,
            "memory_mb": 4_096,
            "storage_mb": 10_240,
            "env": {
                "LEON_BENCHMARK_SUITE": "pinch",
                "PINCHBENCH_TASK": item["name"],
                "PINCHBENCH_WORKSPACE": WORKSPACE,
            },
        },
        "artifacts": [{"source": WORKSPACE, "destination": "workspace"}],
    }
    TaskConfig.model_validate(config)
    (target / "task.toml").write_text(toml.dumps(config))
    balanced.save(
        receipt,
        {
            "source_sha256": item["sha256"],
            "fixtures": fixtures,
            "sessions": len(sessions),
            "native_timeout_seconds": task.timeout_seconds,
            "grader_visibility": "Uploaded only after candidate execution",
            "prepared_files": {
                str(p.relative_to(target)): balanced.digest(p)
                for p in target.rglob("*")
                if p.is_file()
            },
        },
    )
    return target


def replace_once(path: Path, before: str, after: str) -> None:
    """Reject incompatible runner layouts rather than silently losing sessions."""
    text = path.read_text()
    if text.count(before) != 1:
        raise RuntimeError(f"Cannot stage native multi-session runner: {path.name}")
    path.write_text(text.replace(before, after))


def copy_runtime_file(source, destination):
    """Preserve shared dependency hardlinks while refreshing distinct runtime files."""
    target = Path(destination)
    if target.exists() and Path(source).samefile(target):
        return str(target)

    return shutil.copy2(source, destination)


def stage_runtime(output: Path) -> Path:
    """Stage phase-specific wrappers without mutating active suite mounts."""
    target = output / "pinch-runtime/benchmarks"
    if (output / "pinch-runtime.json").exists():
        receipt = json.loads((output / "pinch-runtime.json").read_text())
        for name, digest in receipt["files"].items():
            if balanced.digest(target / name) != digest:
                raise RuntimeError(f"Frozen PinchBench runner changed: {name}")
        return target
    shutil.copytree(
        output / "runtime/benchmarks",
        target,
        dirs_exist_ok=True,
        copy_function=copy_runtime_file,
    )
    for name in ("pinch_grader.py", "pinch_transcript.py", "pinch_mock.py"):
        shutil.copyfile(Path(__file__).with_name(name), target / name)
    leon = target / "run-leon.ts"
    replace_once(
        leon,
        "const sessionId = CONVERSATION_SESSION_MANAGER.createSession().id",
        """const requestedSessionId = process.env['LEON_BENCHMARK_SESSION_ID']
  if (requestedSessionId && !CONVERSATION_SESSION_MANAGER.getSession(requestedSessionId)) {
    throw new Error('The requested native conversation session is unavailable')
  }
  const sessionId = requestedSessionId || CONVERSATION_SESSION_MANAGER.createSession().id""",
    )
    replace_once(
        leon,
        "path.join(outputDirectory, 'requests.jsonl')",
        "process.env['LEON_BENCHMARK_AUDIT_PATH'] || path.join(outputDirectory, 'requests.jsonl')",
    )
    hermes = target / "run-hermes.py"
    replace_once(
        hermes,
        'OUTPUT_DIRECTORY = Path("/logs/agent")',
        'OUTPUT_DIRECTORY = Path(os.environ.get("PINCHBENCH_TURN_OUTPUT", "/logs/agent"))',
    )
    exporter = target / "export-openclaw-transcript.mjs"
    replace_once(
        exporter,
        "for (const session of sessions.sessions) {",
        """for (const session of sessions.sessions) {
  if (process.env.PINCHBENCH_EXPORT_SESSION_ID &&
      session.sessionId !== process.env.PINCHBENCH_EXPORT_SESSION_ID) {
    continue
  }""",
    )
    replace_once(
        hermes,
        'result = agent.run_conversation(os.environ["HERMES_INSTRUCTION"])',
        """history_path = os.environ.get("PINCHBENCH_CONVERSATION_HISTORY")
        history = json.loads(Path(history_path).read_text())["messages"] if history_path else None
        result = agent.run_conversation(
            os.environ["HERMES_INSTRUCTION"], conversation_history=history
        )""",
    )
    settings = json.loads((target / "pilot.json").read_text())
    settings["task_skills_dir"] = f"{WORKSPACE}/skills"
    balanced.save(target / "pilot.json", settings)
    balanced.save(
        output / "pinch-runtime.json",
        {
            "phase": "pinch",
            "activated": False,
            "native_agent_sources": "Frozen campaign versions, unchanged",
            "session_policy": "Native conversation APIs; new_session clears history and preserves files",
            "files": {
                str(p.relative_to(target)): balanced.digest(p)
                for p in target.iterdir()
                if p.is_file()
            },
        },
    )
    return target


async def native_preflight(output: Path, selection: dict, baseline: set[str]):
    """Validate real session reuse and fresh-session file access before scoring."""
    import yaml

    receipt = output / "preflight/pinch-native-sessions.json"
    if receipt.exists():
        if not json.loads(receipt.read_text()).get("passed"):
            raise RuntimeError("PinchBench's saved native session preflight failed")
        return
    directory = output / "preflight/pinch-session-spec"
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / "pinch-native-session-smoke.md"
    if not source.exists():
        secret = uuid4().hex
        frontmatter = {
            "id": source.stem,
            "name": "Native conversation and workspace preflight",
            "category": "preflight",
            "grading_type": "automated",
            "timeout_seconds": 180,
            "workspace_files": [],
            "sessions": [
                {
                    "prompt": f"Remember my codeword: {secret}. Create session-contract.txt containing exactly that codeword. Reply with the codeword."
                },
                {
                    "prompt": "What is my codeword from our conversation? Reply with it. Do not read files for this turn."
                },
                {
                    "new_session": True,
                    "prompt": "Read session-contract.txt in the current directory and reply with its contents.",
                },
            ],
        }
        grader = """def grade(transcript, workspace_path):
    import json
    from pathlib import Path
    logs = Path("/logs/agent")
    turns = json.loads((logs / "pinch-turns.json").read_text())["turns"]
    assert len(turns) == 3 and all(row["completed"] for row in turns)
    folders = sorted((logs / "turns").iterdir())
    assert (Path(workspace_path) / "session-contract.txt").read_text().strip() == SECRET
    leon = [folder / "leon-result.json" for folder in folders]
    hermes = [folder / "hermes-result.json" for folder in folders]
    if all(path.exists() for path in leon):
        records = [json.loads(path.read_text()) for path in leon]
        ids = [record["sessionId"] for record in records]
        assert ids[0] == ids[1] and ids[1] != ids[2]
        assert all(SECRET in json.dumps(record["result"]) for record in records)
    elif all(path.exists() for path in hermes):
        records = [json.loads(path.read_text()) for path in hermes]
        assert records[1]["current_turn_user_idx"] > records[0]["current_turn_user_idx"]
        assert records[2]["current_turn_user_idx"] == records[0]["current_turn_user_idx"]
        assert all(SECRET in record.get("final_response", "") for record in records)
    else:
        histories = [[json.loads(line) for line in (folder / "native-transcript.jsonl").read_text().splitlines() if line.strip()] for folder in folders]
        counts = [sum(row.get("message", {}).get("role") == "user" for row in history) for history in histories]
        assert counts[1] > counts[0] and counts[2] == counts[0]
        for history in histories:
            assistants = [row for row in history if row.get("message", {}).get("role") == "assistant"]
            assert assistants and SECRET in json.dumps(assistants[-1])
    return {"native_sessions_and_workspace": 1.0}
"""
        source.write_text(
            "---\n" + yaml.safe_dump(frontmatter, sort_keys=False) + "---\n\n"
            "## Prompt\n\n" + frontmatter["sessions"][0]["prompt"] + "\n\n"
            "## Automated Checks\n\n```python\nSECRET = "
            + repr(secret)
            + "\n"
            + grader
            + "```\n"
        )
    loader = importlib.import_module("lib_tasks").TaskLoader(directory)
    task = loader.load_task(source)
    item = {
        "name": source.stem,
        "domain": "preflight",
        "sha256": balanced.digest(source),
        "timeout_seconds": task.timeout_seconds,
    }
    target = prepare(output, item, task)
    shutil.copyfile(source, target / source.name)
    config = toml.load(target / "task.toml")
    config["environment"]["env"]["PINCHBENCH_PREFLIGHT"] = "1"
    (target / "task.toml").write_text(toml.dumps(config))
    source_receipt = json.loads((target / "source.json").read_text())
    source_receipt["prepared_files"]["task.toml"] = balanced.digest(
        target / "task.toml"
    )
    source_receipt["prepared_files"][source.name] = balanced.digest(
        target / source.name
    )
    balanced.save(target / "source.json", source_receipt)
    results = await balanced.execute(
        output,
        selection,
        "pinch",
        item,
        "session-smoke",
        baseline,
        agents=AGENTS,
        attempts=1,
        release_images=False,
    )
    valid = len(results) == 3 and all(
        not row.get("exception_info")
        and ((row.get("verifier_result") or {}).get("rewards") or {}).get("reward") == 1
        for row in results
    )
    job = output / "jobs" / f"pinch--{item['name']}" / "session-smoke"
    evidence = []
    for row in results:
        requests_path = job / row["trial_name"] / "agent/requests.jsonl"
        requests = (
            [json.loads(line) for line in requests_path.read_text().splitlines()]
            if requests_path.exists()
            else []
        )
        verified = bool(requests) and all(
            request.get("model") == "gpt-6.1-sol"
            and (request.get("reasoning") or {}).get("effort") == "high"
            and request.get("serviceTier") == "priority"
            and request.get("authMode") == "chatgpt_subscription"
            and request.get("endpoint", "").startswith(
                "https://chatgpt.com/backend-api/codex/"
            )
            for request in requests
        )
        evidence.append(
            {"agent": row["agent_info"]["name"], "settings_verified": verified}
        )
    valid = valid and all(row["settings_verified"] for row in evidence)
    balanced.save(
        receipt,
        {
            "passed": valid,
            "candidate_slots": 0,
            "agents": [row["agent_info"]["name"] for row in results],
            "inference_checks": evidence,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    await asyncio.to_thread(
        balanced.cleanup_job,
        output,
        output / "jobs" / f"pinch--{item['name']}" / "session-smoke",
        True,
        True,
    )
    if not valid:
        raise RuntimeError(
            "PinchBench native session preflight failed; candidate runs held"
        )
    shutil.rmtree(target / "environment")


async def run(output: Path, selection: dict, state: dict) -> None:
    """Execute one frozen task cohort at a time, then retire its resources."""
    from scripts.benchmarks.campaign import quota, rotate
    from scripts.benchmarks import metrics
    from scripts.benchmarks.fairness_audit import require_review

    require_review(output, selection, "wild")

    if not (output / "pinch-preparation.json").exists():
        raise RuntimeError(
            "PinchBench's native adapter preflight must pass before execution"
        )
    preparation = json.loads((output / "pinch-preparation.json").read_text())
    if (
        not preparation.get("fixtures_verified")
        or preparation.get("mock_prerequisite_preflight") != "passed"
    ):
        raise RuntimeError(
            "PinchBench fixtures and prerequisites are not yet validated"
        )
    importlib.reload(metrics)
    tasks = load_tasks()
    runtime = stage_runtime(output)
    mounts_path = output / "mounts.json"
    original = json.loads(mounts_path.read_text())
    mounts = [dict(mount) for mount in original]
    for mount in mounts:
        if mount["target"] == "/opt/leon/scripts/benchmarks":
            mount["source"] = str(runtime)
    mounts.append(
        {
            "type": "bind",
            "source": str(output / "pinch-dependencies"),
            "target": DEPENDENCIES,
            "read_only": True,
        }
    )
    balanced.save(mounts_path, mounts)
    baseline = {row["Id"] for row in balanced.docker_objects("image")}
    try:
        state.update(
            status="native session preflight",
            suite="pinch",
            task="pinch-native-session-smoke",
        )
        balanced.save(output / "status.json", state)
        await native_preflight(output, selection, baseline)
        preparation.update(ready=True, native_session_preflight="passed")
        balanced.save(output / "pinch-preparation.json", preparation)
        for item in rotate(selection["pinch"]):
            group = output / "jobs" / f"pinch--{item['name']}"
            if (group / "complete.json").exists():
                continue
            state.update(status="preparing task", suite="pinch", task=item["name"])
            balanced.save(output / "status.json", state)
            await quota(output, state)
            await asyncio.to_thread(prepare, output, item, tasks[item["name"]])
            state["status"] = "agent execution"
            balanced.save(output / "status.json", state)
            results = await balanced.execute(
                output,
                selection,
                "pinch",
                item,
                "native",
                baseline,
                agents=AGENTS,
                attempts=3,
                release_images=False,
            )
            snapshot = balanced.report(output, selection)
            rows = [
                row
                for row in snapshot["trials"]
                if row["suite"] == "pinch" and row["task"] == item["name"]
            ]
            if (
                len(results) != 9
                or len(rows) != 9
                or any(row["score"] is None for row in rows)
            ):
                raise RuntimeError(
                    "PinchBench cohort requires configuration or grading review"
                )
            balanced.save(
                group / "complete.json",
                {"attempts": 9, "finished_at": datetime.now(timezone.utc).isoformat()},
            )
            await asyncio.to_thread(
                balanced.cleanup_job, output, group / "native", True, True
            )
            shutil.rmtree(output / "tasks/pinch" / item["name"] / "environment")
        balanced.save(
            output / "pinch-complete.json",
            {"finished_at": datetime.now(timezone.utc).isoformat()},
        )
        balanced.command(["docker", "image", "rm", IMAGE])
        shutil.rmtree(output / "pinch-dependencies/node_modules")
    finally:
        balanced.save(mounts_path, original)
