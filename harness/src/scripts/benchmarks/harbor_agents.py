"""Run native assistant runtimes in isolated Harbor task containers."""

import asyncio
import json
import math
import os
import shlex
import tempfile
import tomllib
from pathlib import Path

import yaml
from harbor.agents.installed.base import BaseInstalledAgent
from harbor.agents.installed.openclaw import OpenClaw
from harbor.agents.installed.hermes import Hermes
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.task.config import TaskConfig

LEON_DIRECTORY = "/opt/leon"
OPENCLAW_DIRECTORY = "/opt/openclaw"
LOG_DIRECTORY = "/logs/agent"
MANIFEST_PATH = Path(__file__).with_name("pilot.json")
SETTINGS = json.loads(MANIFEST_PATH.read_text())
ROOT = MANIFEST_PATH.parents[2]
SUBSCRIPTION_PATH = "/run/leon-benchmark-subscription.json"
SUBSCRIPTION_LOCK = asyncio.Lock()
SUBSCRIPTION_BASE_URL = "https://chatgpt.com/backend-api/codex"
MANAGED_NODE = "/tmp/leon-benchmark/bin/node/bin/node"


def native_task_timeout(environment: BaseEnvironment) -> int:
    """Give native CLIs the published task budget rather than a shorter default."""
    config = TaskConfig.model_validate(
        tomllib.loads((environment.environment_dir.parent / "task.toml").read_text())
    )
    return math.ceil(config.agent.timeout_sec)


def native_openclaw_entrypoint() -> str:
    """Resolve the isolated package layout recorded for this comparison phase."""
    runtime = json.loads(Path(os.environ["LEON_BENCHMARK_RUNTIME"]).read_text())
    return runtime.get("openclaw_entrypoint", f"{OPENCLAW_DIRECTORY}/openclaw.mjs")


def native_media_task(environment: BaseEnvironment) -> bool:
    """Identify tasks whose declared native capability includes image generation."""
    return environment.environment_dir.parent.name in SETTINGS.get(
        "media_generation", {}
    ).get("native_gateway_tasks", [])


async def native_workspace(environment: BaseEnvironment) -> str:
    """Respect each task's working directory rather than another suite's layout."""
    result = await environment.exec("pwd")
    workspace = result.stdout.strip()
    if result.return_code or not workspace.startswith("/"):
        raise RuntimeError("Could not resolve the task working directory")
    return workspace


def native_node(environment: BaseEnvironment) -> str:
    """Reuse the declared native runtime where task images provide no Node.js."""
    if environment.task_env_config.env.get("LEON_BENCHMARK_SUITE") == "terminal":
        return MANAGED_NODE
    return "node"


def inference_environment() -> dict[str, str]:
    """Point native runtimes at trial-owned, short-lived subscription access."""
    runtime = json.loads(Path(os.environ["LEON_BENCHMARK_RUNTIME"]).read_text())
    browser = runtime.get("browser_executable")
    # Task images can carry developer proxy settings that are unreachable from
    # independent trials. Subscription calls must use the host's working route.
    return {
        "LEON_BENCHMARK_SUBSCRIPTION_PATH": SUBSCRIPTION_PATH,
        **({"AGENT_BROWSER_EXECUTABLE_PATH": browser} if browser else {}),
        **{
            name: ""
            for name in (
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "ALL_PROXY",
                "http_proxy",
                "https_proxy",
                "all_proxy",
            )
        },
    }


