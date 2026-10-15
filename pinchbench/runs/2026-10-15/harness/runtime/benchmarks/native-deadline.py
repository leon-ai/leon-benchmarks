"""Enforce the published task deadline inside the isolated trial container."""

import argparse
import json
import os
import signal
import subprocess
import time
from pathlib import Path


def save(path, value):
    """Let the cancellation path read a complete process identity record."""
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value) + "\n")
    temporary.replace(path)


def descendants(parent):
    """Find child workers even when a native runtime creates another session."""
    parents = {}
    for path in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = path.read_text().rsplit(")", 1)[1].split()
            parents[int(path.parent.name)] = int(fields[1])
        except (FileNotFoundError, ProcessLookupError):
            continue
    found = {parent}
    while True:
        expanded = found | {pid for pid, ppid in parents.items() if ppid in found}
        if expanded == found:
            return found
        found = expanded


def terminate(parent):
    """Stop the isolated query and its detached descendants before artifact export."""
    workers = descendants(parent)
    try:
        os.killpg(parent, signal.SIGKILL)
    except ProcessLookupError:
        pass
    for pid in workers:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def main():
    """Keep completed calls intact and stop unfinished writers before grading."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--deadline", type=float)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("command", nargs="?")
    args = parser.parse_args()
    if args.stop:
        if args.journal.exists():
            state = json.loads(args.journal.read_text())
            stat = Path(f"/proc/{state['process_id']}/stat")
            if (
                stat.exists()
                and stat.read_text().rsplit(")", 1)[1].split()[19]
                == state["process_start"]
            ):
                terminate(state["process_id"])
        return
    if args.deadline is None or args.command is None:
        parser.error("a query requires its published deadline and command")
    process = subprocess.Popen(["bash", "-c", args.command], start_new_session=True)
    stat = Path(f"/proc/{process.pid}/stat")
    state = {
        "process_id": process.pid,
        "process_start": stat.read_text().rsplit(")", 1)[1].split()[19],
        "deadline_epoch": args.deadline,
        "expired": False,
    }
    save(args.journal, state)
    expired = False
    try:
        code = process.wait(timeout=max(0, args.deadline - time.time()))
    except subprocess.TimeoutExpired:
        expired = True
        terminate(process.pid)
        process.wait()
        code = 124
    save(
        args.journal,
        state
        | {
            "deadline_epoch": args.deadline,
            "finished_epoch": time.time(),
            "expired": expired,
            "return_code": code,
        },
    )
    raise SystemExit(code)


if __name__ == "__main__":
    main()
