"""Run Hermes' public agent with explicit OpenAI inference settings."""

import json
import os
import shutil
from pathlib import Path

from hermes_state import SessionDB
from run_agent import AIAgent

OUTPUT_DIRECTORY = Path("/logs/agent")
SETTINGS = json.loads(Path(__file__).with_name("pilot.json").read_text())


def main() -> None:
    """Keep the native agent loop while configuring its public inference knobs."""
    # A benchmark process owns one query and no later conversation turn.
    # Hermes uses its native finite-session mode to join delegated results.
    os.environ["HERMES_SINGLE_QUERY_SESSION"] = "1"

    task_skills = Path(SETTINGS["task_skills_dir"])
    native_skills = Path(os.environ["HERMES_HOME"]) / "skills"
    if task_skills.is_dir():
        shutil.copytree(task_skills, native_skills, dirs_exist_ok=True)

    (OUTPUT_DIRECTORY / "task-skills.json").write_text(
        json.dumps(
            {
                "registered": task_skills.is_dir(),
                "source": str(task_skills),
                "target": str(native_skills),
            },
            indent=2,
        )
        + "\n"
    )

    database = SessionDB()
    agent = None
    try:
        credentials = json.loads(
            Path(os.environ["LEON_BENCHMARK_SUBSCRIPTION_PATH"]).read_text()
        )["hermes"]
        agent = AIAgent(
            model=SETTINGS["model"].split("/", 1)[1],
            provider="openai-codex",
            requested_provider="openai-codex",
            api_mode="codex_responses",
            base_url=credentials["base_url"],
            api_key=credentials["access_token"],
            enabled_toolsets=["hermes-cli"],
            max_iterations=90,
            reasoning_config={"effort": SETTINGS["reasoning"]},
            service_tier=SETTINGS["service_tier"],
            request_overrides={"service_tier": SETTINGS["service_tier"]},
            platform="cli",
            session_db=database,
            quiet_mode=True,
        )
        result = agent.run_conversation(os.environ["HERMES_INSTRUCTION"])
        (OUTPUT_DIRECTORY / "hermes-result.json").write_text(
            json.dumps(result, indent=2, default=str) + "\n"
        )
        print(result.get("final_response") or "", flush=True)
        if (
            result.get("failed")
            or result.get("partial")
            or result.get("completed") is False
        ):
            raise RuntimeError("Hermes did not complete the benchmark turn")
    finally:
        if agent is not None:
            agent.close()
        database.close()


if __name__ == "__main__":
    main()
