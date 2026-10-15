"""Validate artifact-only native grading while retaining original trial records."""

import hashlib
import json
from datetime import datetime
from pathlib import Path

NATIVE_DEADLINE_ERRORS = (None, "CancelledError", "TimeoutError", "AbortError")


def digest(path: Path) -> str:
    """Identify the exact evidence consumed by a grading receipt."""
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def jsonl_boundary_error(trial: Path) -> bool:
    """Prove intact native JSONL was split inside a valid JSON string."""
    for pattern in ("*/trajectory-events.jsonl", "*/openclaw.session.jsonl"):
        for path in (trial / "agent/turns").glob(pattern):
            text = path.read_text()
            try:
                records = [json.loads(line) for line in text.split("\n") if line.strip()]
            except ValueError:
                continue
            if not records:
                continue
            try:
                for line in text.splitlines():
                    if line.strip():
                        json.loads(line)
            except ValueError:
                return True
    return False


def native_content_schema_error(trial: Path) -> bool:
    """Identify text serialized outside the native grader's content-block schema."""
    path = trial / "agent/pinch-transcript.jsonl"
    if not path.exists():
        return False
    return any(
        message.get("role") in ("user", "toolResult")
        and isinstance(message.get("content"), str)
        and bool(message["content"])
        for line in path.read_text().split("\n")
        if line.strip()
        for message in (json.loads(line).get("message", {}),)
    )


def late_native_input(trial: Path, deadline: float) -> bool:
    """Require real late tool/text evidence before replaying an accepted native grade."""
    for path in (trial / "agent/turns").glob("*/native-transcript.jsonl"):
        for line in path.read_text().splitlines():
            row = json.loads(line)
            message = row.get("message", {})
            if row.get("type") != "message" or message.get("role") not in (
                "assistant",
                "toolResult",
            ):
                continue
            value = row.get("timestamp")
            if isinstance(value, str):
                value = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            if not isinstance(value, (int, float)):
                continue
            if value > 1_000_000_000_000:
                value /= 1_000
            if value <= deadline + 1:
                continue
            content = message.get("content", [])
            if isinstance(content, list) and any(
                part.get("type") in ("text", "toolCall") for part in content
            ):
                return True
    return False


def expected_output_failure(output: Path, trial: Path, grade: dict) -> bool:
    """Recognize audited native zero outcomes without masking grader failures."""
    policy_file = output / "diagnostic-policy.json"
    if not policy_file.exists():
        return False

    policy = json.loads(policy_file.read_text())
    task = trial.parent.parent.name.split("--", 1)[1]
    contract = policy.get("native_output_failure_contracts", {}).get(task)
    if not contract or grade != contract["rewards"]:
        return False

    checks = output / contract["checks_file"]
    if digest(checks) != contract["checks_sha256"]:
        raise RuntimeError("Native output failure contract changed")

    native_grade = trial / "verifier/wildclaw_score.json"
    result = json.loads((trial / "result.json").read_text())
    if (
        not native_grade.exists()
        or json.loads(native_grade.read_text()) != grade
        or (result.get("verifier_result") or {}).get("rewards")
        != contract.get("native_rewards", grade)
        or (trial / contract["exported_output"]).exists()
    ):
        return False

    manifest = trial / "artifacts/manifest.json"
    if not manifest.exists():
        return False

    return any(
        artifact.get("source") == contract["artifact_source"]
        and artifact.get("status")
        in contract.get("artifact_statuses", [contract["artifact_status"]])
        for artifact in json.loads(manifest.read_text())
    )


def judge_transport_failed(directory: Path) -> bool:
    """Retain a native retry's valid final grade while keeping every failed call visible."""
    path = directory / "judge-reliability.json"
    if not path.exists():
        return False
    reliability = json.loads(path.read_text())
    if not reliability.get("failed_calls"):
        return False
    requests_path = directory / "requests.jsonl"
    if not reliability.get("native_retry_recovered") or not requests_path.exists():
        return True
    requests = [
        json.loads(line)
        for line in requests_path.read_text().splitlines()
        if line.strip()
    ]
    last = requests[-1] if requests else {}
    return not (
        last.get("completed")
        and 200 <= last.get("status", 0) < 300
        and not last.get("errorType")
        and last.get("model") == "gpt-6.1-sol"
        and (last.get("reasoning") or {}).get("effort") == "high"
        and last.get("serviceTier") == "priority"
        and last.get("authMode") == "chatgpt_subscription"
        and last.get("endpoint", "").startswith(
            "https://chatgpt.com/backend-api/codex/"
        )
    )


