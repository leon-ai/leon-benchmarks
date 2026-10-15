"""Configure and audit subscription inference without changing native prompts."""

import json
import os
import time
import uuid
import hashlib
from pathlib import Path

import httpx

OUTPUT_PATH = Path(
    os.environ.get("LEON_BENCHMARK_AUDIT_PATH", "/logs/agent/requests.jsonl")
)
OPENAI_URL = "https://api.openai.com/v1/"
SUBSCRIPTION_URL = "https://chatgpt.com/backend-api/codex/"
OPENROUTER_URL = "https://openrouter.ai/"
ORIGINAL_SEND = httpx.Client.send
ORIGINAL_ASYNC_SEND = httpx.AsyncClient.send
SETTINGS = json.loads(Path(__file__).with_name("pilot.json").read_text())
MAX_ARGUMENT_CHARACTERS = 262_144
MAX_IMAGE_RESPONSE_BYTES = 67_108_864
CATALOGS = set()


def image_request(url: str) -> bool:
    """Recognize only the subscription image endpoints approved by the owner."""
    return url in {
        SUBSCRIPTION_URL + endpoint
        for endpoint in SETTINGS.get("media_generation", {}).get(
            "subscription_endpoints", []
        )
    }


class Observation:
    """Capture bounded metadata without copying prompts or changing stream bytes."""

    def __init__(self, request):
        self.started = time.monotonic()
        self.pending = b""
        self.saved = False
        self.names = {}
        self.media = image_request(str(request.url))
        self.output_path = (
            OUTPUT_PATH.with_name("image-requests.jsonl") if self.media else OUTPUT_PATH
        )
        payload = json.loads(request.content)
        schemas = [
            {
                "name": tool.get("name") or tool.get("function", {}).get("name"),
                "type": tool.get("type"),
                "parameters": tool.get("parameters")
                or tool.get("function", {}).get("parameters"),
            }
            for tool in payload.get("tools", [])
        ]
        schema_id = hashlib.sha256(
            json.dumps(schemas, sort_keys=True).encode()
        ).hexdigest()
        if schema_id not in CATALOGS:
            self.append("tool-schemas.jsonl", {"schemaId": schema_id, "tools": schemas})
            CATALOGS.add(schema_id)
        self.value = {
            "inferenceId": uuid.uuid4().hex,
            "model": payload.get("model"),
            "reasoning": payload.get("reasoning"),
            "serviceTier": payload.get("service_tier"),
            "status": 0,
            "authMode": "chatgpt_subscription",
            "endpoint": str(request.url),
            "schemaId": schema_id,
            "completed": False,
            "observerMilliseconds": 0,
        }
        self.append(
            "image-inference-events.jsonl" if self.media else "inference-events.jsonl",
            {"inferenceId": self.value["inferenceId"], "event": "started"},
        )

    def append(self, name, value):
        """Write only metadata and bounded tool arguments alongside native evidence."""
        with (OUTPUT_PATH.parent / name).open("a") as output:
            output.write(json.dumps(value) + "\n")

    def feed(self, data):
        """Parse SSE metadata while preserving the exact bytes consumed by the SDK."""
        started = time.monotonic()
        if self.media:
            if not self.value.get("oversizedEvents"):
                self.pending += data
                if len(self.pending) > MAX_IMAGE_RESPONSE_BYTES:
                    self.pending = b""
                    self.value["oversizedEvents"] = 1
            self.value["observerMilliseconds"] += (time.monotonic() - started) * 1_000
            return
        self.pending += data
        while b"\n" in self.pending:
            line, self.pending = self.pending.split(b"\n", 1)
            if not line.startswith(b"data: ") or line.strip() == b"data: [DONE]":
                continue
            try:
                event = json.loads(line[6:])
            except (ValueError, UnicodeDecodeError):
                self.value["unparsedEvents"] = self.value.get("unparsedEvents", 0) + 1
                continue
            seconds = time.monotonic() - self.started
            kind = event.get("type")
            if kind == "response.output_text.delta":
                self.value.setdefault("firstTextSeconds", seconds)
            if (
                kind == "response.output_item.added"
                and event.get("item", {}).get("type") == "function_call"
            ):
                self.names[event["item"]["id"]] = event["item"]
            if kind == "response.function_call_arguments.done":
                item = self.names.get(event.get("item_id"), {})
                arguments = event.get("arguments", "")
                self.value.setdefault("firstToolActionSeconds", seconds)
                self.append(
                    "model-tool-calls.jsonl",
                    {
                        "inferenceId": self.value["inferenceId"],
                        "schemaId": self.value["schemaId"],
                        "callId": item.get("call_id"),
                        "name": event.get("name") or item.get("name"),
                        "arguments": arguments
                        if len(arguments) <= MAX_ARGUMENT_CHARACTERS
                        else None,
                        "argumentsOmitted": len(arguments) > MAX_ARGUMENT_CHARACTERS,
                        "readySeconds": seconds,
                    },
                )
            if kind in ("response.completed", "response.failed", "response.incomplete"):
                result = event.get("response", {})
                self.value.update(
                    completed=kind == "response.completed",
                    terminalEvent=kind,
                    returnedModel=result.get("model"),
                    returnedServiceTier=result.get("service_tier"),
                    usage=result.get("usage"),
                    responseSeconds=seconds,
                )
        if len(self.pending) > 4_194_304:
            self.pending = b""
            self.value["oversizedEvents"] = self.value.get("oversizedEvents", 0) + 1
        self.value["observerMilliseconds"] += (time.monotonic() - started) * 1_000

    def finish(self):
        """Retain unsuccessful and canceled requests without inventing token counts."""
        if not self.saved:
            self.saved = True
            self.value["elapsedSeconds"] = time.monotonic() - self.started
            if self.media and not self.value.get("oversizedEvents"):
                try:
                    response = json.loads(self.pending)
                    if not isinstance(response, dict):
                        raise ValueError("Image response is not a JSON object")
                    images = response.get("data")
                    self.value.update(
                        completed=200 <= self.value["status"] < 300
                        and isinstance(images, list)
                        and bool(images)
                        and not response.get("error"),
                        imageCount=len(images) if isinstance(images, list) else 0,
                        usage=response.get("usage"),
                        returnedModel=response.get("model"),
                        reportedQuality=response.get("quality"),
                        reportedSize=response.get("size"),
                        responseSeconds=self.value["elapsedSeconds"],
                    )
                except (ValueError, UnicodeDecodeError):
                    self.value["unparsedEvents"] = (
                        self.value.get("unparsedEvents", 0) + 1
                    )
            with self.output_path.open("a") as output:
                output.write(json.dumps(self.value) + "\n")


