"""Validate native subscription image delivery outside scored candidate coverage."""

import argparse
import asyncio
import json
import os
import shlex
from pathlib import Path

import toml

from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from scripts.benchmarks.harbor_agents import (
    LOG_DIRECTORY,
    MANAGED_NODE,
    SETTINGS,
    BenchmarkOpenClaw,
    native_openclaw_entrypoint,
)
from scripts.benchmarks import balanced, campaign, metrics

IMAGE_OUTPUT = "/tmp_workspace/results/image-preflight.png"
IMAGE_PROMPT = (
    "Minimal flat test image: one blue circle on a white background, no text."
)
PROVIDER_TIMEOUT_MS = 240_000


class OpenClawImageProviderPreflight(BenchmarkOpenClaw):
    """Exercise the installed native image provider with the campaign's OAuth wiring."""

    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        """Use the native CLI provider without requesting a scored agent solution."""
        env = self._native_execution_env
        model = SETTINGS["media_generation"]["image_models"][0]
        arguments = [
            MANAGED_NODE,
            native_openclaw_entrypoint(),
            "infer",
            "image",
            "generate",
            "--model",
            f"openai/{model}",
            "--quality",
            "low",
            "--size",
            "1024x1024",
            "--count",
            "1",
            "--output",
            IMAGE_OUTPUT,
            "--prompt",
            IMAGE_PROMPT,
            "--timeout-ms",
            str(PROVIDER_TIMEOUT_MS),
            "--json",
        ]
        await self.exec_as_agent(
            environment,
            "mkdir -p /tmp_workspace/results\n"
            + shlex.join(arguments)
            + f" > {LOG_DIRECTORY}/native-image-provider.json 2>&1",
            env=env,
        )


