"""Admit isolated benchmark trials without changing published task budgets."""

import asyncio
import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from scripts.benchmarks.shared_admission import SharedPool

GIB = 1_024**3


def resources(output: Path) -> dict:
    """Read host headroom without inspecting credentials or making model calls."""
    memory = {
        line.split(":", 1)[0]: int(line.split()[1]) * 1_024
        for line in Path("/proc/meminfo").read_text().splitlines()
        if line.startswith(("MemTotal:", "MemAvailable:"))
    }
    return {
        "cpus": len(os.sched_getaffinity(0)),
        "memory_available": memory["MemAvailable"],
        "disk_free": min(shutil.disk_usage(path).free for path in (Path("/"), output)),
        "disk_root_free": shutil.disk_usage("/").free,
        "disk_workspace_free": shutil.disk_usage(output).free,
    }


def capacity(snapshot: dict, task: dict, policy: dict) -> int:
    """Keep CPU, RAM and estimated writable-layer headroom for the owner."""
    bounds = (
        policy["maximum_trials"],
        (snapshot["cpus"] - policy["cpu_reserve"]) // task["cpus"],
        (snapshot["memory_available"] - policy["memory_reserve_gib"] * GIB)
        // (task["memory_mb"] * 1_024**2),
        (snapshot["disk_free"] - policy["disk_reserve_gib"] * GIB)
        // (policy["disk_estimate_per_trial_gib"] * GIB),
    )
    slots = max(0, min(bounds))
    if slots >= policy["initial_trials"]:
        slots -= slots % policy["initial_trials"]
    return slots


class Admission:
    """Limit live environments and grow concurrency after audited real trials."""

    def __init__(
        self, directory, output, task, policy, read_quota, quota_available, ceiling
    ):
        self.directory = directory
        self.output = output
        self.task = task
        self.policy = policy
        self.read_quota = read_quota
        self.quota_available = quota_available
        self.initial_resources = resources(output)
        self.maximum = min(
            ceiling, max(1, capacity(self.initial_resources, task, policy))
        )
        self.limit = min(self.maximum, policy["initial_trials"])
        self.active = set()
        self.completed = 0
        self.peak = 0
        self.healthy = True
        self.allowance = False
        self.quota_checked_at = float("-inf")
        self.cooldown_until = 0
        self.seen_throttles = set()
        self.lock = asyncio.Lock()
        self.pool = (
            SharedPool(output, policy) if policy.get("shared_admission") else None
        )

    def record(self, action, **details):
        """Keep queue and environment timing separate from agent execution."""
        value = {
            "at": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "active": sorted(self.active),
            "limit": self.limit,
            "maximum": self.maximum,
            "completed": self.completed,
            "peak": self.peak,
            **details,
        }
        with (self.directory / "scheduling.jsonl").open("a") as stream:
            stream.write(json.dumps(value) + "\n")

    def check_throttles(self):
        """Pause new admissions on provider throttling; let active trials finish."""
        for path in self.directory.glob("*/agent/requests.jsonl"):
            for index, line in enumerate(path.read_text().splitlines()):
                try:
                    status = json.loads(line).get("status", 0)
                except json.JSONDecodeError:
                    continue
                key = (str(path), index)
                if (
                    status in self.policy["backoff_statuses"]
                    and key not in self.seen_throttles
                ):
                    self.seen_throttles.add(key)
                    self.cooldown_until = (
                        time.monotonic() + self.policy["backoff_seconds"]
                    )
                    self.limit = min(self.limit, self.policy["initial_trials"])
                    self.healthy = False
                    self.record(
                        "provider_backoff", status=status, trial=path.parent.parent.name
                    )

    async def admit(self, event):
        """Wait before environment timers begin, without shortening task budgets."""
        queued_at = time.monotonic()
        async with self.lock:
            self.record("queued", trial=event.trial_name)
        last_reason = None
        while True:
            async with self.lock:
                now = time.monotonic()
                self.check_throttles()
                snapshot = resources(self.output)
                reason = None
                if len(self.active) >= self.limit:
                    reason = "concurrency_limit"
                elif snapshot["memory_available"] < (
                    self.policy["memory_reserve_gib"] * GIB
                    + self.task["memory_mb"] * 1_024**2
                ):
                    reason = "memory_headroom"
                elif (
                    snapshot["disk_free"]
                    < (
                        self.policy["disk_reserve_gib"]
                        + self.policy["disk_estimate_per_trial_gib"]
                    )
                    * GIB
                ):
                    reason = "disk_headroom"
                elif now < self.cooldown_until:
                    reason = "provider_backoff"
                else:
                    if (
                        now - self.quota_checked_at
                        >= self.policy["quota_refresh_seconds"]
                    ):
                        try:
                            self.allowance = self.quota_available(
                                await asyncio.to_thread(self.read_quota)
                            )
                        except (RuntimeError, TimeoutError, OSError):
                            self.allowance = False
                        self.quota_checked_at = time.monotonic()
                    if not self.allowance:
                        reason = "subscription_allowance"
                if reason is None:
                    if self.pool:
                        reason = self.pool.acquire(
                            event.trial_name,
                            self.task,
                            snapshot,
                            directory=self.directory,
                        )
                if reason is None:
                    self.active.add(event.trial_name)
                    self.peak = max(self.peak, len(self.active))
                    self.record(
                        "admitted",
                        trial=event.trial_name,
                        queue_seconds=time.monotonic() - queued_at,
                        resources=snapshot,
                    )
                    return
                if reason != last_reason:
                    self.record("waiting", trial=event.trial_name, reason=reason)
                    last_reason = reason
            await asyncio.sleep(self.policy["admission_poll_seconds"])

    async def release(self, event, validated=True):
        """Free an admission only after artifact export, grading and cleanup."""
        async with self.lock:
            if event.trial_name not in self.active:
                return
            self.active.remove(event.trial_name)
            if self.pool:
                self.pool.release(event.trial_name)
            self.completed += 1
            self.healthy = self.healthy and validated
            self.check_throttles()
            if self.healthy:
                # Outcomes never decide admission: only configuration and host health do.
                if self.completed >= self.policy["initial_trials"]:
                    self.limit = min(self.maximum, self.policy["second_stage_trials"])
                if self.completed >= self.policy["second_stage_trials"]:
                    self.limit = self.maximum
            self.record("released", trial=event.trial_name, validated=validated)