async def provision_subscription(environment: BaseEnvironment) -> None:
    """Resolve native logins on the host without exporting rotating refresh tokens."""
    async with SUBSCRIPTION_LOCK:
        runtime = json.loads(Path(os.environ["LEON_BENCHMARK_RUNTIME"]).read_text())
        leon_source = Path(
            runtime.get(
                "leon_host_source", ROOT / "tmp/benchmarks/comparison/source/leon"
            )
        )
        hermes_source = Path(
            runtime.get(
                "hermes_host_source", ROOT / "tmp/benchmarks/comparison/source/hermes"
            )
        )
        hermes_imports = runtime.get(
            "hermes_host_import_paths",
            [str(hermes_source), *runtime["hermes_import_paths"][1:]],
        )
        with tempfile.TemporaryDirectory(prefix="leon-benchmark-auth-") as directory:
            export = Path(directory) / "subscription.json"
            hermes_command = [
                runtime.get("hermes_host_python", runtime["hermes_command"][0]),
                "-I",
                "-c",
                "import sys,runpy; "
                f"sys.path[:0]={hermes_imports!r}; "
                f"runpy.run_path({str(ROOT / 'scripts/benchmarks/export_subscription.py')!r},run_name='__main__')",
                str(export),
            ]
            for command in (
                hermes_command,
                [
                    "pnpm",
                    "exec",
                    "tsx",
                    "scripts/benchmarks/export-subscription.ts",
                    str(export),
                ],
            ):
                process = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=leon_source,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                if await process.wait():
                    raise RuntimeError(
                        "Could not resolve configured subscription authorization"
                    )

            credentials = json.loads(export.read_text())
            if (
                credentials["hermes"]["base_url"].rstrip("/") != SUBSCRIPTION_BASE_URL
                or credentials["leon"]["auth_kind"] != "chatgpt"
                or credentials["leon"]["chatgpt_account_id"]
                != credentials["hermes"]["chatgpt_account_id"]
            ):
                raise RuntimeError(
                    "All benchmark agents must use the same ChatGPT subscription"
                )

            await environment.upload_file(export, SUBSCRIPTION_PATH)
            identity = await environment.exec("id -u && id -g")
            identifiers = identity.stdout.split()
            if (
                identity.return_code
                or len(identifiers) != 2
                or not all(value.isdecimal() for value in identifiers)
            ):
                raise RuntimeError("Could not resolve the task's execution identity")

            owner = ":".join(identifiers)
            # Docker uploads run outside the task user's identity. Keep access
            # private while making the authorization readable by that user.
            permission = await environment.exec(
                f"chown {owner} {SUBSCRIPTION_PATH} && chmod 600 {SUBSCRIPTION_PATH}",
                user="root",
            )
            readable = await environment.exec(f"test -r {SUBSCRIPTION_PATH}")
            if permission.return_code or readable.return_code:
                raise RuntimeError(
                    "Task user cannot read its private subscription access"
                )


class Leon(BaseInstalledAgent):
    """Use Leon's real agent loop and its installed bridge/tool dependencies."""

    @staticmethod
    def name() -> str:
        return "leon"

    async def install(self, environment: BaseEnvironment) -> None:
        await self.exec_as_agent(
            environment,
            f"test -r {LEON_DIRECTORY}/pnpm-workspace.yaml && "
            f"test -r {LEON_DIRECTORY}/scripts/benchmarks/run-leon.ts && "
            f"{MANAGED_NODE} --version",
        )
        runtime = json.loads(Path(os.environ["LEON_BENCHMARK_RUNTIME"]).read_text())
        if runtime.get("browser_executable"):
            # Leon connects to an existing CDP browser. Each trial owns its
            # browser and profile, which disappear with the task container.
            script = (
                "import json,subprocess,time\n"
                "from pathlib import Path\n"
                "profile=Path('/tmp/leon-benchmark/browser-profile')\n"
                "profile.mkdir(parents=True,exist_ok=True)\n"
                "settings=Path('/tmp/leon-benchmark/profiles/benchmark/tools/browser_use/browser-use/settings.json')\n"
                "settings.parent.mkdir(parents=True,exist_ok=True)\n"
                "settings.write_text(json.dumps({'user_data_dir':str(profile)}))\n"
                f"executable={runtime['browser_executable']!r}\n"
                "with open('/logs/agent/chromium.txt','w') as log:\n"
                "    process=subprocess.Popen([executable,'--headless','--no-sandbox',"
                "'--disable-dev-shm-usage','--remote-debugging-port=0',"
                "'--user-data-dir='+str(profile),'about:blank'],"
                "stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)\n"
                "    deadline=time.monotonic()+20\n"
                "    while not (profile/'DevToolsActivePort').exists():\n"
                "        if process.poll() is not None or time.monotonic()>deadline:\n"
                "            raise RuntimeError('Trial Chromium CDP startup failed')\n"
                "        time.sleep(0.1)\n"
            )
            await self.exec_as_agent(environment, "python3 -c " + shlex.quote(script))

    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        await provision_subscription(environment)
        instruction_path = self.logs_dir / "instruction.txt"
        instruction_path.write_text(instruction)
        env = inference_environment() | {
            "LEON_HOME": "/tmp/leon-benchmark",
            "LEON_CODEBASE_PATH": LEON_DIRECTORY,
            "LEON_BENCHMARK_MODEL": str(self.model_name),
            "LEON_BENCHMARK_REASONING": SETTINGS["reasoning"],
            "LEON_BENCHMARK_SPEED": "fast",
            "LEON_PNPM_BIN": "/tmp/leon-benchmark/bin/pnpm/pnpm",
            "LEON_NODE_BIN": "/tmp/leon-benchmark/bin/node/bin/node",
        }
        env["LEON_BENCHMARK_WORKDIR"] = await native_workspace(environment)
        command = (
            'export PATH="/tmp/leon-benchmark/bin/node/bin:/tmp/leon-benchmark/bin/pnpm:$PATH"\n'
            f"cd {LEON_DIRECTORY} && "
            "/tmp/leon-benchmark/bin/pnpm/pnpm exec tsx scripts/benchmarks/run-leon.ts "
            f"{LOG_DIRECTORY}/instruction.txt {LOG_DIRECTORY} "
            f"> {LOG_DIRECTORY}/leon.txt 2>&1"
        )
        try:
            await self.exec_as_agent(environment, command, env=env)
        finally:
            await self.exec_as_agent(
                environment,
                "if [ -d /tmp/leon-benchmark/profiles/benchmark/logs ]; then\n"
                f"  cp -r /tmp/leon-benchmark/profiles/benchmark/logs {LOG_DIRECTORY}/leon-logs\n"
                "fi",
            )

    def populate_context_post_run(self, context: AgentContext) -> None:
        result_path = self.logs_dir / "leon-result.json"
        if not result_path.is_file():
            return
        records = json.loads(result_path.read_text())["usage"]
        usage = [record["usage"] for record in records]
        if usage and all("inputTokens" in item for item in usage):
            context.n_input_tokens = sum(item["inputTokens"] for item in usage)
        if usage and all("outputTokens" in item for item in usage):
            context.n_output_tokens = sum(item["outputTokens"] for item in usage)
        if usage and all("cachedInputTokens" in item for item in usage):
            context.n_cache_tokens = sum(item["cachedInputTokens"] for item in usage)
        if usage and all("costUSD" in item for item in usage):
            context.cost_usd = sum(item["costUSD"] for item in usage)


