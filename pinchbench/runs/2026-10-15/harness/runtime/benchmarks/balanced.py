"""Run frozen, domain-balanced native comparisons with task-scoped cleanup."""

import argparse
import asyncio
import collections
import concurrent.futures
import fcntl
import hashlib
import importlib
import json
import os
import selectors
import shutil
import signal
import statistics
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import toml
import yaml
from harbor.job import Job
from harbor.models.job.config import JobConfig
from harbor.environments.definition import environment_content_hash

from scripts.benchmarks.regrading import expected_output_failure, verified_receipt

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
benchmark = importlib.import_module("scripts.benchmarks.benchmark")
resolve_saved_path = importlib.import_module(
    "scripts.benchmarks.paths"
).resolve_saved_path
scheduling = importlib.import_module("scripts.benchmarks.scheduling")
PILOT = ROOT / "tmp/benchmarks/comparison"
DEFAULT_OUTPUT = ROOT / "tmp/benchmarks/balanced"
GIB = 1_024**3
DISK_RESERVE = 5 * GIB
EMERGENCY_DISK_RESERVE = 3 * GIB
QUOTA_RESERVE_PERCENT = 15
ORACLE_ATTEMPTS = 5
BUILDER_PREFIX = "leon-balanced-"
PROXY_NAMES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)
OVERLAY_FILES = ("run-leon.ts", "run-judge.py")


def save(path: Path, value: object) -> None:
    """Replace state atomically so interruption cannot leave partial metadata."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".new")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def digest(path: Path) -> str:
    """Hash evidence without loading large task fixtures into memory."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def command(args: list[str], check: bool = True) -> str:
    """Run host operations with bounded waits and captured diagnostic output."""
    result = subprocess.run(
        args, check=check, capture_output=True, text=True, timeout=90
    )
    return result.stdout.strip()


def docker_objects(kind: str) -> list[dict]:
    """Inspect exact resource identities rather than relying on broad pruning."""
    args = (
        ["docker", "ps", "-aq"] if kind == "container" else ["docker", kind, "ls", "-q"]
    )
    identifiers = command(args).split()
    if not identifiers:
        return []
    # Concurrent cleanup may remove listed objects before inspection. Docker
    # still returns the surviving objects as JSON when an identifier is gone.
    return json.loads(
        command(["docker", "inspect", *sorted(set(identifiers))], check=False)
    )