def verified_receipt(trial: Path) -> dict | None:
    """Accept offline scores only when their saved evidence remains unchanged."""
    path = trial / "verifier/artifact-regrade.json"
    if not path.exists():
        return None

    receipt = json.loads(path.read_text())
    campaign = trial.parents[3]
    for relative, expected in receipt["evidence_sha256"].items():
        if digest(campaign / relative) != expected:
            raise RuntimeError(f"Regrading evidence changed: {relative}")

    if receipt.get("method_type") in (
        "native_grader_replay",
        "native_judge_retry_recovery",
    ):
        grade = json.loads((campaign / receipt["grade_file"]).read_text())
        requests = [
            json.loads(line)
            for line in (campaign / receipt["requests_file"]).read_text().splitlines()
        ]
        if grade != receipt["rewards"] or grade.get("error"):
            raise RuntimeError("Native grader replay did not produce a valid grade")
        if judge_transport_failed((campaign / receipt["grade_file"]).parent):
            raise RuntimeError("Native grader replay has failed judge calls")
        reliability_path = (
            campaign / receipt["grade_file"]
        ).parent / "judge-reliability.json"
        reliability = (
            json.loads(reliability_path.read_text())
            if reliability_path.exists()
            else {}
        )
        recovered_retry = reliability.get("native_retry_recovered", False)
        if (not requests and not receipt.get("deterministic_no_inference")) or any(
            (not row.get("completed") and not recovered_retry)
            or (row.get("errorType") and not recovered_retry)
            or row.get("model") != "gpt-6.1-sol"
            or (row.get("reasoning") or {}).get("effort") != "high"
            or row.get("serviceTier") != "priority"
            or row.get("authMode") != "chatgpt_subscription"
            or not row.get("endpoint", "").startswith(
                "https://chatgpt.com/backend-api/codex/"
            )
            for row in requests
        ):
            raise RuntimeError("Native grader replay lacks verified subscription calls")
        if receipt.get("candidate_execution_preserved"):
            result = json.loads((trial / "result.json").read_text())
            exception = result.get("exception_info") or {}
            candidate_path = next(
                (
                    path
                    for path in (
                        trial / "agent/leon-result.json",
                        trial / "agent/hermes-result.json",
                    )
                    if path.exists()
                ),
                None,
            )
            candidate = json.loads(candidate_path.read_text()) if candidate_path else {}
            candidate_output = (candidate.get("result") or {}).get(
                "output"
            ) or candidate.get("final_response")
            turns_path = trial / "agent/pinch-turns.json"
            turns = (
                json.loads(turns_path.read_text()).get("turns", [])
                if turns_path.exists()
                else []
            )
            pinch_completed = bool(turns) and all(row.get("completed") for row in turns)
            calls = [
                json.loads(line)
                for line in (trial / "agent/requests.jsonl").read_text().splitlines()
            ]
            if receipt.get("deadline_outcome_preserved"):
                selection = json.loads((campaign / "selection.json").read_text())
                task = trial.parent.parent.name.split("--", 1)[1]
                item = next(row for row in selection["pinch"] if row["name"] == task)
                started = datetime.fromisoformat(
                    result["agent_execution"]["started_at"]
                ).timestamp()
                original = json.loads((trial / "verifier/pinch_score.json").read_text())
                export_missing = (
                    original.get("error", "").startswith("FileNotFoundError:")
                    and "hermes-native-messages.json" in original["error"]
                )
                clipped_input = receipt.get(
                    "transport_repair"
                ) == "native-deadline-input-window" and late_native_input(
                    trial, receipt["deadline_epoch"]
                )
                boundary_error = (
                    receipt.get("transport_repair") == "native-jsonl-record-boundary"
                    and original.get("error", "").startswith("JSONDecodeError:")
                    and jsonl_boundary_error(trial)
                )
                schema_error = (
                    receipt.get("transport_repair") == "native-content-block-schema"
                    and native_content_schema_error(trial)
                )
                if (
                    exception.get("exception_type") != "AgentTimeoutError"
                    or not turns
                    or all(row.get("completed") for row in turns)
                    or not (
                        export_missing or clipped_input or boundary_error or schema_error
                    )
                    or receipt["deadline_epoch"] != started + item["timeout_seconds"]
                    or not calls
                    or any(
                        not row.get("completed")
                        and not (
                            (
                                200 <= row.get("status", 0) < 300
                                or row.get("status", 0) == 0
                                and row.get("errorType") in NATIVE_DEADLINE_ERRORS[1:]
                            )
                            and row.get("errorType") in NATIVE_DEADLINE_ERRORS
                            or clipped_input
                            and row.get("status") in (429, 500, 502, 503, 504)
                            and calls[-1].get("completed")
                            and 200 <= calls[-1].get("status", 0) < 300
                        )
                        for row in calls
                    )
                ):
                    raise RuntimeError(
                        "Timeout replay does not preserve the native deadline outcome"
                    )
                return receipt
            native_request_recovered = (
                pinch_completed
                and bool(calls)
                and calls[-1].get("completed")
                and 200 <= calls[-1].get("status", 0) < 300
                and not calls[-1].get("errorType")
                and all(
                    row.get("completed")
                    or (
                        200 <= row.get("status", 0) < 300
                        and row.get("errorType") in NATIVE_DEADLINE_ERRORS
                    )
                    or row.get("status") in (429, 500, 502, 503, 504)
                    for row in calls
                )
            )
            if (
                (
                    exception
                    and (
                        exception.get("exception_type") != "ValidationError"
                        or "VerifierResult"
                        not in exception.get("exception_message", "")
                    )
                )
                or not (result.get("agent_execution") or {}).get("finished_at")
                or not (candidate_output or pinch_completed)
                or candidate.get("failed")
                or candidate.get("partial")
                or not calls
                or (
                    any(not row.get("completed") for row in calls)
                    and not native_request_recovered
                )
            ):
                raise RuntimeError(
                    "Verifier recovery does not prove completed candidate execution"
                )
        return receipt

    validation = json.loads((campaign / receipt["validation_file"]).read_text())
    recovered = next(
        row for row in validation["recoverable_trials"] if row["trial"] == trial.name
    )
    if (
        validation["controls_count"] != len(validation["control_trials_exact_matches"])
        or validation["controls_count"] < 1
    ):
        raise RuntimeError("Offline grading lacks native-score validation")
    if receipt["rewards"] != recovered["scores"]:
        raise RuntimeError("Offline grading receipt differs from validated scores")
    return receipt