class BenchmarkOpenClaw(OpenClaw):
    """Keep OpenClaw's native trace conversion with explicit model settings."""

    async def install(self, environment: BaseEnvironment) -> None:
        await self.exec_as_agent(
            environment,
            f"{MANAGED_NODE} {native_openclaw_entrypoint()} --version",
        )
        self._native_execution_env = await self.prepare_environment(environment)
        if self.uses_native_media_gateway(environment):
            arguments = [
                "python3",
                f"{LEON_DIRECTORY}/scripts/benchmarks/run-openclaw-media.py",
                "--node",
                MANAGED_NODE,
                "--entrypoint",
                native_openclaw_entrypoint(),
                "--start",
            ]
            await self.exec_as_agent(
                environment, shlex.join(arguments), env=self._native_execution_env
            )

    @staticmethod
    def uses_native_media_gateway(environment: BaseEnvironment) -> bool:
        """Preserve asynchronous media lifecycle for tasks requiring image generation."""
        return native_media_task(environment)

    async def prepare_environment(self, environment: BaseEnvironment) -> dict[str, str]:
        """Prepare native configuration and credentials outside the task budget."""
        await provision_subscription(environment)
        runtime = json.loads(Path(os.environ["LEON_BENCHMARK_RUNTIME"]).read_text())
        model = (
            runtime.get("openclaw_provider", "openai-codex")
            + "/"
            + str(self.model_name).split("/", 1)[1]
        )
        provider, model_id = model.split("/", 1)
        workspace = await native_workspace(environment)
        writable = await environment.exec(f"test -w {shlex.quote(workspace)}")
        node = MANAGED_NODE
        config = {
            "agents": {
                "defaults": {
                    "workspace": workspace,
                    # Restricted task roots permit edits only in designated
                    # subdirectories; native bootstrap files must respect that.
                    "skipBootstrap": writable.return_code != 0,
                    "model": {"primary": model},
                    "thinkingDefault": SETTINGS["reasoning"],
                    "models": {
                        model: {
                            **(
                                {"agentRuntime": {"id": "openclaw"}}
                                if runtime.get("openclaw_provider") == "openai"
                                else {}
                            ),
                            "params": {
                                "serviceTier": SETTINGS["service_tier"],
                                "fastMode": "on",
                                "transport": "sse",
                            },
                        }
                    },
                }
            },
            "models": {
                "providers": {
                    provider: {
                        "baseUrl": "https://chatgpt.com/backend-api/codex",
                        "api": runtime.get("openclaw_api", "openai-codex-responses"),
                        "models": [
                            {
                                "id": model_id,
                                "name": model_id,
                                "reasoning": True,
                                "input": ["text", "image"],
                                "contextWindow": 1_050_000,
                                "maxTokens": 128_000,
                            }
                        ],
                    }
                }
            },
            "tools": {
                "exec": {"host": "gateway", "security": "full", "ask": "off"},
                "web": {
                    "search": {
                        "enabled": True,
                        "openaiCodex": {"enabled": True, "mode": "live"},
                    }
                },
            },
        }
        if runtime.get("browser_executable"):
            config["browser"] = {
                "enabled": True,
                "headless": True,
                "noSandbox": True,
                "executablePath": runtime["browser_executable"],
            }
        if self.uses_native_media_gateway(environment):
            config["agents"]["defaults"]["utilityModel"] = model
            config["gateway"] = {
                "mode": "local",
                "bind": "loopback",
                "port": SETTINGS["media_generation"]["gateway_port"],
                "auth": {"mode": "none"},
            }
        config_path = self.logs_dir / "openclaw-config.json"
        config_path.write_text(json.dumps(config, indent=2))
        env = inference_environment() | {
            "HOME": "/tmp/openclaw-benchmark/home",
            "TMPDIR": "/tmp/openclaw-benchmark/tmp",
            "OPENCLAW_STATE_DIR": "/tmp/openclaw-benchmark",
            "OPENCLAW_CONFIG_PATH": "/tmp/openclaw-benchmark/openclaw-config.json",
            "NODE_OPTIONS": f"--import {LEON_DIRECTORY}/scripts/benchmarks/configure-openclaw.mjs",
            "LEON_BENCHMARK_OPENCLAW_ENTRYPOINT": native_openclaw_entrypoint(),
        }
        await environment.exec(
            f"{node} --input-type=module -e "
            + shlex.quote(
                "import fs from 'node:fs'; "
                f"const auth=JSON.parse(fs.readFileSync('{SUBSCRIPTION_PATH}','utf8')).hermes; "
                "const directory='/tmp/openclaw-benchmark/agents/main/agent'; "
                "fs.mkdirSync(directory,{recursive:true}); "
                # Restricted image users may have no writable home or temp root.
                # Native plugin maintenance needs private, task-owned directories.
                f"for(const path of {json.dumps([env['HOME'], env['TMPDIR']])}) "
                "{fs.mkdirSync(path,{recursive:true,mode:0o700});} "
                # Native maintenance changes configuration permissions. The
                # task user must own its copy, independent of host log ownership.
                f"fs.copyFileSync('{LOG_DIRECTORY}/openclaw-config.json',"
                f"'{env['OPENCLAW_CONFIG_PATH']}'); "
                f"fs.chmodSync('{env['OPENCLAW_CONFIG_PATH']}',0o600); "
                "fs.writeFileSync(directory+'/auth-profiles.json',JSON.stringify({"
                f"version:1,profiles:{{'{provider}:benchmark':{{type:'oauth',provider:'{provider}',"
                "access:auth.access_token,refresh:'',expires:auth.expires_at,accountId:auth.chatgpt_account_id}}"
                "}),{mode:0o600});"
            )
        )
        runtime = json.loads(Path(os.environ["LEON_BENCHMARK_RUNTIME"]).read_text())
        if runtime.get("openclaw_migrate_configuration"):
            # Let the installed CLI migrate provider configuration and import
            # ephemeral OAuth profiles into its own credential store.
            await self.exec_as_agent(
                environment,
                f"{node} {native_openclaw_entrypoint()} doctor "
                "--fix --non-interactive "
                f"> {LOG_DIRECTORY}/openclaw-migration.txt 2>&1",
                env=env,
            )
        return env

    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        env = getattr(self, "_native_execution_env", None)
        if env is None:
            env = await self.prepare_environment(environment)

        workspace = await native_workspace(environment)
        timeout = native_task_timeout(environment)
        node = MANAGED_NODE
        command = (
            f"cd {shlex.quote(workspace)} && "
            f"{node} {native_openclaw_entrypoint()} agent --local --json "
            f"--session-id {shlex.quote(str(self.session_id))} "
            f"--thinking {SETTINGS['reasoning']} "
            f"--timeout {timeout} "
            f"--message {shlex.quote(instruction)} "
            f"> {LOG_DIRECTORY}/openclaw.txt 2>&1"
        )
        if self.uses_native_media_gateway(environment):
            (self.logs_dir / "instruction.txt").write_text(instruction)
            command = shlex.join(
                [
                    "python3",
                    f"{LEON_DIRECTORY}/scripts/benchmarks/run-openclaw-media.py",
                    "--node",
                    node,
                    "--entrypoint",
                    native_openclaw_entrypoint(),
                    "--instruction",
                    f"{LOG_DIRECTORY}/instruction.txt",
                    "--session-id",
                    str(self.session_id),
                    "--timeout",
                    str(timeout),
                ]
            )
        try:
            await self.exec_as_agent(environment, command, env=env)
        finally:
            await self._copy_openclaw_session_file_to_agent_logs(environment, env)


