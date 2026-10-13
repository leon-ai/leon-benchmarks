"""Restore omitted native service seeds and replace only affected task cohorts."""

import argparse
import ast
import asyncio
import json
import os
import shutil
import signal
from pathlib import Path

import toml

from scripts.benchmarks import balanced, campaign, task_integrity

MODE = "native-service-repair"


def prepare(output: Path, task: dict) -> Path:
    """Preserve native instructions, graders, seed bytes and random error policy."""
    name = task["task"]
    target = balanced.download(
        output, "wild", name, balanced.benchmark.MANIFEST["revision"]
    )
    receipt_path = output / "inputs" / f"wild-{name}.json"
    receipt = json.loads(receipt_path.read_text())
    already_prepared = any(
        row.get("native_live_service_seed_repair") for row in receipt["adaptations"]
    )

    for fixture in task["restored_files"]:
        source = Path(fixture["local_source"])
        if balanced.digest(source) != fixture["sha256"]:
            raise RuntimeError("Canonical live service input changed")
        destination = target / fixture["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    if already_prepared:
        for adaptation in receipt["adaptations"]:
            if adaptation.get("native_live_service_seed_repair"):
                adaptation["native_live_service_seed_repair"] = task
        receipt["prepared_files"] = {
            str(path.relative_to(target)): balanced.digest(path)
            for path in target.rglob("*")
            if path.is_file()
        }
        balanced.save(receipt_path, receipt)
        return target

    warmup = target / "environment/.wildclaw/run-warmup.sh"
    text = warmup.read_text()
    services = []
    for server in (target / "environment/mock_services").glob("*/server.py"):
        tree = ast.parse(server.read_text())
        ports = {
            int(node.args[1].value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and len(node.args) == 2
            and all(isinstance(value, ast.Constant) for value in node.args)
            and node.args[0].value == "PORT"
        }
        if len(ports) != 1:
            raise RuntimeError("Native service port contract is ambiguous")
        services.append(
            {
                "name": server.parent.name,
                "url": f"http://127.0.0.1:{ports.pop()}/openapi.json",
            }
        )
    balanced.save(target / "environment/.wildclaw/required-services.json", services)
    marker = 'touch "$marker"'
    insertion = (
        "python3 /opt/leon/scripts/benchmarks/service_readiness.py "
        "/tmp_workspace/.wildclaw/required-services.json /logs/agent/native-service-readiness.json\n"
    )
    lines = text.splitlines(keepends=True)
    deletion = next(
        index
        for index, line in enumerate(lines)
        if "rm -f -r /tmp_workspace/tmp" in line
    )
    lines.insert(deletion, insertion)
    if marker not in text:
        raise RuntimeError("Native warmup completion marker changed")
    warmup.write_text("".join(lines))
    configuration = toml.loads((target / "task.toml").read_text())
    plan = json.loads((output / "social-service-repair-plan.json").read_text())
    configuration["environment"]["docker_image"] = plan["service_image"]
    (target / "task.toml").write_text(toml.dumps(configuration))
    receipt["adaptations"].append(
        {
            "native_live_service_seed_repair": task,
            "readiness": "Error-exempt OpenAPI probe before seed cleanup; no native error policy changes.",
        }
    )
    receipt["prepared_files"] = {
        str(path.relative_to(target)): balanced.digest(path)
        for path in target.rglob("*")
        if path.is_file()
    }
    balanced.save(receipt_path, receipt)
    return target


async def run(output: Path) -> None:
    """Keep one nine-attempt task cohort active and retire its resources after export."""
    plan = json.loads((output / "social-service-repair-plan.json").read_text())
    if not plan["approved"]:
        raise RuntimeError("Native service repair lacks authorization")
    if not plan.get("service_preflight_passed"):
        raise RuntimeError(
            "Native live fixture and service controls are required before candidates"
        )
    current = asyncio.current_task()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, current.cancel)
    balanced.benchmark.configure_runtime(output)
    selection = json.loads((output / "selection.json").read_text())
    baseline = {row["Id"] for row in balanced.docker_objects("image")}
    state = {"status": "native service repair", "suite": "wild", "pid": os.getpid()}
    for task in plan["tasks"]:
        name = task["task"]
        group = output / "jobs" / f"wild--{name}"
        if (group / "service-repair-complete.json").exists():
            continue
        state.update(task=name, status="preparing native service repair")
        balanced.save(output / "status.json", state)
        await campaign.quota(output, state)
        await asyncio.to_thread(prepare, output, task)
        results = await balanced.execute(
            output,
            selection,
            "wild",
            next(row for row in selection["wild"] if row["name"] == name),
            MODE,
            baseline,
            agents=balanced.benchmark.AGENTS,
            attempts=3,
            release_images=False,
        )
        if len(results) != 9:
            raise RuntimeError("Native service repair requires nine completed attempts")
        audit_path = output / "environment-repair-audit.json"
        audit = json.loads(audit_path.read_text())
        for agent in balanced.benchmark.AGENTS:
            old = sorted(
                [
                    row
                    for row in audit["affected_trials"]
                    if row["task"] == name
                    and row["agent"] == agent
                    and row["replacement_directory"] == str(group / MODE)
                ],
                key=lambda row: row["trial_name"],
            )
            new = sorted(
                [row for row in results if row["agent_info"]["name"] == agent],
                key=lambda row: row["trial_name"],
            )
            if len(old) != 3 or len(new) != 3:
                raise RuntimeError(
                    "Native service replacement slots differ from quarantine"
                )
            for original, result in zip(old, new, strict=True):
                trial = group / MODE / result["trial_name"]
                if task_integrity.inspect_trial(output, trial)["requires_review"]:
                    raise RuntimeError(
                        "Native service replacement requires validity review"
                    )
                readiness = json.loads(
                    (trial / "agent/native-service-readiness.json").read_text()
                )
                if not readiness["passed"] or readiness["business_calls"] != 0:
                    raise RuntimeError(
                        "Native service readiness evidence is incomplete"
                    )
                original.update(
                    replacement_trial=str(trial),
                    replacement_result_sha256=balanced.digest(trial / "result.json"),
                )
        balanced.save(audit_path, audit)
        balanced.save(
            group / "service-repair-complete.json",
            {"attempts": 9, "canonical_fixtures_restored": True},
        )
        campaign.report(output, selection)
        await asyncio.to_thread(balanced.cleanup_job, output, group / MODE, True, False)
        shutil.rmtree(output / "tasks/wild" / name / "environment", ignore_errors=True)
    state.update(status="Wild service replacements completed; final review pending")
    balanced.save(output / "status.json", state)
    balanced.command(["docker", "image", "rm", plan["service_image"]], check=False)
    shutil.rmtree(output / "social-service-image-context", ignore_errors=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(run(args.output.resolve()))
