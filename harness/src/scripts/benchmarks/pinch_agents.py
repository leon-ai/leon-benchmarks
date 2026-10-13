"""Deliver PinchBench prompts through native conversations within one task budget."""

import json
import math
import shlex
import shutil
import time
from uuid import uuid4

from scripts.benchmarks import harbor_agents, pinch


class PinchConversation:
    """Retain files between turns while honoring explicit fresh conversations."""

    async def install(self, environment):
        """Provision native runtimes and mock prerequisites outside execution time."""
        await super().install(environment)
        self._mock_environment = {}
        pinch.load_tasks()
        task = pinch.task_for_environment(environment)
        if any(
            "fws" in str(value) for value in task.frontmatter.get("prerequisites", [])
        ):
            result = await environment.exec(
                "python3 /opt/leon/scripts/benchmarks/pinch_mock.py",
                env={
                    "PATH": f"{pinch.DEPENDENCIES}/bin:{pinch.DEPENDENCIES}/node_modules/.bin:/tmp/leon-benchmark/bin/node/bin:/usr/local/bin:/usr/bin:/bin"
                },
                timeout_sec=60,
            )
            if result.return_code:
                raise RuntimeError("PinchBench mock service could not start")
            self._mock_environment = json.loads(result.stdout)

    async def exec_as_agent(
        self, environment, command, env=None, cwd=None, timeout_sec=None
    ):
        """Scope prerequisite routing and native conversation IDs to this trial."""
        variables = dict(env or {})
        variables.update(getattr(self, "_mock_environment", {}))
        variables["PATH"] = (
            f"{pinch.DEPENDENCIES}/bin:{pinch.DEPENDENCIES}/node_modules/.bin:"
            "/tmp/leon-benchmark/bin/node/bin:/tmp/leon-benchmark/bin/pnpm:"
            "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        )
        turn = getattr(self, "_turn_directory", None)
        if turn and "exec tsx scripts/benchmarks/run-leon.ts" in command:
            command = command.replace(
                "/logs/agent/instruction.txt /logs/agent ",
                f"/logs/agent/instruction.txt {turn} ",
            )
            variables["LEON_BENCHMARK_WORKDIR"] = pinch.WORKSPACE
            variables["LEON_BENCHMARK_AUDIT_PATH"] = "/logs/agent/requests.jsonl"
            if self._conversation_id:
                variables["LEON_BENCHMARK_SESSION_ID"] = self._conversation_id
        if turn and variables.get("HERMES_INSTRUCTION"):
            variables["PINCHBENCH_TURN_OUTPUT"] = turn
            if self._previous_hermes_result:
                variables["PINCHBENCH_CONVERSATION_HISTORY"] = (
                    self._previous_hermes_result
                )
        if turn and " agent --local --json " in command:
            budget = harbor_agents.native_task_timeout(environment)
            remaining = max(1, math.ceil(self._deadline - time.monotonic()))
            command = command.replace(f"--timeout {budget} ", f"--timeout {remaining} ")
        return await super().exec_as_agent(
            environment, command, env=variables, cwd=cwd, timeout_sec=timeout_sec
        )

    async def run(self, instruction, environment, context):
        """Deliver future prompts only when their preceding native turn finishes."""
        task = pinch.task_for_environment(environment)
        sessions = task.frontmatter.get("sessions") or [{"prompt": instruction}]
        self._deadline = time.monotonic() + task.timeout_seconds
        self._conversation_id = None
        self._previous_hermes_result = None
        turns = []
        try:
            for index, entry in enumerate(sessions):
                session = {"prompt": entry} if isinstance(entry, str) else entry
                if session.get("new_session"):
                    self._conversation_id = None
                    self._previous_hermes_result = None
                    self.session_id = str(uuid4())
                local = self.logs_dir / "turns" / f"{index + 1:02d}"
                local.mkdir(parents=True, exist_ok=True)
                self._turn_directory = f"/logs/agent/turns/{index + 1:02d}"
                started = time.monotonic()
                record = {
                    "index": index + 1,
                    "new_session": bool(session.get("new_session")),
                    "completed": False,
                }
                turns.append(record)
                try:
                    await super().run(session["prompt"], environment, context)
                    leon = local / "leon-result.json"
                    hermes = local / "hermes-result.json"
                    if leon.exists():
                        self._conversation_id = json.loads(leon.read_text())[
                            "sessionId"
                        ]
                        shutil.copyfile(leon, self.logs_dir / leon.name)
                    if hermes.exists():
                        self._previous_hermes_result = (
                            f"{self._turn_directory}/hermes-result.json"
                        )
                        shutil.copyfile(hermes, self.logs_dir / hermes.name)
                    if self.name() == "openclaw":
                        result = await environment.exec(
                            "python3 /opt/leon/scripts/benchmarks/pinch_transcript.py --snapshot-openclaw "
                            + shlex.quote(str(self.session_id))
                            + " "
                            + shlex.quote(self._turn_directory),
                            timeout_sec=60,
                        )
                        if result.return_code:
                            raise RuntimeError(
                                "Native OpenClaw turn transcript was not archived"
                            )
                    record["completed"] = True
                finally:
                    record["elapsed_seconds"] = time.monotonic() - started
                    for filename in ("leon.txt", "openclaw.txt", "hermes.txt"):
                        path = self.logs_dir / filename
                        if path.exists():
                            shutil.copyfile(path, local / filename)
        finally:
            (self.logs_dir / "pinch-turns.json").write_text(
                json.dumps(
                    {"turns": turns, "timeout_seconds": task.timeout_seconds}, indent=2
                )
                + "\n"
            )


class PinchLeon(PinchConversation, harbor_agents.Leon):
    """Use Leon's native session manager for PinchBench conversations."""


class PinchOpenClaw(PinchConversation, harbor_agents.BenchmarkOpenClaw):
    """Use OpenClaw's native session IDs for PinchBench conversations."""


class PinchHermes(PinchConversation, harbor_agents.BenchmarkHermes):
    """Use Hermes' native conversation-history input for PinchBench conversations."""
