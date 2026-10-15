"""Expose native execution history to unchanged task graders without model calls."""

import json
import os
import subprocess
from pathlib import Path

LOGS = Path("/logs/agent")
COMPATIBILITY = Path("/root/.openclaw/agents/main/sessions/chat.jsonl")
MANAGED_NODE = "/tmp/leon-benchmark/bin/node/bin/node"
OPENCLAW_ENTRY = "/opt/openclaw/node_modules/openclaw/openclaw.mjs"


def chat_rows(messages):
    """Preserve function names, arguments, responses, and message ordering."""
    rows = []
    names = {
        call.get("id"): (call.get("function") or {}).get("name")
        for message in messages
        for call in message.get("tool_calls") or []
    }
    for message in messages:
        role = message.get("role")
        content = message.get("content") or ""
        if role == "assistant":
            parts = (
                [{"type": "text", "text": content}]
                if isinstance(content, str) and content
                else list(content or [])
            )
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                arguments = function.get("arguments", "{}")
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except ValueError:
                        pass
                parts.append(
                    {
                        "type": "toolCall",
                        "id": call.get("id"),
                        "name": function.get("name"),
                        "arguments": arguments,
                    }
                )
            content = parts
        value = {"role": "toolResult" if role == "tool" else role, "content": content}
        if role == "tool":
            value["toolCallId"] = message.get("tool_call_id")
            value["name"] = (
                message.get("name")
                or message.get("tool_name")
                or names.get(message.get("tool_call_id"))
            )
        rows.append(
            {"type": "message", "message": value, "timestamp": message.get("timestamp")}
        )
    return rows


def trajectory_rows(trajectory):
    """Expose native trajectory messages and observations without inferring content."""
    rows = []
    for step in trajectory["steps"]:
        content = []
        if step.get("message"):
            content.append({"type": "text", "text": step["message"]})
        for call in step.get("tool_calls", []):
            content.append(
                {
                    "type": "toolCall",
                    "id": call["tool_call_id"],
                    "name": call["function_name"],
                    "arguments": call["arguments"],
                }
            )
        rows.append(
            {
                "type": "message",
                "timestamp": step.get("timestamp"),
                "message": {
                    "role": "assistant" if step["source"] == "agent" else "user",
                    "content": content,
                },
            }
        )
        for result in (step.get("observation") or {}).get("results", []):
            rows.append(
                {
                    "type": "message",
                    "message": {
                        "role": "toolResult",
                        "toolCallId": result["source_call_id"],
                        "content": result["content"],
                    },
                }
            )
    return rows


