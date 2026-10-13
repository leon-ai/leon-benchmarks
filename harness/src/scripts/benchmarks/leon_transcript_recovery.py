"""Restore native public messages without repeating candidate execution."""

import argparse
import concurrent.futures
import hashlib
import json
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from scripts.benchmarks import balanced, campaign, metrics
from scripts.benchmarks.network_recovery import regrade
from scripts.benchmarks.regrading import verified_receipt


def restore_file(trial: Path, source_path: str, destination: Path) -> dict:
    """Recover complete native reads only when their original file hashes agree."""
    trajectory_file = trial / "agent/trajectory.json"
    trajectory = json.loads(trajectory_file.read_text())
    chunks = {}
    expected_hashes = set()
    expected_sizes = set()
    for step in trajectory["steps"]:
        for call in step.get("tool_calls", []):
            if (
                not call["function_name"].endswith(".read")
                or call.get("arguments", {}).get("path") != source_path
            ):
                continue
            for observation in (step.get("observation") or {}).get("results", []):
                response = json.loads(observation["content"])
                result = response.get("data", {}).get("output", {}).get("result", {})
                if response.get("status") != "success" or not result.get("success"):
                    raise RuntimeError("Cannot restore a failed native file read")
                data = result["data"]
                chunks[data["offsetChars"]] = data["content"]
                expected_hashes.add(data["contentHash"])
                expected_sizes.add(data["size"])

    content = ""
    for offset, chunk in sorted(chunks.items()):
        if offset != len(content):
            raise RuntimeError("Native file read coverage is not contiguous")
        content += chunk
    encoded = content.encode()
    digest = hashlib.sha256(encoded).hexdigest()
    if expected_hashes != {digest} or expected_sizes != {len(encoded)}:
        raise RuntimeError("Restored bytes differ from the original native file")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(encoded)
    return {
        "original_path": source_path,
        "restored_path": str(destination),
        "original_native_sha256": digest,
        "bytes": len(encoded),
        "source_sha256": {str(trajectory_file): balanced.digest(trajectory_file)},
    }


def restore_history(trial: Path, destination: Path) -> dict:
    """Merge timestamped public trace events with unchanged native tool history."""
    original = trial / "agent/trajectory.json"
    trajectory = json.loads(original.read_text())
    steps = trajectory["steps"]
    existing = {step.get("message") for step in steps}
    sources = [original]
    messages = []
    result_file = trial / "agent/leon-result.json"

    if result_file.exists():
        sources.append(result_file)
        result = json.loads(result_file.read_text())
        trace = result.get("result", {}).get("data", {}).get("agentResponseTrace", {})
        for message in trace.get("progressMessages", []):
            if message["content"] not in existing:
                messages.append(
                    {
                        "source": "agent",
                        "message": message["content"],
                        "timestamp": datetime.fromtimestamp(
                            message["createdAt"] / 1_000, timezone.utc
                        ).isoformat(),
                        "benchmark_evidence": {
                            "source": "native progress_message trace",
                            "native_message_id": message["id"],
                        },
                    }
                )
                existing.add(message["content"])

    # Native prompt history preserves textual assistant messages accompanying
    # tool calls even when a deadline prevents the final result from being saved.
    # Internal protocol messages without tool calls and reasoning are excluded.
    prompt_file = trial / "agent/leon-logs/prompts/agent.log"
    requests_file = trial / "agent/requests.jsonl"
    calls_file = trial / "agent/model-tool-calls.jsonl"
    if prompt_file.exists():
        sources.extend([prompt_file, requests_file, calls_file])
        requests = {
            row["inferenceId"]: row
            for row in metrics.lines(requests_file)
        }
        calls = {
            row["callId"]: row
            for row in metrics.lines(calls_file)
            if row.get("callId")
        }
        for line in prompt_file.read_text().splitlines():
            if not line.startswith('[{"role"'):
                continue
            history = json.loads(line)
            if any(message.get("compactionContext") for message in history):
                raise RuntimeError("Compacted history requires separate completeness review")
            for message in history:
                content = message.get("content")
                tool_calls = message.get("toolCalls") or []
                if (
                    message.get("role") != "assistant"
                    or not isinstance(content, str)
                    or not content
                    or not tool_calls
                    or content in existing
                ):
                    continue
                anchors = [calls[call["id"]] for call in tool_calls if call["id"] in calls]
                if not anchors:
                    raise RuntimeError("Public message lacks native model-call ordering evidence")
                anchor = anchors[0]
                request = requests[anchor["inferenceId"]]
                started = datetime.fromisoformat(request["startedAt"]).timestamp()
                upper_bound = started + anchor["readySeconds"]
                messages.append(
                    {
                        "source": "agent",
                        "message": content,
                        "timestamp": datetime.fromtimestamp(
                            upper_bound, timezone.utc
                        ).isoformat(),
                        "benchmark_evidence": {
                            "source": "native assistant/tool-call prompt history",
                            "timestamp_kind": "associated tool ready upper bound",
                            "before_model_tool_call_id": anchor["callId"],
                        },
                    }
                )
                existing.add(content)

    merged = sorted(
        [*steps, *messages],
        key=lambda step: datetime.fromisoformat(step["timestamp"]),
    )
    for index, step in enumerate(merged, start=1):
        step["step_id"] = index
    trajectory["steps"] = merged
    destination.mkdir(parents=True, exist_ok=True)
    balanced.save(destination / "trajectory.json", trajectory)
    receipt = {
        "method": "Unchanged native tool history plus saved public assistant messages; no private reasoning.",
        "original_trial": str(trial),
        "restored_public_messages": len(messages),
        "messages": messages,
        "source_sha256": {str(source): balanced.digest(source) for source in sources},
        "additional_candidate_attempts": 0,
    }
    balanced.save(destination / "history-restoration.json", receipt)
    return receipt


def main():
    """Replay all reviewed transcript-dependent grades with bounded concurrency."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    review = json.loads((output / "leon-fairness-review.json").read_text())
    trials = [Path(trial) for trial in review["transcript_dependent_impact_review_trials"]]
    restored = []
    recovery_plan = json.loads((output / "leon-public-transcript-recovery-plan.json").read_text())
    for trial in trials:
        history = trial / "verifier/public-transcript-regrade/history"
        if verified_receipt(trial):
            restored.append(json.loads((history / "history-restoration.json").read_text()))
            continue
        receipt = restore_history(trial, history)
        task = trial.parent.parent.name.split("--", 1)[1]
        files = []
        for relative in recovery_plan.get("required_workspace_files", {}).get(task, []):
            files.append(
                restore_file(
                    trial,
                    "/tmp_workspace/" + relative,
                    history / "workspace" / relative,
                )
            )
        receipt["restored_workspace_files"] = files
        receipt["preserved_native_gates"] = recovery_plan.get(
            "preserved_native_gates", {}
        ).get(task, {})
        balanced.save(history / "history-restoration.json", receipt)
        restored.append(receipt)
    balanced.save(output / "leon-public-transcript-restoration.json", restored)

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
                    recovery_name="public-transcript-regrade",
                    history_directory=trial / "verifier/public-transcript-regrade/history",
                )
                for trial in trials
            ]
            for future in futures:
                future.result()
    campaign.report(output, json.loads((output / "selection.json").read_text()))


if __name__ == "__main__":
    main()
