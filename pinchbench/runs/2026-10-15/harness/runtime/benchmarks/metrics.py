"""Derive comparable measurements from native traces without model requests."""

import collections
import json
import statistics
from datetime import datetime
from pathlib import Path

import jsonschema


def lines(path: Path) -> list[dict]:
    """Ignore an interrupted final line while retaining complete evidence."""
    rows = []
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def duration(stage: dict | None) -> float | None:
    """Use published lifecycle timestamps instead of including queue time."""
    if not stage or not stage.get("started_at") or not stage.get("finished_at"):
        return None
    return (
        datetime.fromisoformat(stage["finished_at"].replace("Z", "+00:00"))
        - datetime.fromisoformat(stage["started_at"].replace("Z", "+00:00"))
    ).total_seconds()


def distribution(values: list[float]) -> dict:
    """Retain sample size and range so short samples are not overstated."""
    return {
        "count": len(values),
        "median": statistics.median(values) if values else None,
        "minimum": min(values) if values else None,
        "maximum": max(values) if values else None,
    }


def inference_metrics(directory: Path, media: bool = False) -> dict:
    """Separate observed requests, token availability, and tool schema accuracy."""
    prefix = "image-" if media else ""
    requests = lines(directory / f"{prefix}requests.jsonl")
    starts = lines(directory / f"{prefix}inference-events.jsonl")
    catalogs = {
        row["schemaId"]: row["tools"] for row in lines(directory / "tool-schemas.jsonl")
    }
    calls = [] if media else lines(directory / "model-tool-calls.jsonl")
    checks = collections.Counter()
    for call in calls:
        tools = {
            tool.get("name"): tool for tool in catalogs.get(call.get("schemaId"), [])
        }
        tool = tools.get(call.get("name"))
        if not tool:
            checks["unknown_name"] += 1
            continue
        if call.get("arguments") is None or not tool.get("parameters"):
            checks["uncheckable"] += 1
            continue
        try:
            arguments = json.loads(call["arguments"])
            jsonschema.validate(arguments, tool["parameters"])
            checks["valid"] += 1
        except (json.JSONDecodeError, jsonschema.ValidationError):
            checks["invalid_arguments"] += 1
        except jsonschema.SchemaError:
            checks["uncheckable"] += 1
    checkable = checks["valid"] + checks["invalid_arguments"] + checks["unknown_name"]
    usages = [row["usage"] for row in requests if row.get("usage")]
    tokens = {
        key: sum(row.get(key, 0) for row in usages)
        for key in ("input_tokens", "output_tokens", "total_tokens")
    }
    tokens["cached_input_tokens"] = sum(
        (row.get("input_tokens_details") or {}).get("cached_tokens", 0)
        for row in usages
    )
    tokens["reasoning_output_tokens"] = sum(
        (row.get("output_tokens_details") or {}).get("reasoning_tokens", 0)
        for row in usages
    )
    return {
        "attempted_inference_requests": len({row["inferenceId"] for row in starts}),
        "recorded_inference_requests": len(requests),
        "completed_inference_requests": sum(
            bool(row.get("completed")) for row in requests
        ),
        "hosted_image_generation_requests": sum(
            row.get("requestKind") == "hosted_image_generation" for row in requests
        ),
        "reported_hosted_image_usage": [
            row["toolUsage"] for row in requests if row.get("toolUsage")
        ],
        "requests_with_token_usage": len(usages),
        "token_usage": tokens if usages else None,
        "token_usage_complete": bool(requests)
        and len(usages) == len(requests) == len(starts),
        "returned_service_tiers": dict(
            collections.Counter(
                row.get("returnedServiceTier", "unreported") for row in requests
            )
        ),
        "http_statuses": dict(
            collections.Counter(str(row.get("status", 0)) for row in requests)
        ),
        "latency_seconds": {
            field: distribution(
                [
                    row[field]
                    for row in requests
                    if isinstance(row.get(field), (int, float))
                ]
            )
            for field in (
                "headersSeconds",
                "firstTextSeconds",
                "firstToolActionSeconds",
                "responseSeconds",
            )
        },
        "observer_milliseconds": sum(
            row.get("observerMilliseconds", 0) for row in requests
        ),
        "model_tool_calls": len(calls),
        "tool_schema_checks": dict(checks),
        "tool_schema_validity": checks["valid"] / checkable if checkable else None,
        "tool_schema_check_coverage": checkable / len(calls) if calls else None,
        "semantic_tool_accuracy": None,
        "semantic_tool_accuracy_note": "Schema validity does not establish semantic correctness. Use task verifiers and native execution evidence; unverified calls remain unknown.",
    }


def trial_metrics(path: Path) -> dict:
    """Preserve grading criteria and native lifecycle stages alongside efficiency."""
    result = json.loads((path / "result.json").read_text())
    from scripts.benchmarks.regrading import verified_receipt

    regrade = verified_receipt(path)
    grade_path = next(
        (
            path / "verifier" / name
            for name in ("wildclaw_score.json", "pinch_score.json")
            if (path / "verifier" / name).exists()
        ),
        path / "verifier/wildclaw_score.json",
    )
    grade = json.loads(grade_path.read_text()) if grade_path.exists() else None
    value = {
        "agent": result["agent_info"]["name"],
        "candidate": inference_metrics(path / "agent"),
        "candidate_image_generation": inference_metrics(path / "agent", media=True),
        "judge": inference_metrics(path / "verifier"),
        "judge_recovery": inference_metrics(
            (path.parents[3] / regrade["requests_file"]).parent
        )
        if regrade and regrade.get("method_type") == "native_grader_replay"
        else None,
        "stage_seconds": {
            key: duration(result.get(key))
            for key in (
                "environment_setup",
                "agent_setup",
                "agent_execution",
                "verifier",
            )
        },
        "native_task_grade": (regrade or {}).get("rewards")
        or grade
        or (result.get("verifier_result") or {}).get("rewards"),
        "exception": result.get("exception_info"),
        "artifact_regrade": regrade,
        "interpretation": "Candidate and judge counts are separate. Tokens without provider usage are unknown. Native auxiliary and delegated requests passing the observed transport are included; external tool subprocess inference coverage must be audited separately.",
    }
    (path / "metrics.json").write_text(json.dumps(value, indent=2) + "\n")
    return value
