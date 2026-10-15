"""Coordinate local benchmark lanes before environments start consuming resources."""

import contextlib
import fcntl
import json
import os
import time
import uuid
from pathlib import Path

import toml

GIB = 1_024**3


def alive(pid):
    """Retain reservations only while their owning process exists."""
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True
    return True


def save_unique(path, value):
    """Publish state without sharing temporary filenames between lane processes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.new")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


class SharedPool:
    """Reserve CPU, memory and disk across independent benchmark processes."""

    def __init__(self, output, policy):
        self.output = output
        self.policy = policy
        self.path = output / "admission-pool.json"
        self.pid = os.getpid()
        with self.state() as state:
            state["participants"][str(self.pid)] = True

    @contextlib.contextmanager
    def state(self):
        """Serialize resource decisions and discard reservations from dead owners."""
        with (self.output / "admission-pool.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = (
                json.loads(self.path.read_text())
                if self.path.exists()
                else {"participants": {}, "leases": {}, "cooldown_until": 0}
            )
            state["participants"] = {
                pid: value for pid, value in state["participants"].items() if alive(pid)
            }
            state["leases"] = {
                name: value
                for name, value in state["leases"].items()
                if alive(value["pid"])
            }
            try:
                yield state
            finally:
                save_unique(self.path, state)

    def legacy(self, state):
        """Protect a live older runner until its next safe scheduling handoff."""
        path = self.output / "status.json"
        if not path.exists():
            return []
        status = json.loads(path.read_text())
        pid = status.get("pid")
        if (
            not pid
            or not alive(pid)
            or str(pid) in state["participants"]
            or status.get("status") in ("stopped", "finished")
        ):
            return []
        name = status.get("task")
        suite = status.get("suite")
        task_file = self.output / "tasks" / str(suite) / str(name) / "task.toml"
        task = toml.load(task_file)["environment"] if task_file.exists() else {}
        slots = self.policy["legacy_trials"]
        group = self.output / "jobs" / f"{suite}--{name}"
        mode = {
            "oracle validation": "oracle",
            "native runtime setup validation": "setup",
            "agent execution": "native",
        }.get(status.get("status"))
        config_file = group / str(mode) / "config.json"
        if config_file.exists():
            slots = max(
                slots, json.loads(config_file.read_text())["n_concurrent_trials"]
            )
        return [
            {
                "pid": pid,
                "cpus": task.get("cpus", 2),
                "memory_mb": task.get("memory_mb", 4_096),
                "disk_bytes": self.policy["disk_estimate_per_trial_gib"] * GIB,
                "workspace_bytes": self.policy["disk_estimate_per_trial_gib"] * GIB,
                "kind": "trial",
                "directory": str(group / str(mode)),
            }
            for _ in range(slots)
        ]

    def acquire(
        self,
        name,
        task,
        snapshot,
        directory=None,
        kind="trial",
        disk_bytes=None,
        workspace_bytes=None,
    ):
        """Reserve headroom atomically; queued work never consumes agent timeouts."""
        key = f"{self.pid}:{name}"
        estimate = self.policy["disk_estimate_per_trial_gib"] * GIB
        request = {
            "pid": self.pid,
            "cpus": task["cpus"],
            "memory_mb": task["memory_mb"],
            "disk_bytes": estimate if disk_bytes is None else disk_bytes,
            "workspace_bytes": estimate if workspace_bytes is None else workspace_bytes,
            "kind": kind,
            "directory": str(directory) if directory else None,
        }
        with self.state() as state:
            if key in state["leases"]:
                return None
            leases = [*state["leases"].values(), *self.legacy(state)]
            self.check_throttles(state, [*leases, request])
            if time.time() < state["cooldown_until"]:
                return "shared_provider_backoff"
            if (
                kind == "trial"
                and sum(row["kind"] == "trial" for row in leases)
                >= self.policy["host_maximum_trials"]
            ):
                return "shared_concurrency_limit"
            if (
                sum(row["cpus"] for row in leases) + request["cpus"]
                > snapshot["cpus"] - self.policy["cpu_reserve"]
            ):
                return "shared_cpu_headroom"
            if (
                sum(row["memory_mb"] for row in leases) * 1_024**2
                + request["memory_mb"] * 1_024**2
                + self.policy["memory_reserve_gib"] * GIB
                > snapshot["memory_available"]
            ):
                return "shared_memory_headroom"
            for field, available in (
                ("disk_bytes", "disk_root_free"),
                ("workspace_bytes", "disk_workspace_free"),
            ):
                if sum(row[field] for row in leases) + request[field] + self.policy[
                    "disk_reserve_gib"
                ] * GIB > snapshot.get(available, snapshot["disk_free"]):
                    return "shared_disk_headroom"
            state["leases"][key] = request
            return None

    def check_throttles(self, state, leases):
        """Carry provider backoff across lanes and completed environments."""
        paths = {row["directory"] for row in leases if row.get("directory")}
        for directory_path in paths:
            for path in Path(directory_path).glob("*/agent/requests.jsonl"):
                try:
                    lines = path.read_text().splitlines()
                except FileNotFoundError:
                    continue
                seen = state.setdefault("throttles", {})
                start = seen.get(str(path), 0)
                for index, line in enumerate(lines[start:], start=start):
                    try:
                        status = json.loads(line).get("status", 0)
                    except json.JSONDecodeError:
                        break
                    if status in self.policy["backoff_statuses"]:
                        state["cooldown_until"] = (
                            time.time() + self.policy["backoff_seconds"]
                        )
                    seen[str(path)] = index + 1

    def release(self, name):
        """Release reservations after grading, export and environment cleanup."""
        with self.state() as state:
            self.check_throttles(state, list(state["leases"].values()))
            state["leases"].pop(f"{self.pid}:{name}", None)