def rate_limits() -> dict:
    """Read subscription allowance through Codex without making inference calls."""
    process = subprocess.Popen(
        ["codex", "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:

        def send(value):
            process.stdin.write(json.dumps(value) + "\n")
            process.stdin.flush()

        def receive(identifier):
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                if not selector.select(timeout=1):
                    continue
                line = process.stdout.readline()
                if not line:
                    raise RuntimeError("Codex allowance reader exited")
                value = json.loads(line)
                if value.get("id") == identifier:
                    if "error" in value:
                        raise RuntimeError(
                            "Codex could not read subscription allowance"
                        )
                    return value["result"]
            raise TimeoutError("Codex allowance read timed out")

        send(
            {
                "id": 1,
                "method": "initialize",
                "params": {"clientInfo": {"name": "leon-benchmark", "version": "1"}},
            }
        )
        receive(1)
        send({"method": "initialized"})
        send({"id": 2, "method": "account/rateLimits/read"})
        return receive(2)
    finally:
        selector.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def quota_available(value: dict) -> bool:
    """Leave an allowance reserve for the owner and tasks already in flight."""
    if value.get("ordinaryUsageAllowed") is False:
        return False
    limits = value.get("rateLimitsByLimitId") or {
        "default": value.get("rateLimits", {})
    }
    relevant = [v for k, v in limits.items() if "codex" in k.lower() or k == "default"]
    if not relevant:
        relevant = list(limits.values())
    windows = [
        v[name] for v in relevant for name in ("primary", "secondary") if v.get(name)
    ]
    if not windows:
        raise RuntimeError("Subscription allowance response has no usable windows")
    return all(w["usedPercent"] < 100 - QUOTA_RESERVE_PERCENT for w in windows)


def initialize(output: Path) -> dict:
    """Freeze selection, evidence references, and runtime overlays before execution."""
    selection = json.loads((output / "selection.json").read_text())
    for suite in ("wild", "terminal"):
        tasks = selection[suite]
        assert tasks and len({task["name"] for task in tasks}) == len(tasks)
        quotas = selection.get("domain_quotas", {}).get(suite)
        if quotas:
            assert dict(collections.Counter(task["domain"] for task in tasks)) == quotas

    assert selection["total_agent_runs"] == (
        sum(len(selection[suite]) for suite in ("wild", "terminal"))
        * len(benchmark.AGENTS)
        * benchmark.MANIFEST["attempts"]
    )
    assert selection["new_agent_runs"] == (
        selection["total_agent_runs"] - selection["retained_agent_runs"]
    )
    assert {x["name"] for x in selection["wild"] if x["retained"]} == set(
        benchmark.MANIFEST["tasks"]
    )
    provenance_path = output / "provenance.json"
    if provenance_path.exists():
        provenance = json.loads(provenance_path.read_text())
        if digest(output / "selection.json") != provenance["selection_sha256"]:
            raise RuntimeError("Frozen task selection changed")
        for path, expected in provenance["frozen_files"].items():
            if digest(resolve_saved_path(path)) != expected:
                raise RuntimeError(f"Frozen runtime or evidence changed: {path}")
        return selection

    plan = json.loads((PILOT / "recovery-plan.json").read_text())
    for path, expected in plan["frozen_inputs"].items():
        if digest(PILOT / path) != expected:
            raise RuntimeError(f"Pilot input changed: {path}")
    retained = (
        json.loads(resolve_saved_path(selection["retained_report"]).read_text())[
            "trials"
        ]
        if selection.get("retained_report")
        else []
    )
    assert len(retained) == selection["retained_agent_runs"]
    assert len({x["task"] for x in retained if x.get("suite", "wild") == "wild"}) == 12
    overlay = output / "runtime/benchmarks"
    shutil.copytree(
        PILOT / "source/leon/scripts/benchmarks", overlay, dirs_exist_ok=True
    )
    for name in OVERLAY_FILES:
        shutil.copy2(ROOT / "scripts/benchmarks" / name, overlay / name)
    shutil.copy2(PILOT / "runtime.json", output / "runtime.json")
    job = json.loads((PILOT / "job.json").read_text())
    mounts = job["environment"]["mounts"] + [
        benchmark.mount(overlay, "/opt/leon/scripts/benchmarks")
    ]
    save(output / "mounts.json", mounts)
    nameservers = [
        line.split()[1]
        for line in Path("/etc/resolv.conf").read_text().splitlines()
        if line.startswith("nameserver ") and not line.split()[1].startswith("127.")
    ]
    if not nameservers:
        raise RuntimeError("A container-accessible host DNS server is required")
    (output / "network.yaml").write_text(
        yaml.safe_dump({"services": {"main": {"dns": nameservers}}})
    )
    frozen_files = {str(p): digest(p) for p in overlay.rglob("*") if p.is_file()}
    for p in [
        resolve_saved_path(selection["retained_report"]),
        output / "runtime.json",
        output / "mounts.json",
        output / "network.yaml",
        ROOT / "scripts/benchmarks/harbor_agents.py",
        Path(__file__),
        ROOT / "scripts/benchmarks/load-wild-image.py",
    ]:
        frozen_files[str(p)] = digest(p)
    for row in retained:
        path = resolve_saved_path(row["trial"]) / "result.json"
        frozen_files[str(path)] = digest(path)
    save(
        provenance_path,
        {
            "selection_sha256": digest(output / "selection.json"),
            "frozen_files": frozen_files,
            "native_sources": plan["frozen_inputs"],
            "pilot_provenance": json.loads((PILOT / "provenance.json").read_text()),
            "overlay_files": list(OVERLAY_FILES),
            "overlay_purpose": "Task workspace support and completed-stream judge text reconstruction; native agent sources remain the pilot snapshots.",
            "cleanup_policy": "Delete trial containers after artifact export. Remove task-owned images, builder/cache, volumes, networks and downloadable fixture directories after all task runs. Preserve logs, scores, instructions, graders and file hashes.",
            "fast_policy": "priority requested; record actual returned tier; default is not confirmed Fast service",
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    return selection


def prepared_task(target: Path, receipt: Path) -> bool:
    """Reuse fixtures only when every prepared file is present and unchanged."""
    if not receipt.exists() or not (target / "environment").is_dir():
        return False
    files = json.loads(receipt.read_text()).get("prepared_files", {})
    return bool(files) and all(
        (target / name).is_file() and digest(target / name) == expected
        for name, expected in files.items()
    )


def download(output: Path, suite: str, name: str, revision: str) -> Path:
    """Fetch one immutable task at a time, retaining source hashes for cleanup."""
    target = output / "tasks" / suite / name
    receipt = output / "inputs" / f"{suite}-{name}.json"
    if prepared_task(target, receipt):
        return target
    files = []
    if suite == "terminal":
        tree = json.loads((output / "terminal-tree.json").read_text())["tree"]
        prefix = f"tasks/{name}/"
        files = [
            {
                "path": x["path"][len(prefix) :],
                "url": f"https://raw.githubusercontent.com/harbor-framework/terminal-bench/{revision}/{urllib.parse.quote(x['path'], safe='/')}",
                "size": x["size"],
                "git_blob": x["sha"],
            }
            for x in tree
            if x["type"] == "blob" and x["path"].startswith(prefix)
        ]
    else:
        url = f"https://huggingface.co/api/datasets/{benchmark.MANIFEST['dataset']}/tree/{revision}/{name}?recursive=true&limit=1000"
        while url:
            with urllib.request.urlopen(url, timeout=90) as response:
                for x in json.load(response):
                    if x["type"] == "file":
                        files.append(
                            {
                                "path": x["path"][len(name) + 1 :],
                                "url": f"https://huggingface.co/datasets/{benchmark.MANIFEST['dataset']}/resolve/{revision}/{urllib.parse.quote(x['path'], safe='/')}",
                                "size": x["size"],
                                "lfs": x.get("lfs"),
                            }
                        )
                link = response.headers.get("Link", "")
                url = next(
                    (
                        p.split("<", 1)[1].split(">", 1)[0]
                        for p in link.split(",")
                        if 'rel="next"' in p
                    ),
                    "",
                )
    if shutil.disk_usage(output).free < sum(x["size"] for x in files) + DISK_RESERVE:
        raise RuntimeError(f"Not enough disk to download {name} safely")

    def fetch(item):
        path = target / item["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.stat().st_size != item["size"]:
            temporary = path.with_suffix(path.suffix + ".part")
            with (
                urllib.request.urlopen(item["url"], timeout=120) as response,
                temporary.open("wb") as stream,
            ):
                shutil.copyfileobj(response, stream, length=1_048_576)
            temporary.replace(path)
        if path.stat().st_size != item["size"]:
            raise RuntimeError(f"Incomplete fixture: {path}")
        checksum = digest(path)
        if item.get("git_blob"):
            blob = hashlib.sha1(
                b"blob " + str(path.stat().st_size).encode() + b"\0" + path.read_bytes()
            ).hexdigest()
            if blob != item["git_blob"]:
                raise RuntimeError(f"Upstream blob identity mismatch: {path}")
        if item.get("lfs") and checksum != item["lfs"]["oid"]:
            raise RuntimeError(f"Upstream fixture identity mismatch: {path}")
        return {"path": item["path"], "size": item["size"], "sha256": checksum}

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        identities = list(pool.map(fetch, files))
    (target / "environment").mkdir(exist_ok=True)
    adaptations = []
    config = toml.loads((target / "task.toml").read_text())
    if suite == "wild":
        for skill in (target / "environment").rglob("SKILL.md"):
            content = skill.read_text()
            for original, alias in benchmark.MANIFEST[
                "task_skill_name_aliases"
            ].items():
                content = content.replace(f"name: {original}", f"name: {alias}")
            skill.write_text(content)
        for script in target.rglob("*.sh"):
            content = script.read_bytes()
            if b"\r\n" in content:
                script.write_bytes(content.replace(b"\r\n", b"\n"))
                adaptations.append(
                    {"shell_line_endings": str(script.relative_to(target))}
                )
            if script.name == "run-warmup.sh":
                content = script.read_text()
                for original, replacement in benchmark.WARMUP_COMMANDS.items():
                    content = content.replace(original, replacement)
                script.write_text(content)
        config["environment"]["docker_image"] = benchmark.DEFAULT_IMAGE
        for grader in (target / "tests").rglob("*.py"):
            grader.write_text(
                grader.read_text().replace("OPENROUTER", "CODEX_SUBSCRIPTION")
            )
        entry = target / "tests/test.sh"
        entry.write_text(
            entry.read_text().replace(
                "python3 /tests/grader.py",
                "python3 /opt/leon/scripts/benchmarks/run-judge.py",
            )
        )
        env = config["environment"].setdefault("env", {})
        for key in list(env):
            if key.startswith("OPENROUTER_"):
                del env[key]
        env["JUDGE_MODEL"] = benchmark.MANIFEST["model"].split("/", 1)[1]
        if (
            config["environment"].get("healthcheck")
            and not (target / "environment/.wildclaw/run-warmup.sh").exists()
        ):
            adaptations.append(
                {"missing_healthcheck": config["environment"].pop("healthcheck")}
            )
        adaptations.append(
            {
                "judge_transport": "chatgpt_subscription_responses",
                "judge_model": benchmark.MANIFEST["model"],
            }
        )
        from scripts.benchmarks.safety_fixture_grader import prepare

        fixture_repair = prepare(target)
        if fixture_repair:
            adaptations.append({"initial_secret_fixture_validation": fixture_repair})
    else:
        config["environment"].setdefault("env", {})["LEON_BENCHMARK_SUITE"] = "terminal"
        adaptations.append(
            {
                "native_runtime": "managed Node for assistants; task workdir and resource limits preserved"
            }
        )
    selection_file = output / "selection.json"
    if selection_file.exists():
        selection = json.loads(selection_file.read_text())
        item = next(
            (item for item in selection.get(suite, []) if item["name"] == name),
            {},
        )
        additional_artifacts = item.get("additional_artifacts", [])
        for artifact in additional_artifacts:
            # A parent snapshot already contains child outputs. Export each
            # source once so nested destinations cannot overwrite one another.
            config["artifacts"] = [
                source
                for source in config.setdefault("artifacts", [])
                if not isinstance(source, str)
                or source == artifact
                or not Path(source).is_relative_to(Path(artifact))
            ]
            if artifact not in config.setdefault("artifacts", []):
                config["artifacts"].append(artifact)
        if additional_artifacts:
            adaptations.append({"additional_artifact_exports": additional_artifacts})

    (target / "task.toml").write_text(toml.dumps(config))
    save(
        receipt,
        {
            "revision": revision,
            "original_files": identities,
            "adaptations": adaptations,
            "prepared_files": {
                str(p.relative_to(target)): digest(p)
                for p in target.rglob("*")
                if p.is_file()
            },
        },
    )
    return target


def capture_resources(output: Path, job_dir: Path, baseline: set[str]) -> dict:
    """Record resources linked to this job's trial mounts or Compose projects."""
    receipt = job_dir / "resources.json"
    owned = (
        json.loads(receipt.read_text())
        if receipt.exists()
        else {"projects": [], "images": [], "baseline_images": sorted(baseline)}
    )
    containers = docker_objects("container")
    for c in containers:
        if any(
            m.get("Source", "").startswith(str(job_dir) + "/")
            for m in c.get("Mounts", [])
        ):
            project = (c["Config"].get("Labels") or {}).get(
                "com.docker.compose.project"
            )
            if project and project not in owned["projects"]:
                owned["projects"].append(project)
    for c in containers:
        project = (c["Config"].get("Labels") or {}).get("com.docker.compose.project")
        if (
            (
                project in owned["projects"]
                or c["Name"]
                == "/buildx_buildkit_" + os.environ.get("BUILDX_BUILDER", "") + "0"
            )
            and c["Image"] not in owned["baseline_images"]
            and c["Image"] not in owned["images"]
        ):
            owned["images"].append(c["Image"])
    save(receipt, owned)
    return owned


def cleanup_job(
    output: Path, job_dir: Path, final: bool = False, release_images: bool = False
) -> None:
    """Clean only proven task-owned resources, retaining immutable trial evidence."""
    receipt = job_dir / "resources.json"
    if not receipt.exists():
        return
    owned = json.loads(receipt.read_text())
    completed = {p.parent.name for p in job_dir.glob("*/result.json")}
    removed = []
    for c in docker_objects("container"):
        project = (c["Config"].get("Labels") or {}).get("com.docker.compose.project")
        if project in owned["projects"] and (
            final or any(project.startswith(name.lower() + "__") for name in completed)
        ):
            command(["docker", "rm", "-f", c["Id"]])
            removed.append(c["Id"])
    if final:
        for kind in ("network", "volume"):
            for resource in docker_objects(kind):
                if (resource.get("Labels") or {}).get(
                    "com.docker.compose.project"
                ) in owned["projects"]:
                    command(
                        ["docker", kind, "rm", resource.get("Id") or resource["Name"]]
                    )
        if release_images:
            for image in owned["images"]:
                command(["docker", "image", "rm", image], check=False)
    with (output / "cleanup.jsonl").open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "job": job_dir.name,
                    "final": final,
                    "removed_containers": removed,
                    "owned": owned,
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            + "\n"
        )


def report(output: Path, selection: dict) -> dict:
    """Serialize snapshots so concurrent lanes cannot overwrite newer progress."""
    with (output / "report.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return build_report(output, selection)


def build_report(output: Path, selection: dict) -> dict:
    """Compare matched tasks with equal domain weights and visible exclusions."""
    rows = []
    retained = (
        json.loads(resolve_saved_path(selection["retained_report"]).read_text())[
            "trials"
        ]
        if selection.get("retained_report")
        else []
    )
    for row in retained:
        valid = row["inference_settings_verified"] and not row["grader_error"]
        suite = row.get("suite", "wild")
        rows.append(
            {
                "suite": suite,
                "task": row["task"],
                "agent": row["agent"],
                "score": (row["rewards"] or {}).get(
                    "overall_score" if suite == "wild" else "reward"
                )
                if valid
                else None,
                "trial": str(resolve_saved_path(row["trial"])),
                "retained": True,
                "exception": row["exception"],
            }
        )
    lifecycle_path = output / "hermes-lifecycle-audit.json"
    lifecycle = (
        json.loads(lifecycle_path.read_text()) if lifecycle_path.exists() else {}
    )
    repair_path = output / "environment-repair-audit.json"
    if repair_path.exists():
        repairs = json.loads(repair_path.read_text())
        lifecycle = {
            "affected_trials": [
                *lifecycle.get("affected_trials", []),
                *repairs.get("affected_trials", []),
            ]
        }
    recovery_directories = {
        Path(issue["replacement_directory"])
        for issue in lifecycle.get("affected_trials", [])
        if issue.get("replacement_directory")
    }
    result_paths = list((output / "jobs").glob("*/native/*/result.json"))
    for directory in recovery_directories:
        result_paths.extend(directory.glob("*/result.json"))
    for path in result_paths:
        value = json.loads(path.read_text())
        # Dataset metadata may qualify names with a suite prefix. The frozen
        # selection and job directory share the same canonical task identifier.
        suite, task = path.parent.parent.parent.name.split("--", 1)
        evidence = path.parent / "agent/requests.jsonl"
        requests = (
            [json.loads(line) for line in evidence.read_text().splitlines()]
            if evidence.exists()
            else []
        )
        settings = (
            bool(requests)
            and all(
                x.get("model") == "gpt-6.1-sol"
                and (x.get("reasoning") or {}).get("effort") == "high"
                and x.get("serviceTier") == "priority"
                and x.get("authMode") == "chatgpt_subscription"
                and x.get("endpoint", "").startswith(
                    "https://chatgpt.com/backend-api/codex/"
                )
                for x in requests
            )
            and any(200 <= x.get("status", 0) < 300 for x in requests)
        )
        rewards = (value.get("verifier_result") or {}).get("rewards") or {}
        regrade = verified_receipt(path.parent)
        if regrade:
            rewards = regrade["rewards"]
        grade_path = path.parent / "verifier/wildclaw_score.json"
        grade = json.loads(grade_path.read_text()) if grade_path.exists() else {}
        if regrade:
            grade = regrade["rewards"]
        output_failure = expected_output_failure(output, path.parent, grade)
        issues = {
            k: v
            for k, v in grade.items()
            if (k in benchmark.GRADER_ERROR_KEYS and v and not output_failure)
            or (k.endswith("judge_method") and v in benchmark.FALLBACK_JUDGE_METHODS)
        }
        reliability_path = path.parent / "verifier/judge-reliability.json"
        if reliability_path.exists() and not regrade:
            reliability = json.loads(reliability_path.read_text())
            if reliability.get("failed_calls"):
                issues["judge_transport_failures"] = reliability["failed_calls"]
        integrity_path = path.parent / "integrity-review.json"
        if integrity_path.exists() and json.loads(integrity_path.read_text()).get(
            "requires_review"
        ):
            issues["integrity_review_pending"] = True
        exception = value.get("exception_info")
        native_returned = any(
            (path.parent / "agent" / n).exists()
            for n in benchmark.NATIVE_AGENT_RESULT_FILES
        )
        scored_exception = (
            not exception
            or exception["exception_type"] in benchmark.SCORED_AGENT_EXCEPTIONS
            or (
                exception["exception_type"] == benchmark.RETURNED_AGENT_EXCEPTION
                and native_returned
            )
        )
        score = rewards.get("overall_score" if suite == "wild" else "reward")
        if not settings or issues or not scored_exception:
            score = None
        rows.append(
            {
                "suite": suite,
                "task": task,
                "upstream_task_name": value["task_name"],
                "agent": value["agent_info"]["name"],
                "score": score,
                "trial": str(path.parent),
                "retained": False,
                "exception": exception,
                "grader_error": issues,
                "settings_verified": settings,
                "artifact_regrade": regrade,
            }
        )
    rows, superseded = apply_lifecycle_recovery(rows, lifecycle)
    selected_tasks = {
        (suite, item["name"])
        for suite in ("wild", "pinch", "terminal")
        for item in selection.get(suite, [])
    }
    withdrawn = [row for row in rows if (row["suite"], row["task"]) not in selected_tasks]
    rows = [row for row in rows if (row["suite"], row["task"]) in selected_tasks]
    budget_audit = output / "runtime-budget-audit.json"
    budget_issues = (
        {
            issue["trial_name"]: issue
            for issue in json.loads(budget_audit.read_text())["affected_trials"]
        }
        if budget_audit.exists()
        else {}
    )
    for row in rows:
        row.setdefault("raw_score", row["score"])
        issue = budget_issues.get(Path(row["trial"]).name)
        if issue:
            # A shorter adapter budget cannot establish an agent capability
            # failure. Retain its grade as evidence, outside matched rankings.
            row["budget_issue"] = issue["reason"]
            row["score"] = None

    issue_file = output / "grader-issues.json"
    grader_issues = json.loads(issue_file.read_text()) if issue_file.exists() else []
    flagged = {issue["task"] for issue in grader_issues}
    excluded_tasks = {
        (issue.get("suite", "wild"), issue["task"]): issue
        for issue in grader_issues
        if issue.get("exclude_from_ranking")
    }
    for issue in excluded_tasks.values():
        for relative, expected in issue["evidence_sha256"].items():
            if digest(output / relative) != expected:
                raise RuntimeError(f"Fixture issue evidence changed: {relative}")
    for row in rows:
        issue = excluded_tasks.get((row["suite"], row["task"]))
        if issue:
            row["fixture_issue"] = issue["reason"]
            row["score"] = None
    summaries = {}
    for suite in ("wild", "pinch", "terminal"):
        selected = {x["name"]: x["domain"] for x in selection.get(suite, [])}
        groups = collections.defaultdict(list)
        for row in rows:
            if row["suite"] == suite and row["score"] is not None:
                groups[(row["task"], row["agent"])].append(row["score"])
        matched = [
            t for t in selected if all(len(groups[t, a]) == 3 for a in benchmark.AGENTS)
        ]
        summaries[suite] = {
            "matched_tasks": matched,
            "selected_tasks": len(selected),
            "pending_lifecycle_replacements": sum(
                issue["suite"] == suite and not issue.get("replacement_trial")
                for issue in lifecycle.get("affected_trials", [])
            ),
            "agents": {},
        }
        for agent in benchmark.AGENTS:
            domains = collections.defaultdict(list)
            sensitivity = collections.defaultdict(list)
            for name in matched:
                score = statistics.mean(groups[name, agent])
                domains[selected[name]].append(score)
                if name not in flagged:
                    sensitivity[selected[name]].append(score)
            means = {d: statistics.mean(v) for d, v in domains.items()}
            sensitivity_means = {d: statistics.mean(v) for d, v in sensitivity.items()}
            summaries[suite]["agents"][agent] = {
                "domain_means": means,
                "domain_macro_score": statistics.mean(means.values())
                if means and len(means) == len(set(selected.values()))
                else None,
                "without_reported_grader_defects": statistics.mean(
                    sensitivity_means.values()
                )
                if sensitivity_means
                and len(sensitivity_means) == len(set(selected.values()))
                else None,
            }
    value = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "completed_agent_runs": len(rows),
        "new_completed_agent_runs": len(rows) - len(retained),
        "retained_agent_runs": len(retained),
        "remaining_agent_runs": selection["total_agent_runs"] - len(rows),
        "total_agent_runs": selection["total_agent_runs"],
        "comparable_scored_runs": sum(row["score"] is not None for row in rows),
        "comparable_percent": round(
            sum(row["score"] is not None for row in rows)
            / selection["total_agent_runs"]
            * 100,
            2,
        ),
        "budget_limited_runs": sum("budget_issue" in row for row in rows),
        "fixture_invalid_runs": sum("fixture_issue" in row for row in rows),
        "remaining_valid_agent_runs": selection["total_agent_runs"]
        - sum(row["score"] is not None for row in rows),
        "quarantined_agent_runs": sum("lifecycle_issue" in row for row in rows),
        "superseded_agent_runs": len(superseded),
        "superseded_trials": superseded,
        "withdrawn_task_attempts": len(withdrawn),
        "withdrawn_trials": withdrawn,
        "percent": round(len(rows) / selection["total_agent_runs"] * 100, 2),
        "summaries": summaries,
        "trials": rows,
        "interpretation": "Balanced local subsets; score only matched tasks with 3 valid attempts per agent. Equal weights across domains. Quarantined lifecycle attempts have no ranking score; replacements fill their original slots, and superseded evidence is preserved. Three candidate attempts differ from Terminal-Bench leaderboard's five; oracle validation is five attempts. Fast requested, actual served tier may be default. WildClaw judge uses the subscription model variant.",
    }
    readiness_path = output / "tool-readiness-repair.json"
    if readiness_path.exists():
        readiness = json.loads(readiness_path.read_text())
        value["publication_status"] = readiness["publication_status"]
        value["tool_readiness_receipt"] = str(readiness_path)
    network_path = output / "network-incident.json"
    if network_path.exists():
        network = json.loads(network_path.read_text())
        value["network_interrupted_attempts"] = len(network["affected_candidates"])
        value["physical_candidate_attempts"] = len(rows) + len(
            network["affected_candidates"]
        )
        value["network_recovery_receipt"] = str(network_path)
    setup_path = output / "setup-recovery.json"
    if setup_path.exists():
        setup = json.loads(setup_path.read_text())
        value["setup_interrupted_environments"] = len(setup["affected_environments"])
        value["setup_recovery_receipt"] = str(setup_path)
    disk_path = output / "disk-interruption.json"
    if disk_path.exists():
        disk = json.loads(disk_path.read_text())
        value["disk_interrupted_attempts"] = len(disk["affected_candidates"])
        value["physical_candidate_attempts"] = value.get(
            "physical_candidate_attempts", len(rows)
        ) + len(disk["affected_candidates"])
        value["disk_recovery_receipt"] = str(disk_path)
    save(output / "results.json", value)
    # Keep an explicit audited snapshot for consumers that must distinguish
    # lifecycle exclusions from saved attempt counts.
    if lifecycle:
        save(output / "validated-results.json", value)
    return value


def apply_lifecycle_recovery(rows: list[dict], audit: dict) -> tuple[list, list]:
    """Replace invalid attempt slots without adding repetitions or selecting scores."""
    issues = {
        str(resolve_saved_path(issue["trial"])): issue
        for issue in audit.get("affected_trials", [])
    }
    replacements = {
        str(resolve_saved_path(issue["replacement_trial"])): issue
        for issue in issues.values()
        if issue.get("replacement_trial")
    }
    if len(replacements) != sum(
        bool(issue.get("replacement_trial")) for issue in issues.values()
    ):
        raise RuntimeError("A replacement cannot fill more than one attempt slot")
    by_trial = {row["trial"]: row for row in rows}
    included = []
    superseded = []
    for row in rows:
        issue = issues.get(row["trial"])
        if issue:
            replacement = issue.get("replacement_trial")
            if replacement and str(resolve_saved_path(replacement)) in by_trial:
                superseded.append(row)
                continue
            row["raw_score"] = row["score"]
            row["score"] = None
            row["lifecycle_issue"] = issue["reason"]
        elif Path(row["trial"]).parent.name in (
            "hermes-recovery", "native-repair", "native-stream-repair", "native-service-repair"
        ):
            replacement_issue = replacements.get(row["trial"])
            if not replacement_issue:
                # A trial may finish before its group receives a deterministic
                # slot assignment. Pending replacements must not add samples.
                continue
            if (
                row["agent"] != replacement_issue.get("agent", "hermes")
                or row["suite"] != replacement_issue["suite"]
                or row["task"] != replacement_issue["task"]
            ):
                raise RuntimeError("Replacement does not match its attempt slot")
            if Path(row["trial"]).parent.name in ("native-repair", "native-stream-repair", "native-service-repair") and digest(
                Path(row["trial"]) / "result.json"
            ) != replacement_issue["replacement_result_sha256"]:
                raise RuntimeError("Replacement evidence changed")
            row["replaces_trial"] = replacement_issue["trial"]
        included.append(row)
    return included, superseded


def pending_agents(selection: dict, suite: str, task: str) -> dict[str, str]:
    """Retain whole attempt groups when comparing a replacement agent version."""
    retained = json.loads(resolve_saved_path(selection["retained_report"]).read_text())[
        "trials"
    ]
    counts = collections.Counter(
        row["agent"]
        for row in retained
        if row.get("suite", "wild") == suite and row["task"] == task
    )
    if any(count not in (0, 3) for count in counts.values()):
        raise RuntimeError("Retained candidate groups must contain three attempts")
    return {
        name: import_path
        for name, import_path in benchmark.AGENTS.items()
        if counts[name] == 0
    }


def retained_oracle(output: Path, task: str) -> bool:
    """Reuse verified reference runs independently of candidate runtime versions."""
    receipt = output / "jobs" / f"terminal--{task}" / "oracle-validation.json"
    if not receipt.exists():
        return False
    trials = json.loads(receipt.read_text())["trials"]
    if len(trials) != ORACLE_ATTEMPTS:
        raise RuntimeError("Retained oracle validation has incomplete coverage")
    for item in trials:
        path = resolve_saved_path(item["result"])
        if digest(path) != item["sha256"]:
            raise RuntimeError("Retained reference evidence changed")
        result = json.loads(path.read_text())
        rewards = (result.get("verifier_result") or {}).get("rewards") or {}
        if result.get("exception_info") or rewards.get("reward") != 1:
            raise RuntimeError("Retained reference validation was unsuccessful")
    return True


async def execute(
    output: Path,
    selection: dict,
    suite: str,
    item: dict,
    mode: str,
    baseline: set[str],
    *,
    agents: dict[str, str] | None = None,
    attempts: int | None = None,
    release_images: bool = True,
) -> list[dict]:
    """Resume saved trials without automatically repeating interrupted agent work."""
    name = item["name"]
    directory = output / "jobs" / f"{suite}--{name}" / mode
    directory.mkdir(parents=True, exist_ok=True)
    for p in directory.iterdir():
        if (
            p.is_dir()
            and (p / "config.json").exists()
            and not (p / "result.json").exists()
        ):
            raise RuntimeError(
                f"Interrupted trial retained for review; refusing an implicit rerun: {p}"
            )
    config_path = directory / "config.json"
    policy = json.loads((output / "scheduling-policy.json").read_text())
    policy.update(policy.get("task_overrides", {}).get(f"{suite}--{name}", {}))
    task_resources = (
        {"cpus": 2, "memory_mb": 4_096}
        if suite == "wild"
        else toml.load(output / "tasks" / suite / name / "task.toml")["environment"]
    )
    concurrency = scheduling.capacity(
        scheduling.resources(output), task_resources, policy
    )
    while concurrency == 0:
        save(directory / "admission-wait.json", {"reason": "Host resource headroom"})
        await asyncio.sleep(policy["admission_poll_seconds"])
        concurrency = scheduling.capacity(
            scheduling.resources(output), task_resources, policy
        )
    native_mode = mode in (
        "native", "hermes-recovery", "native-repair", "native-stream-repair", "native-service-repair"
    )
    if not native_mode:
        concurrency = min(concurrency, policy["validation_trials"])
    if config_path.exists():
        config = JobConfig.model_validate_json(config_path.read_text())
    else:
        config = JobConfig.model_validate(
            {
                "job_name": mode,
                "jobs_dir": str(directory.parent),
                "n_attempts": attempts
                if attempts is not None
                else ORACLE_ATTEMPTS
                if mode == "oracle"
                else 1
                if mode == "setup"
                else 3,
                "n_concurrent_trials": concurrency,
                "quiet": True,
                "retry": {"max_retries": 0},
                "install_only": mode == "setup",
                "environment": {
                    "type": "docker",
                    "delete": True,
                    "mounts": json.loads((output / "mounts.json").read_text()),
                    "extra_docker_compose": [str(output / "network.yaml")],
                    **(
                        {"override_cpus": 2, "override_memory_mb": 4_096}
                        if suite == "wild"
                        else {}
                    ),
                },
                "agents": [{"name": "oracle"}]
                if mode == "oracle"
                else [
                    {"import_path": a, "model_name": benchmark.MANIFEST["model"]}
                    for a in (
                        agents
                        if agents is not None
                        else benchmark.AGENTS
                        if mode == "setup"
                        else pending_agents(selection, suite, name)
                    ).values()
                ],
                "tasks": [{"path": str(output / "tasks" / suite / name)}],
            }
        )
    if agents is not None and (
        {agent.import_path for agent in config.agents} != set(agents.values())
        or config.n_attempts != attempts
    ):
        raise RuntimeError(
            "Saved recovery configuration differs from approved coverage"
        )
    job = await Job.create(config)
    guard = asyncio.Lock()
    admission = scheduling.Admission(
        directory,
        output,
        task_resources,
        policy,
        rate_limits,
        quota_available,
        config.n_concurrent_trials,
    )
    admission.record(
        "initialized",
        task_resources=task_resources,
        resources=admission.initial_resources,
    )

    async def started(event):
        await admission.admit(event)
        async with guard:
            await asyncio.to_thread(capture_resources, output, directory, baseline)

    async def ended(event):
        async with guard:
            if native_mode and (output / "diagnostic-policy.json").exists():
                from scripts.benchmarks.task_integrity import inspect_trial

                await asyncio.to_thread(
                    inspect_trial, output, directory / event.trial_name
                )
            await asyncio.to_thread(capture_resources, output, directory, baseline)
            await asyncio.to_thread(cleanup_job, output, directory)
            await asyncio.to_thread(report, output, selection)
        validated = not event.result.exception_info
        if native_mode:
            rows = json.loads((output / "results.json").read_text())["trials"]
            row = next(
                (row for row in rows if Path(row["trial"]).name == event.trial_name),
                None,
            )
            validated = bool(
                row
                and row.get("settings_verified")
                and not row.get("grader_error")
                and row["score"] is not None
            )
        await admission.release(event, validated)

    job.on_environment_started(started)
    job.on_trial_ended(ended)
    running = asyncio.create_task(job.run())

    async def disk_guard():
        while not running.done():
            await asyncio.sleep(10)
            if (
                min(shutil.disk_usage(p).free for p in (Path("/"), output))
                < EMERGENCY_DISK_RESERVE
            ):
                save(
                    directory / "disk-guard.json",
                    {
                        "reason": "Disk reserve reached; canceling in-flight work and preserving its evidence"
                    },
                )
                running.cancel()
                return

    monitor = asyncio.create_task(disk_guard())
    try:
        await running
    finally:
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)
        await asyncio.to_thread(capture_resources, output, directory, baseline)
        # Reference/setup images are no longer needed once their own mode ends.
        # Shared layers stay in the task builder for the remaining candidate work.
        await asyncio.to_thread(cleanup_job, output, directory, True, release_images)
    return [json.loads(p.read_text()) for p in directory.glob("*/result.json")]


async def run(output: Path, suite_filter: str | None = None) -> None:
    """Run one task group at a time, retaining progress across owner interruptions."""
    selection = initialize(output)
    runtime_receipt = output / "openclaw-runtime-files.json"
    if runtime_receipt.exists():
        for path, expected in json.loads(runtime_receipt.read_text())["files"].items():
            if digest(Path(path)) != expected:
                raise RuntimeError(f"Frozen OpenClaw runtime changed: {path}")
    benchmark.configure_runtime(output)
    for key in PROXY_NAMES:
        os.environ.pop(key, None)
    state = {
        "status": "running",
        "pid": os.getpid(),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    save(output / "status.json", state)
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, task.cancel)
    baseline = {x["Id"] for x in docker_objects("image")}
    # Keep the large shared WildClaw image out of Docker while terminal tasks run.
    # Domain rotation gives intermediate reports useful coverage across the suite.
    groups = collections.defaultdict(list)
    for item in selection["terminal"]:
        groups[item["domain"]].append(item)
    terminal = [
        groups[d][i]
        for i in range(5)
        for d in sorted(groups, key=lambda d: (d != "Operations", d))
        if i < len(groups[d])
    ]
    schedule = [("terminal", x) for x in terminal] + [
        ("wild", x) for x in selection["wild"]
    ]
    if suite_filter is not None:
        schedule = [entry for entry in schedule if entry[0] == suite_filter]
    try:
        for suite, item in schedule:
            group = output / "jobs" / f"{suite}--{item['name']}"
            marker = group / "complete.json"
            if marker.exists() or (group / "held.json").exists():
                continue
            candidates = pending_agents(selection, suite, item["name"])
            if not candidates:
                continue
            limits = await asyncio.to_thread(rate_limits)
            save(output / "rate-limits.json", limits)
            while not quota_available(limits):
                state.update(
                    status="waiting for subscription allowance", task=item["name"]
                )
                save(output / "status.json", state)
                await asyncio.sleep(30)
                limits = await asyncio.to_thread(rate_limits)
                save(output / "rate-limits.json", limits)
            if (
                min(shutil.disk_usage(p).free for p in (Path("/"), output))
                < DISK_RESERVE + 5 * GIB
            ):
                raise RuntimeError(
                    "Insufficient free disk for the next task; no task launched"
                )
            state.update(status="preparing task", suite=suite, task=item["name"])
            save(output / "status.json", state)
            if suite == "wild" and not command(
                ["docker", "image", "ls", "-q", benchmark.DEFAULT_IMAGE]
            ):
                state["status"] = "loading official WildClaw image"
                save(output / "status.json", state)
                with (output / "image-load.log").open("a") as log:
                    loader = await asyncio.create_subprocess_exec(
                        sys.executable,
                        str(ROOT / "scripts/benchmarks/load-wild-image.py"),
                        "--temporary-directory",
                        str(output / "image-transfer"),
                        stdout=log,
                        stderr=log,
                    )
                    try:
                        if await loader.wait():
                            raise RuntimeError(
                                "Official image load failed; inspect image-load.log"
                            )
                    finally:
                        if loader.returncode is None:
                            loader.terminate()
                            await loader.wait()
            await asyncio.to_thread(
                download,
                output,
                suite,
                item["name"],
                selection["dataset_revisions"][suite],
            )
            builder = (
                BUILDER_PREFIX + hashlib.sha256(item["name"].encode()).hexdigest()[:12]
            )
            if suite == "terminal":
                command(
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
            try:
                if suite == "terminal" and not retained_oracle(output, item["name"]):
                    state["status"] = "oracle validation"
                    save(output / "status.json", state)
                    oracle = await execute(
                        output, selection, suite, item, "oracle", baseline
                    )
                    passed = len(oracle) == ORACLE_ATTEMPTS and all(
                        not x.get("exception_info")
                        and ((x.get("verifier_result") or {}).get("rewards") or {}).get(
                            "reward"
                        )
                        == 1
                        for x in oracle
                    )
                    if not passed:
                        save(
                            group / "held.json",
                            {
                                "reason": "Official oracle failed validation; candidate runs not launched",
                                "oracle_results": [x["trial_name"] for x in oracle],
                            },
                        )
                        continue
                state["status"] = "native runtime setup validation"
                save(output / "status.json", state)
                setup = await execute(output, selection, suite, item, "setup", baseline)
                if len(setup) != 3 or any(x.get("exception_info") for x in setup):
                    save(
                        group / "held.json",
                        {
                            "reason": "Native runtime setup failed; candidate runs not launched"
                        },
                    )
                    continue
                state["status"] = "agent execution"
                save(output / "status.json", state)
                results = await execute(
                    output, selection, suite, item, "native", baseline
                )
                expected = 3 * len(candidates)
                if len(results) != expected:
                    raise RuntimeError(
                        f"Expected {expected} saved agent attempts for {item['name']}"
                    )
                save(
                    marker,
                    {
                        "agent_runs": expected,
                        "results": {
                            x["trial_name"]: digest(
                                group / "native" / x["trial_name"] / "result.json"
                            )
                            for x in results
                        },
                    },
                )
            finally:
                if suite == "terminal":
                    command(["docker", "buildx", "rm", builder], check=False)
                    os.environ.pop("BUILDX_BUILDER", None)
                for mode in ("oracle", "setup", "native"):
                    await asyncio.to_thread(
                        cleanup_job, output, group / mode, True, True
                    )
                # A build can finish before a container starts. Its content tag
                # still proves task ownership when no lifecycle hook ran.
                for folder in ("environment", "tests"):
                    context = output / "tasks" / suite / item["name"] / folder
                    if suite == "terminal" and context.is_dir():
                        tag = "hb__" + environment_content_hash(
                            context, docker_image=None
                        )
                        identity = command(
                            ["docker", "image", "inspect", tag, "--format", "{{.Id}}"],
                            check=False,
                        )
                        if identity and identity not in baseline:
                            command(["docker", "image", "rm", tag], check=False)
                if marker.exists() or (group / "held.json").exists():
                    for folder in ("environment", "solution"):
                        shutil.rmtree(
                            output / "tasks" / suite / item["name"] / folder,
                            ignore_errors=True,
                        )
            await asyncio.to_thread(report, output, selection)
        state["status"] = (
            "finished"
            if report(output, selection)["remaining_agent_runs"] == 0
            else "execution finished with held tasks"
        )
        if command(["docker", "image", "ls", "-q", benchmark.DEFAULT_IMAGE]):
            identity = command(
                [
                    "docker",
                    "image",
                    "inspect",
                    benchmark.DEFAULT_IMAGE,
                    "--format",
                    "{{.Id}}",
                ]
            )
            if (
                identity
                == json.loads((PILOT / "provenance.json").read_text())["image_identity"]
                and identity not in baseline
            ):
                command(["docker", "image", "rm", benchmark.DEFAULT_IMAGE])
    except BaseException as error:
        state.update(
            status="stopped", error_type=type(error).__name__, error=str(error)
        )
        raise
    finally:
        loop.remove_signal_handler(signal.SIGTERM)
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        save(output / "status.json", state)
        report(output, selection)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preflight", "report", "run"))
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--suite", choices=("wild", "terminal"))
    args = parser.parse_args()
    if args.action == "run":
        asyncio.run(run(args.output.resolve(), args.suite))
    else:
        selection = initialize(args.output.resolve())
        print(
            json.dumps(report(args.output.resolve(), selection)["summaries"], indent=2)
        )