class ObservedSyncStream(httpx.SyncByteStream):
    """Forward native synchronous response chunks and close the original stream."""

    def __init__(self, stream, observation):
        self.stream = stream
        self.observation = observation

    def __iter__(self):
        try:
            for data in self.stream:
                self.observation.feed(data)
                yield data
        finally:
            self.observation.finish()

    def close(self):
        self.observation.finish()
        self.stream.close()


class ObservedAsyncStream(httpx.AsyncByteStream):
    """Forward native asynchronous response chunks and close the original stream."""

    def __init__(self, stream, observation):
        self.stream = stream
        self.observation = observation

    async def __aiter__(self):
        try:
            async for data in self.stream:
                self.observation.feed(data)
                yield data
        finally:
            self.observation.finish()

    async def aclose(self):
        self.observation.finish()
        await self.stream.aclose()


def configure_request(request: httpx.Request) -> httpx.Request:
    """Keep auxiliary inference at the same declared settings as main calls."""
    url = str(request.url)
    if url.startswith((OPENAI_URL, OPENROUTER_URL)):
        raise RuntimeError(
            "Benchmark inference requires the ChatGPT subscription route"
        )
    if not url.startswith(SUBSCRIPTION_URL) or not request.content:
        return request
    payload = json.loads(request.content)
    if image_request(url):
        if payload.get("model") not in SETTINGS["media_generation"]["image_models"]:
            raise RuntimeError(
                "Benchmark image generation requires the approved image model"
            )
        # Native image endpoints do not accept reasoning or service-tier fields.
        # Keep the plugin's generation/editing payload unchanged.
        return request
    if payload.get("model") != SETTINGS["model"].split("/", 1)[1]:
        raise RuntimeError("Benchmark inference cannot substitute another model")

    payload["reasoning"] = {
        **(payload.get("reasoning") or {}),
        "effort": SETTINGS["reasoning"],
    }
    payload["service_tier"] = SETTINGS["service_tier"]
    headers = request.headers.copy()
    headers.pop("content-length", None)

    return httpx.Request(
        request.method,
        request.url,
        headers=headers,
        content=json.dumps(payload).encode(),
        extensions=request.extensions,
    )


def send(self: httpx.Client, request: httpx.Request, **kwargs) -> httpx.Response:
    """Observe the native synchronous SDK transport."""
    request = configure_request(request)
    observed = (
        Observation(request)
        if str(request.url).startswith(SUBSCRIPTION_URL) and request.content
        else None
    )
    try:
        response = ORIGINAL_SEND(self, request, **kwargs)
    except Exception as error:
        if observed:
            observed.value["errorType"] = type(error).__name__
            observed.finish()
        raise
    if observed:
        observed.value["status"] = response.status_code
        observed.value["headersSeconds"] = time.monotonic() - observed.started
        if response.is_stream_consumed:
            observed.feed(response.content)
            observed.finish()
        else:
            response.stream = ObservedSyncStream(response.stream, observed)
    return response


async def async_send(
    self: httpx.AsyncClient, request: httpx.Request, **kwargs
) -> httpx.Response:
    """Observe the native asynchronous SDK transport."""
    request = configure_request(request)
    observed = (
        Observation(request)
        if str(request.url).startswith(SUBSCRIPTION_URL) and request.content
        else None
    )
    try:
        response = await ORIGINAL_ASYNC_SEND(self, request, **kwargs)
    except Exception as error:
        if observed:
            observed.value["errorType"] = type(error).__name__
            observed.finish()
        raise
    if observed:
        observed.value["status"] = response.status_code
        observed.value["headersSeconds"] = time.monotonic() - observed.started
        if response.is_stream_consumed:
            observed.feed(response.content)
            observed.finish()
        else:
            response.stream = ObservedAsyncStream(response.stream, observed)
    return response


httpx.Client.send = send
httpx.AsyncClient.send = async_send
