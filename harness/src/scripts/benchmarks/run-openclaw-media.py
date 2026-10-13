"""Keep the native Gateway alive through background media and completion turns."""

import argparse
import json
import os
import signal
import socket
import subprocess
import time
from pathlib import Path

SETTINGS = json.loads(Path(__file__).with_name("pilot.json").read_text())
LOG_DIRECTORY = Path("/logs/agent")
ACTIVITY_PATH = LOG_DIRECTORY / "openclaw-native-activity.json"
PROCESS_PATH = LOG_DIRECTORY / "openclaw-gateway-process.json"
STARTUP_TIMEOUT_SECONDS = 60
INFERENCE_LEDGER_PAIRS = (
    ("inference-events.jsonl", "requests.jsonl"),
    ("image-inference-events.jsonl", "image-requests.jsonl"),
)


def read_records(path: Path) -> list[dict]:
    """Read complete append-only records while a native transport is writing."""
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text().splitlines(keepends=True)
        if line.endswith("\n") and line.strip()
    ]


def pending_inferences(directory: Path = LOG_DIRECTORY) -> int:
    """Keep containers alive until every observed subscription stream settles."""
    pending = set()
    for events_name, requests_name in INFERENCE_LEDGER_PAIRS:
        finished = {
            row["inferenceId"] for row in read_records(directory / requests_name)
        }
        pending.update(
            row["inferenceId"]
            for row in read_records(directory / events_name)
            if row.get("event") == "started" and row["inferenceId"] not in finished
        )
    return len(pending)


def start(node: str, entrypoint: str) -> None:
    """Start an isolated native Gateway during agent setup, before its task budget."""
    settings = SETTINGS["media_generation"]
    env = dict(os.environ)
    env["LEON_BENCHMARK_GATEWAY_ACTIVITY_PATH"] = str(ACTIVITY_PATH)
    command = [
        node,
        entrypoint,
        "gateway",
        "run",
        "--bind",
        "loopback",
        "--auth",
        "none",
        "--allow-unconfigured",
        "--port",
        str(settings["gateway_port"]),
    ]
    with (LOG_DIRECTORY / "openclaw-gateway.txt").open("w") as log:
        process = subprocess.Popen(
            command,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    PROCESS_PATH.write_text(json.dumps({"pid": process.pid, "entrypoint": entrypoint}))
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Native Gateway stopped during setup; inspect its log")
        try:
            with socket.create_connection(("127.0.0.1", settings["gateway_port"]), 1):
                if ACTIVITY_PATH.exists():
                    return
        except OSError:
            pass
        time.sleep(0.2)
    process.terminate()
    raise RuntimeError("Native Gateway did not become ready during setup")


def run(
    node: str, entrypoint: str, instruction: str, session_id: str, timeout: int
) -> None:
    """Wait for the native activity inventory without injecting conversation turns."""
    settings = SETTINGS["media_generation"]
    deadline = time.monotonic() + timeout
    process = json.loads(PROCESS_PATH.read_text())
    command = [
        node,
        entrypoint,
        "agent",
        "--json",
        "--session-id",
        session_id,
        "--thinking",
        SETTINGS["reasoning"],
        "--timeout",
        str(timeout),
        "--message-file",
        instruction,
    ]
    idle_since = None
    try:
        with (LOG_DIRECTORY / "openclaw.txt").open("w") as log:
            subprocess.run(
                command,
                stdout=log,
                stderr=log,
                check=True,
                timeout=max(0.1, deadline - time.monotonic()),
            )
        while time.monotonic() < deadline:
            os.kill(process["pid"], 0)
            activity = json.loads(ACTIVITY_PATH.read_text())
            age = time.time() * 1_000 - activity["observedAt"]
            fresh = (
                activity["pid"] == process["pid"]
                and 0 <= age <= settings["gateway_activity_poll_ms"] * 4
            )
            unsettled = pending_inferences()
            if fresh and activity["idle"] and unsettled == 0:
                idle_since = idle_since or time.monotonic()
                if (
                    time.monotonic() - idle_since
                    >= settings["gateway_idle_confirm_seconds"]
                ):
                    (LOG_DIRECTORY / "openclaw-media-completion.json").write_text(
                        json.dumps(
                            {
                                "native_inventory": activity,
                                "unsettled_subscription_streams": unsettled,
                                "no_injected_turns": True,
                            }
                        )
                    )
                    return
            else:
                idle_since = None
            time.sleep(settings["gateway_activity_poll_ms"] / 1_000)
        raise TimeoutError("Native media work exceeded the published task budget")
    finally:
        try:
            from native_transcript import openclaw_rows

            # Gateway CLI envelopes contain only a reply summary. Save the
            # actual native history before its SQLite owner is stopped.
            rows = openclaw_rows()
            (LOG_DIRECTORY / "native-transcript.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows)
            )
        finally:
            try:
                os.kill(process["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass


def main() -> None:
    """Select setup or one native task execution in its container-owned profile."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--node", required=True)
    parser.add_argument("--entrypoint", required=True)
    parser.add_argument("--start", action="store_true")
    parser.add_argument("--instruction")
    parser.add_argument("--session-id")
    parser.add_argument("--timeout", type=int)
    args = parser.parse_args()
    if args.start:
        start(args.node, args.entrypoint)
    elif args.instruction and args.session_id and args.timeout:
        run(args.node, args.entrypoint, args.instruction, args.session_id, args.timeout)
    else:
        parser.error(
            "Native task execution requires instruction, session ID and timeout"
        )


if __name__ == "__main__":
    main()