def openclaw_rows():
    """Read full native SQLite history instead of a summarized CLI envelope."""
    environment = os.environ | {
        "OPENCLAW_STATE_DIR": "/tmp/openclaw-benchmark",
        "OPENCLAW_CONFIG_PATH": "/tmp/openclaw-benchmark/openclaw-config.json",
        "HOME": "/tmp/openclaw-benchmark/home",
        "TMPDIR": "/tmp/openclaw-benchmark/tmp",
    }
    completed = subprocess.run(
        [MANAGED_NODE, OPENCLAW_ENTRY, "sessions", "--json", "--limit", "all"],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    # Native diagnostics can precede the command's JSON envelope.
    decoder = json.JSONDecoder()
    sessions = None
    for index, character in enumerate(completed.stdout):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(completed.stdout[index:])
        except ValueError:
            continue
        if isinstance(value, dict) and "sessions" in value:
            sessions = value
            break
    if sessions is None:
        raise RuntimeError("Native OpenClaw session inventory was not exported")
    inventory = LOGS / "openclaw-native-sessions.json"
    inventory.write_text(json.dumps(sessions))
    transcript = LOGS / "openclaw.session.jsonl"
    subprocess.run(
        [
            MANAGED_NODE,
            str(Path(__file__).with_name("export-openclaw-transcript.mjs")),
            str(inventory),
            str(transcript),
        ],
        env=environment,
        check=True,
        timeout=30,
    )
    return [
        json.loads(line) for line in transcript.read_text().split("\n") if line.strip()
    ]


def collect():
    """Read only the isolated trial's native profiles before resources are deleted."""
    journal = LOGS / "trajectory-events.jsonl"
    hermes_result = LOGS / "hermes-result.json"
    if Path("/tmp/openclaw-benchmark/openclaw-config.json").exists():
        rows = openclaw_rows()
    elif (LOGS / "trajectory.json").exists():
        rows = trajectory_rows(json.loads((LOGS / "trajectory.json").read_text()))
    elif journal.exists():
        records = []
        # JSON strings may contain Unicode separators; only LF ends a JSONL record.
        lines = journal.read_text().rstrip("\n").split("\n")
        for index, line in enumerate(lines):
            try:
                records.append(json.loads(line))
            except ValueError:
                # Abrupt termination can leave only the final append incomplete.
                if index != len(lines) - 1:
                    raise
        metadata = next(row for row in records if row["type"] == "metadata")
        steps = {
            row["step"]["step_id"]: row["step"]
            for row in records
            if row["type"] == "step"
        }
        for row in records:
            if row["type"] == "observation":
                steps[row["step_id"]]["observation"] = row["observation"]
        (LOGS / "trajectory.json").write_text(
            json.dumps(
                {
                    "schema_version": "ATIF-v1.7",
                    "session_id": metadata["session_id"],
                    "agent": metadata["agent"],
                    "steps": sorted(steps.values(), key=lambda step: step["step_id"]),
                    "benchmark_evidence": {
                        "source": "native tool event journal",
                        "incomplete_execution": True,
                        "additional_inference_calls": 0,
                    },
                }
            )
        )
        rows = trajectory_rows(json.loads((LOGS / "trajectory.json").read_text()))
    elif (LOGS / "native-transcript.jsonl").exists():
        rows = [
            json.loads(line)
            for line in (LOGS / "native-transcript.jsonl").read_text().split("\n")
            if line.strip()
        ]
    elif (LOGS / "openclaw.session.jsonl").exists():
        # Saved native history supports grading after the isolated profile is retired.
        rows = [
            json.loads(line)
            for line in (LOGS / "openclaw.session.jsonl").read_text().split("\n")
            if line.strip()
        ]
    elif hermes_result.exists():
        rows = chat_rows(json.loads(hermes_result.read_text()).get("messages") or [])
    elif Path("/tmp/hermes-benchmark/state.db").exists():
        environment = os.environ | {"HERMES_HOME": "/tmp/hermes-benchmark"}
        session_path = LOGS / "hermes-session.json"
        session_id = (
            json.loads(session_path.read_text()).get("session_id", "")
            if session_path.exists()
            else ""
        )
        command = [
            "/opt/hermes-python/bin/python3",
            "-I",
            "-c",
            "import sys,json;sys.path[:0]=['/opt/hermes','/opt/hermes/.venv/lib/python3.14/site-packages'];"
            "from hermes_state import SessionDB;db=SessionDB(read_only=True);"
            "sessions=db.export_all(include_compacted=True);"
            "messages=[m for s in sessions if not sys.argv[2] or s['id']==sys.argv[2] for m in db.get_messages_as_conversation(s['id'],include_compacted=True)];"
            "open(sys.argv[1],'w').write(json.dumps(messages));db.close()",
            str(LOGS / "hermes-native-messages.json"),
            session_id,
        ]
        subprocess.run(command, env=environment, check=True, timeout=30)
        rows = chat_rows(json.loads((LOGS / "hermes-native-messages.json").read_text()))
    else:
        raise RuntimeError("Native trial history is unavailable for grading")
    COMPATIBILITY.parent.mkdir(parents=True, exist_ok=True)
    COMPATIBILITY.write_text(
        "".join(json.dumps(row, default=str) + "\n" for row in rows)
    )
    (LOGS / "native-transcript.jsonl").write_text(COMPATIBILITY.read_text())
    (LOGS / "transcript-export.json").write_text(
        json.dumps(
            {
                "rows": len(rows),
                "message_rows": sum(row.get("type") == "message" for row in rows),
                "source": "native agent state",
                "additional_inference_calls": 0,
            }
        )
        + "\n"
    )