class BenchmarkHermes(Hermes):
    """Run Hermes with an isolated home and the same inference configuration."""

    def populate_context_post_run(self, context: AgentContext) -> None:
        super().populate_context_post_run(context)
        result_path = self.logs_dir / "hermes-result.json"
        if not result_path.exists():
            context.n_input_tokens = None
            context.n_output_tokens = None
            return
        result = json.loads(result_path.read_text())
        context.n_input_tokens = result.get("prompt_tokens")
        context.n_output_tokens = result.get("completion_tokens")
        context.n_cache_tokens = result.get("cache_read_tokens")
        context.metadata = {
            "estimated_cost_usd": result.get("estimated_cost_usd"),
            "cost_status": result.get("cost_status"),
            "cost_source": result.get("cost_source"),
        }

    async def install(self, environment: BaseEnvironment) -> None:
        command = os.environ["LEON_BENCHMARK_HERMES_COMMAND"]
        self._hermes_command = json.loads(command)
        await self.exec_as_agent(
            environment,
            f"{shlex.join(self._hermes_command)} --help > /dev/null",
            env={"HERMES_HOME": "/tmp/hermes-benchmark"},
        )

    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        await provision_subscription(environment)
        config = yaml.safe_load(self._build_config_yaml(str(self.model_name)))
        config["provider"] = "openai-codex"
        config["model"] = {
            "default": SETTINGS["model"].split("/", 1)[1],
            "provider": "openai-codex",
        }
        config["agent"]["service_tier"] = "fast"
        config["agent"]["reasoning_effort"] = SETTINGS["reasoning"]
        config["web"] = {
            **config.get("web", {}),
            "search_backend": "openai-native",
            "extract_backend": "keenable",
            "provider_tier": {"keenable": "free"},
        }
        if native_media_task(environment):
            config["image_gen"] = {"provider": "openai-codex"}
        config_path = self.logs_dir / "hermes-config.yaml"
        config_path.write_text(yaml.safe_dump(config))
        env = inference_environment() | {
            "HERMES_HOME": "/tmp/hermes-benchmark",
            "TERMINAL_ENV": "local",
            "HERMES_INSTRUCTION": instruction,
            "LEON_BENCHMARK_HERMES_RUNNER": "1",
            "HERMES_YOLO_MODE": "1",
        }
        await self.exec_as_agent(
            environment,
            f"mkdir -p /tmp/hermes-benchmark && cp {LOG_DIRECTORY}/hermes-config.yaml /tmp/hermes-benchmark/config.yaml",
            env=env,
        )
        await self.exec_as_agent(
            environment,
            f"{shlex.quote(MANAGED_NODE)} --input-type=module -e "
            + shlex.quote(
                "import fs from 'node:fs'; "
                f"const auth=JSON.parse(fs.readFileSync('{SUBSCRIPTION_PATH}','utf8')).hermes; "
                "fs.writeFileSync('/tmp/hermes-benchmark/auth.json',JSON.stringify({"
                "version:1,credential_pool:{'openai-codex':[{"
                "id:'benchmark',label:'subscription',auth_type:'oauth',source:'manual',"
                "access_token:auth.access_token,refresh_token:'',"
                "expires_at:auth.expires_at,base_url:auth.base_url"
                "}]}}),{mode:0o600});"
            ),
            env=env,
        )
        command = (
            f"cd {shlex.quote(await native_workspace(environment))} && "
            f"{shlex.join(self._hermes_command)} "
            f"> {LOG_DIRECTORY}/hermes.txt 2>&1"
        )
        try:
            await self.exec_as_agent(environment, command, env=env)
        finally:
            await self.exec_as_agent(
                environment,
                f"{shlex.join(self._hermes_command)} sessions export "
                f"{LOG_DIRECTORY}/hermes-session.jsonl --source cli",
                env={"HERMES_HOME": "/tmp/hermes-benchmark"},
            )
