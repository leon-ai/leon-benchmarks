"""Restore dispatched shell arguments from exact native public trace pairs."""

import copy
import hashlib
import json
from pathlib import Path

from scripts.benchmarks import balanced


def restore(trial: Path, destination: Path) -> dict:
    """Match transport filenames to native input/output pairs without changing execution."""
    original = trial / "agent/trajectory.json"
    prior_history = trial / "verifier/public-transcript-regrade/history/trajectory.json"
    history_source = prior_history if prior_history.exists() else original
    trajectory = copy.deepcopy(json.loads(history_source.read_text()))
    result_file = trial / "agent/leon-result.json"
    trace = json.loads(result_file.read_text())["result"]["data"]["agentResponseTrace"]
    pairs = {}
    for call in trace.get("toolCalls", []):
        if not call.get("name", "").endswith(".shell.executeCommand"):
            continue
        output = call.get("output")
        if not isinstance(output, dict) or not isinstance(call.get("input"), str):
            continue
        transport = output.get("result", {}).get("command")
        requested = json.loads(call["input"])
        if transport and isinstance(requested.get("command"), str):
            pairs[transport] = (requested["command"], call["id"])

    restored = []
    for step in trajectory["steps"]:
        for call in step.get("tool_calls", []):
            if not call["function_name"].endswith(".shell.executeCommand"):
                continue
            transport = call["arguments"].get("command")
            if transport not in pairs:
                raise RuntimeError("Dispatched shell command lacks an exact native public trace pair")
            command, native_id = pairs[transport]
            call["arguments"]["command"] = command
            restored.append(
                {
                    "exported_call_id": call["tool_call_id"],
                    "native_call_id": native_id,
                    "native_transport_filename": transport,
                    "native_command_sha256": hashlib.sha256(command.encode()).hexdigest(),
                }
            )
    destination.mkdir(parents=True, exist_ok=True)
    balanced.save(destination / "trajectory.json", trajectory)
    receipt = {
        "method": "Exact native public tool input/output filename pairing; original chronology and observations retained.",
        "original_trial": str(trial),
        "restored_commands": restored,
        "source_sha256": {str(path): balanced.digest(path) for path in (history_source, result_file)},
        "additional_candidate_attempts": 0,
        "additional_model_calls": 0,
    }
    balanced.save(destination / "history-restoration.json", receipt)
    return receipt