async def run(
    output: Path,
    agent_loop: bool = False,
    case: str = "",
    agents: list[str] | None = None,
    require_output: bool = False,
) -> None:
    """Retain native provider evidence and clean up the unscored control container."""
    item = {
        "name": "native-image-agent-preflight"
        if agent_loop
        else "native-image-provider-preflight",
        "domain": "preflight",
    }
    if case:
        item["name"] += "-" + case
    target = output / "tasks/wild" / item["name"]
    (target / "environment").mkdir(parents=True, exist_ok=True)
    (target / "tests").mkdir(exist_ok=True)
    instruction = IMAGE_PROMPT
    if agent_loop:
        instruction = (
            "Use your native image-generation tool to create one PNG: "
            + IMAGE_PROMPT
            + " Save it as /tmp_workspace/results/native-image.png if possible. "
            "Use the native image backend; do not draw or synthesize an image with code. "
            "If the tool completes asynchronously, use its native completion lifecycle."
        )
        if require_output:
            instruction = instruction.replace(" if possible", "")
    (target / "instruction.md").write_text(instruction + "\n")
    control = (
        "import json,hashlib,shutil\nfrom pathlib import Path\n"
        "roots=['/tmp_workspace/results','/tmp/openclaw-benchmark/media',"
        "'/tmp/hermes-benchmark/cache/generated/images',"
        "'/tmp/leon-benchmark/profiles/benchmark/sessions']\n"
        "saved=[]\nexport=Path('/logs/verifier/media-exports');export.mkdir(exist_ok=True)\n"
        "for root in roots:\n"
        " for p in Path(root).rglob('*'):\n"
        "  if not p.is_file():\n   continue\n"
        "  with p.open('rb') as f:header=f.read(8)\n"
        "  if header!=bytes.fromhex('89504e470d0a1a0a'):\n   continue\n"
        "  sha=hashlib.sha256(p.read_bytes()).hexdigest()\n"
        "  shutil.copy2(p,export/(sha+'.png'))\n"
        "  saved.append({'native_path':str(p),'sha256':sha,'bytes':p.stat().st_size})\n"
        "Path('/logs/verifier/image-delivery.json').write_text(json.dumps(saved))\n"
        "Path('/logs/verifier/reward.json').write_text(json.dumps({'overall_score':int(bool(saved))}))\n"
    )
    (target / "tests/test.sh").write_text(
        "#!/bin/bash\nset -eu\n"
        "mkdir -p /logs/verifier\n"
        "python3 - <<'PY'\n"
        + (
            control
            + (
                "p=Path('/tmp_workspace/results/native-image.png')\n"
                "valid=p.is_file() and p.read_bytes().startswith(bytes.fromhex('89504e470d0a1a0a'))\n"
                "Path('/logs/verifier/reward.json').write_text(json.dumps({'overall_score':int(valid)}))\n"
                if require_output
                else ""
            )
            if agent_loop
            else (
                "import json\nfrom pathlib import Path\n"
                f"p=Path({IMAGE_OUTPUT!r})\n"
                "valid=p.is_file() and p.read_bytes().startswith(bytes.fromhex('89504e470d0a1a0a'))\n"
                "Path('/logs/verifier/reward.json').write_text(json.dumps({'overall_score':int(valid)}))\n"
            )
        )
        + "PY\n"
    )
    (target / "task.toml").write_text(
        toml.dumps(
            {
                "version": "1.0",
                "artifacts": ["/tmp_workspace/results"],
                "agent": {"timeout_sec": 300},
                "verifier": {"timeout_sec": 60},
                "environment": {
                    "docker_image": balanced.benchmark.DEFAULT_IMAGE,
                    "workdir": str(Path(IMAGE_OUTPUT).parent.parent),
                    "build_timeout_sec": 600,
                    "cpus": 2,
                    "memory_mb": 4_096,
                    "storage_mb": 10_240,
                },
            }
        )
    )
    balanced.benchmark.configure_runtime(output)
    for name in balanced.PROXY_NAMES:
        os.environ.pop(name, None)
    balanced.report = campaign.report
    state = {"status": "unscored native image provider preflight"}
    await campaign.quota(output, state)
    selection = json.loads((output / "selection.json").read_text())
    baseline = {row["Id"] for row in balanced.docker_objects("image")}
    results = await balanced.execute(
        output,
        selection,
        "wild",
        item,
        "image-preflight",
        baseline,
        agents={
            name: implementation
            for name, implementation in balanced.benchmark.AGENTS.items()
            if not agents or name in agents
        }
        if agent_loop
        else {
            "openclaw": "scripts.benchmarks.image_preflight:OpenClawImageProviderPreflight"
        },
        attempts=1,
        release_images=False,
    )
    group = output / "jobs" / f"wild--{item['name']}" / "image-preflight"
    if agent_loop:
        group = output / "jobs" / f"wild--{item['name']}" / "image-preflight"
        checks = []
        for result in results:
            trial = group / result["trial_name"]
            files = [
                "agent/openclaw-media-completion.json",
                "verifier/image-delivery.json",
            ]
            checks.append(
                {
                    "agent": result["agent_info"]["name"],
                    "exception": result.get("exception_info"),
                    "rewards": (result.get("verifier_result") or {}).get("rewards"),
                    "native_evidence": {
                        name: json.loads((trial / name).read_text())
                        for name in files
                        if (trial / name).exists()
                    },
                    "metrics": metrics.trial_metrics(trial),
                }
            )
        receipt = {"scored_candidate_attempts": 0, "checks": checks}
        balanced.save(output / f"{item['name']}.json", receipt)
        await asyncio.to_thread(balanced.cleanup_job, output, group, True, False)
        print(
            json.dumps(
                {
                    "scored_candidate_attempts": 0,
                    "checks": [
                        {key: row[key] for key in ("agent", "exception", "rewards")}
                        for row in checks
                    ],
                }
            ),
            flush=True,
        )
        return
    result = results[0]
    trial = group / result["trial_name"]
    requests = metrics.lines(trial / "agent/requests.jsonl")
    routing = metrics.lines(trial / "agent/image-host-routing.jsonl")
    receipt = {
        "scored_candidate_attempts": 0,
        "trial": str(trial.relative_to(output)),
        "exception": result.get("exception_info"),
        "native_image_delivery": (result.get("verifier_result") or {}).get("rewards"),
        "requests": requests,
        "host_routing": routing,
        "settings_verified": bool(requests)
        and all(
            row.get("model") == SETTINGS["model"].split("/", 1)[1]
            and (row.get("reasoning") or {}).get("effort") == SETTINGS["reasoning"]
            and row.get("serviceTier") == SETTINGS["service_tier"]
            and row.get("authMode") == "chatgpt_subscription"
            and row.get("endpoint", "").startswith(
                "https://chatgpt.com/backend-api/codex/"
            )
            for row in requests
        ),
        "metrics": metrics.trial_metrics(trial),
    }
    balanced.save(output / "native-image-provider-preflight.json", receipt)
    await asyncio.to_thread(balanced.cleanup_job, output, group, True, False)
    print(
        json.dumps(
            {
                key: receipt[key]
                for key in (
                    "scored_candidate_attempts",
                    "trial",
                    "exception",
                    "native_image_delivery",
                )
            }
        ),
        flush=True,
    )


def main() -> None:
    """Run only the explicit unscored image-provider control."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--agent-loop", action="store_true")
    parser.add_argument("--case", default="")
    parser.add_argument("--agents", nargs="+", choices=list(balanced.benchmark.AGENTS))
    parser.add_argument("--require-output", action="store_true")
    args = parser.parse_args()
    asyncio.run(
        run(
            args.output.resolve(),
            args.agent_loop,
            args.case,
            args.agents,
            args.require_output,
        )
    )


if __name__ == "__main__":
    main()
