"""Run unchanged grading criteria through OpenAI's subscription Responses route."""

import json
import os
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import openai

from judge_images import image_module, prepare_image

SETTINGS = json.loads(Path(__file__).with_name("pilot.json").read_text())
SUBSCRIPTION_PATH = Path("/run/leon-benchmark-subscription.json")
ORIGINAL_CLIENT = openai.OpenAI
CLIENTS = []
FAILED_CALLS = []


class SubscriptionJudgeClient:
    """Adapt the graders' text/image chat messages without changing their rubrics."""

    def __init__(self, **kwargs):
        credentials = json.loads(SUBSCRIPTION_PATH.read_text())["hermes"]
        self.client = ORIGINAL_CLIENT(
            api_key=credentials["access_token"],
            base_url=credentials["base_url"],
            default_headers={
                "chatgpt-account-id": credentials["chatgpt_account_id"],
                "originator": "hermes",
            },
        )
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.complete))
        CLIENTS.append(self.client)

    def complete(self, *, messages, **kwargs):
        """Translate supported message parts and return the existing grader shape."""
        try:
            return self._complete(messages=messages, **kwargs)
        except Exception as error:
            FAILED_CALLS.append(type(error).__name__)
            raise

    def _complete(self, *, messages, **kwargs):
        """Keep native rubric retries observable even when a grader returns zero."""
        items = []
        for message in messages:
            role = "developer" if message["role"] == "system" else message["role"]
            content = message["content"]
            if isinstance(content, list):
                parts = []
                for part in content:
                    if part["type"] == "text":
                        parts.append({"type": "input_text", "text": part["text"]})
                    elif part["type"] == "image_url":
                        parts.append(prepare_image(part["image_url"]))
                    else:
                        raise ValueError("Unsupported judge message content")
                content = parts
            items.append({"role": role, "content": content})
        response = None
        text = []
        with self.client.responses.create(
            model=SETTINGS["model"].split("/", 1)[1],
            input=items,
            instructions="",
            reasoning={"effort": SETTINGS["reasoning"]},
            service_tier=SETTINGS["service_tier"],
            include=["reasoning.encrypted_content"],
            store=False,
            stream=True,
        ) as stream:
            for event in stream:
                if event.type == "response.output_text.delta":
                    text.append(event.delta)
                elif event.type == "response.completed":
                    response = event.response
        if response is None:
            raise RuntimeError("Subscription judge did not complete its response")
        # Text events retain the generated answer when the terminal response
        # omits message content. Use only completed streams for grading.
        streamed_text = "".join(text)
        output_text = response.output_text or streamed_text
        with Path("/logs/verifier/judge-responses.jsonl").open("a") as audit:
            audit.write(
                json.dumps(
                    {
                        "completed_text_chars": len(response.output_text),
                        "streamed_text_chars": len(streamed_text),
                        "reconstruction_used": not response.output_text
                        and bool(streamed_text),
                        "returned_model": response.model,
                        "returned_service_tier": response.service_tier,
                    }
                )
                + "\n"
            )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=output_text))]
        )


def main() -> None:
    """Provide subscription-only credentials to legacy grader variable names."""
    from native_transcript import collect

    image_module()
    if os.environ.get("LEON_BENCHMARK_GRADER_HISTORY") != "output_only":
        collect()
    credentials = json.loads(SUBSCRIPTION_PATH.read_text())["hermes"]
    os.environ["CODEX_SUBSCRIPTION_API_KEY"] = credentials["access_token"]
    os.environ["CODEX_SUBSCRIPTION_BASE_URL"] = credentials["base_url"]
    os.environ["JUDGE_MODEL"] = SETTINGS["model"].split("/", 1)[1]
    os.environ["LEON_BENCHMARK_AUDIT_PATH"] = "/logs/verifier/requests.jsonl"
    runpy.run_path(str(Path(__file__).with_name("audit_openai.py")))
    openai.OpenAI = SubscriptionJudgeClient
    # Match the upstream entry point's import path for sibling transcript helpers.
    sys.path.insert(0, "/tests")
    try:
        runpy.run_path("/tests/grader.py", run_name="__main__")
    finally:
        Path("/logs/verifier/judge-reliability.json").write_text(
            json.dumps({"failed_calls": FAILED_CALLS}) + "\n"
        )
        for client in CLIENTS:
            client.close()


if __name__ == "__main__":
    main()
