"""Validate artifact-only native grading while retaining original trial records."""

import hashlib
import json
from pathlib import Path


def digest(path: Path) -> str:
    """Identify the exact evidence consumed by a grading receipt."""
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


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

    if receipt.get("method_type") == "native_grader_replay":
        grade = json.loads((campaign / receipt["grade_file"]).read_text())
        requests = [
            json.loads(line)
            for line in (campaign / receipt["requests_file"]).read_text().splitlines()
        ]
        if grade != receipt["rewards"] or grade.get("error"):
            raise RuntimeError("Native grader replay did not produce a valid grade")
        reliability = json.loads(
            (campaign / receipt["grade_file"]).with_name("judge-reliability.json").read_text()
        )
        if reliability.get("failed_calls"):
            raise RuntimeError("Native grader replay has failed judge calls")
        if (not requests and not receipt.get("deterministic_no_inference")) or any(
            not row.get("completed")
            or row.get("errorType")
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
