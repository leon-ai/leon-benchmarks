"""Project actual native turns into PinchBench's transcript schema without inference."""

import argparse
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

import native_transcript

LOGS = Path("/logs/agent")
ALIASES_PATH = Path(__file__).with_name("pinch-tool-aliases.json")


def within_deadline(row, deadline):
    """Keep messages without known timestamps; exclude proven late evidence."""
    value = row.get("timestamp")
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    if not isinstance(value, (float, int)):
        return True
    if value > 1_000_000_000_000:
        value /= 1_000
    return value <= deadline


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


def dispatch_rows(call, parent=None):
    """Project an observed nested dispatch without calling it a model inference."""
    return [
        {
            "type": "message",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": call["id"],
                        "name": call["name"],
                        "arguments": call["arguments"],
                        "origin": call["source"],
                        "parentToolCallId": parent,
                    }
                ],
            },
        },
        {
            "type": "message",
            "message": {
                "role": "toolResult",
                "toolCallId": call["id"],
                "name": call["name"],
                "content": call["result"]
                if call["result"] is not None
                else json.dumps({"error": call["error"]}),
            },
        },
    ]


def hermes_rows(turn: Path, messages, deadline=None, allow_pending=False):
    """Retain actual nested dispatches at their native code-result boundary."""
    rows = native_transcript.chat_rows(messages)
    path = turn / "nested-tool-calls.jsonl"
    if not path.exists():
        return rows
    calls = [json.loads(line) for line in path.read_text().split("\n") if line.strip()]
    if deadline is not None:
        calls = [call for call in calls if call["finished_at"] <= deadline]
    index = 0
    expanded = []
    for row in rows:
        message = row.get("message", {})
        if (
            message.get("role") == "toolResult"
            and message.get("name") == "execute_code"
        ):
            result = message.get("content", "")
            result = json.loads(result) if isinstance(result, str) else result
            count = result.get("tool_calls_made", 0) if isinstance(result, dict) else 0
            for call in calls[index : index + count]:
                expanded.extend(dispatch_rows(call, message.get("toolCallId")))
            index += count
        expanded.append(row)
    if index > len(calls) or (index != len(calls) and not allow_pending):
        raise RuntimeError(
            "Nested Hermes dispatch journal differs from native call counts"
        )
    for call in calls[index:]:
        expanded.extend(dispatch_rows(call))
    return expanded


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
        start = len(rows)
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
            rows.extend(hermes_rows(turn, messages[boundary:]))
        elif (turn / "hermes-session.json").exists():
            metadata = json.loads((turn / "hermes-session.json").read_text())
            saved = turn / "hermes-native-messages.json"
            if not saved.exists():
                native_transcript.collect()
            messages = json.loads(saved.read_text())
            boundaries = [
                i
                for i, message in enumerate(messages)
                if message.get("role") == "user"
                and isinstance(message.get("content"), str)
                and hashlib.sha256(message["content"].encode()).hexdigest()
                == metadata["prompt_sha256"]
            ]
            if not boundaries:
                raise RuntimeError(
                    "Timed-out native Hermes session lacks its delivered user prompt"
                )
            messages = messages[boundaries[-1] :]
            deadline = metadata["deadline_epoch"]
            messages = [
                message for message in messages if message["timestamp"] <= deadline
            ]
            rows.extend(
                hermes_rows(turn, messages, deadline=deadline, allow_pending=True)
            )
        else:
            if not archived.exists():
                native_transcript.collect()
            current = [
                json.loads(line)
                for line in archived.read_text().split("\n")
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
        clock = turn / "deadline.json"
        if clock.exists():
            deadline = json.loads(clock.read_text())["deadline_epoch"]
            late_calls = {
                part.get("id")
                for row in rows[start:]
                if not within_deadline(row, deadline)
                for part in row.get("message", {}).get("content", [])
                if isinstance(part, dict) and part.get("type") == "toolCall"
            }
            # Results of calls begun after the deadline are also late evidence,
            # even when the native result lacks its own timestamp.
            rows[start:] = [
                row
                for row in rows[start:]
                if within_deadline(row, deadline)
                and not (
                    row.get("message", {}).get("role") == "toolResult"
                    and row["message"].get("toolCallId") in late_calls
                )
            ]
    aliases = json.loads(ALIASES_PATH.read_text())["aliases"]
    invocations_path = LOGS / "gws-invocations.jsonl"
    if invocations_path.exists():
        # The CLI boundary is identical for shell strings and Python argv lists.
        # These are actual process invocations, separate from model tool-call counts.
        for line in invocations_path.read_text().split("\n"):
            if not line.strip():
                continue
            call = json.loads(line)
            rows.append(
                {
                    "type": "message",
                    "timestamp": call["timestamp"],
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "toolCall",
                                "id": call["id"],
                                "name": "gws_cli",
                                "arguments": {"command": call["command"]},
                                "origin": call["source"],
                            }
                        ],
                    },
                }
            )
    for row in rows:
        message = row.get("message", {})
        content = message.get("content", [])
        if isinstance(content, str):
            # Native PinchBench readers expect content blocks for every role.
            # Keep the whole observation or prompt instead of exposing characters.
            content = [{"type": "text", "text": content}] if content else []
            message["content"] = content
        if not isinstance(content, list):
            raise RuntimeError("PinchBench transcript content is not a block list")
        for part in content:
            if not isinstance(part, dict):
                raise RuntimeError("PinchBench transcript block is not an object")
            if part.get("type") == "toolCall" and part.get("name") in aliases:
                # The portable grader schema recognizes file-reading operations;
                # retain the native identity alongside its declared equivalent.
                part["native_name"] = part["name"]
                part["name"] = aliases[part["name"]]
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
