"""Preserve native prerequisite diagnostics without altering healthcheck commands."""

import json
from datetime import datetime, timezone

from harbor.environments.base import BaseEnvironment

MAX_DIAGNOSTIC_CHARACTERS = 100_000


def install():
    """Record failed setup output before a task-owned container is deleted."""
    original = BaseEnvironment.run_healthcheck
    if getattr(original, "benchmark_observed", False):
        return

    async def observed(self, healthcheck=None):
        native_exec = self.exec
        check = (
            healthcheck if healthcheck is not None else self.task_env_config.healthcheck
        )
        if check is None:
            return await original(self, healthcheck)

        async def execute(command, *args, **kwargs):
            result = await native_exec(command, *args, **kwargs)
            if command == check.command:
                destination = (
                    self.trial_paths.agent_dir / "environment-healthcheck.jsonl"
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("a") as stream:
                    stream.write(
                        json.dumps(
                            {
                                "command": command,
                                "return_code": result.return_code,
                                "stdout": (result.stdout or "")[
                                    -MAX_DIAGNOSTIC_CHARACTERS:
                                ],
                                "stderr": (result.stderr or "")[
                                    -MAX_DIAGNOSTIC_CHARACTERS:
                                ],
                                "recorded_at": datetime.now(timezone.utc).isoformat(),
                            }
                        )
                        + "\n"
                    )
            return result

        self.exec = execute
        try:
            return await original(self, healthcheck)
        finally:
            self.exec = native_exec

    observed.benchmark_observed = True
    BaseEnvironment.run_healthcheck = observed
