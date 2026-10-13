"""Project actual native turns into PinchBench's transcript schema without inference."""

import argparse
import json
import os
from pathlib import Path

import native_transcript

LOGS = Path("/logs/agent")


def atif_rows(trajectory):
    """Retain tool identities and observations from the native ATIF journal."""
    rows = []
    for step in trajectory.get("steps", []):
        source = step.get("source")
        if source not in ("user", "agent", "system"):
            continue
        calls = step.get("tool_calls") or []
        content = [{"type": "text", "text": step.get("message") or ""}]
        content.extend(
            {
                "type": "toolCall",
                "id": call["tool_call_id"],
                "name": call["function_name"],
                "arguments": call.get("arguments") or {},
            }
            for call in calls
        )
        role = "assistant" if source == "agent" else source
        rows.append(
            {
                "type": "message",
                "timestamp": step.get("timestamp"),
                "message": {
                    "role": role,
                    "content": content
                    if source == "agent"
                    else step.get("message", ""),
                },
            }
        )
        names = {call["tool_call_id"]: call["function_name"] for call in calls}
        for result in (step.get("observation") or {}).get("results", []):
            identifier = result.get("source_call_id")
            rows.append(
                {
                    "type": "message",
                    "message": {
                        "role": "toolResult",
                        "toolCallId": identifier,
                        "name": names.get(identifier),
                        "content": result.get("content", ""),
                    },
                }
            )
    return rows


def snapshot_openclaw(session_id: str, destination: Path):
    """Archive a session before a fresh conversation changes the active inventory."""
    os.environ["PINCHBENCH_EXPORT_SESSION_ID"] = session_id
    native_transcript.LOGS = destination
    native_transcript.collect()
    path = destination / "native-transcript.jsonl"
    if not path.exists() or not path.stat().st_size:
        raise RuntimeError("Native OpenClaw conversation export is empty")


def collect_all():
    """Merge delivered turns while preserving distinct fresh-session boundaries."""
    rows = []
    previous_openclaw = []
    turns_path = LOGS / "pinch-turns.json"
    records = json.loads(turns_path.read_text())["turns"] if turns_path.exists() else []
    turns = sorted((LOGS / "turns").glob("*"))
    for index, turn in enumerate(turns):
        if not turn.is_dir():
            continue
        native_transcript.LOGS = turn
        trajectory = turn / "trajectory.json"
        hermes = turn / "hermes-result.json"
        archived = turn / "native-transcript.jsonl"
        if trajectory.exists() or (turn / "trajectory-events.jsonl").exists():
            native_transcript.collect()
            rows.extend(atif_rows(json.loads(trajectory.read_text())))
        elif hermes.exists():
            result = json.loads(hermes.read_text())
            messages = result.get("messages") or []
            boundary = result.get("current_turn_user_idx", 0)
            if not isinstance(boundary, int) or not 0 <= boundary <= len(messages):
                raise RuntimeError("Hermes did not export a valid native turn boundary")
            rows.extend(native_transcript.chat_rows(messages[boundary:]))
        else:
            if not archived.exists():
                native_transcript.collect()
            current = [
                json.loads(line)
                for line in archived.read_text().splitlines()
                if line.strip()
            ]
            if index < len(records) and records[index].get("new_session"):
                previous_openclaw = []
            # A reused native session contains its earlier messages as a prefix.
            # Append only newly delivered messages, retaining complete snapshots.
            common = 0
            for before, after in zip(previous_openclaw, current):
                if before != after:
                    break
                common += 1
            rows.extend(current[common:])
            previous_openclaw = current
    path = LOGS / "pinch-transcript.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    (LOGS / "transcript-export.json").write_text(
        json.dumps(
            {
                "rows": len(rows),
                "delivered_turns": len(turns),
                "source": "native conversation snapshots",
                "additional_inference_calls": 0,
            }
        )
        + "\n"
    )
    return rows


def main():
    """Export native evidence without delivering any additional agent prompt."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot-openclaw", nargs=2)
    args = parser.parse_args()
    if args.snapshot_openclaw:
        snapshot_openclaw(args.snapshot_openclaw[0], Path(args.snapshot_openclaw[1]))
    else:
        collect_all()


if __name__ == "__main__":
    main()
