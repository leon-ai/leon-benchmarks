"""Validate native evidence and replace only explicitly approved task cohorts."""

import collections
import json
from datetime import datetime, timezone
from pathlib import Path

from scripts.benchmarks import balanced
from scripts.benchmarks.regrading import expected_output_failure, verified_receipt


def inspect_trial(output: Path, trial: Path) -> dict:
    """Diagnose unsuccessful outputs without treating low scores as setup faults."""
    result = json.loads((trial / "result.json").read_text())
    grade_path = trial / "verifier/wildclaw_score.json"
    grade = json.loads(grade_path.read_text()) if grade_path.exists() else {}
    rewards = (result.get("verifier_result") or {}).get("rewards") or {}
    regrade = verified_receipt(trial)
    if regrade:
        grade = regrade["rewards"]
        rewards = grade
    score = rewards.get("overall_score", rewards.get("reward"))
    requests_path = trial / "agent/requests.jsonl"
    requests = (
        [json.loads(line) for line in requests_path.read_text().splitlines()]
        if requests_path.exists()
        else []
    )
    settings_verified = bool(requests) and any(
        200 <= row.get("status", 0) < 300 for row in requests
    ) and all(
        row.get("model") == "gpt-6.1-sol"
        and (row.get("reasoning") or {}).get("effort") == "high"
        and row.get("serviceTier") == "priority"
        and row.get("authMode") == "chatgpt_subscription"
        and row.get("endpoint", "").startswith(
            "https://chatgpt.com/backend-api/codex/"
        )
        for row in requests
    )
    output_failure = expected_output_failure(output, trial, grade)
    errors = [
        key
        for key, value in grade.items()
        if (key in balanced.benchmark.GRADER_ERROR_KEYS and value and not output_failure)
        or (
            key.endswith("judge_method")
            and value in balanced.benchmark.FALLBACK_JUDGE_METHODS
        )
    ]
    reliability = trial / "verifier/judge-reliability.json"
    if regrade and regrade.get("grade_file"):
        reliability = (output / regrade["grade_file"]).with_name(
            "judge-reliability.json"
        )
    if reliability.exists() and json.loads(reliability.read_text()).get("failed_calls"):
        errors.append("judge_transport_failures")
    policy = json.loads((output / "diagnostic-policy.json").read_text())
    exception = result.get("exception_info")
    triggered = bool(
        exception
        or score is None
        or score <= policy["low_score_threshold"]
        or errors
        or not settings_verified
    )
    markers = []
    if triggered:
        for path in (trial / "agent").glob("*.txt"):
            text = path.read_text(errors="replace")
            markers.extend(
                marker for marker in policy["harness_error_markers"] if marker in text
            )
        transcript = trial / "agent/native-transcript.jsonl"
        if transcript.exists():
            text = transcript.read_text()
            markers.extend(
                marker for marker in policy["harness_error_markers"] if marker in text
            )
    unresolved = bool(errors or markers or not settings_verified or score is None)
    if exception and exception["exception_type"] not in (
        *balanced.benchmark.SCORED_AGENT_EXCEPTIONS,
        balanced.benchmark.RETURNED_AGENT_EXCEPTION,
    ):
        unresolved = True
    value = {
        "trial": str(trial),
        "score": score,
        "review_triggered": triggered,
        "settings_verified": settings_verified,
        "grader_errors": errors,
        "native_error_markers": sorted(set(markers)),
        "requires_review": unresolved,
        "classification": "unresolved_validity"
        if unresolved
        else "native_outcome_retained",
        "result_sha256": balanced.digest(trial / "result.json"),
        "additional_inference_calls": 0,
        "expected_output_failure": output_failure,
    }
    balanced.save(trial / "integrity-review.json", value)
    return value


def prepare_repair(output: Path, suite: str, task: str, agents=None) -> None:
    """Keep original attempts quarantined until replacement evidence is complete."""
    path = output / "environment-repair-audit.json"
    audit = json.loads(path.read_text()) if path.exists() else {"affected_trials": []}
    existing = {row["trial"] for row in audit["affected_trials"]}
    directory = output / "jobs" / f"{suite}--{task}"
    originals = list((directory / "native").glob("*/result.json"))
    counts = collections.Counter(
        json.loads(p.read_text())["agent_info"]["name"] for p in originals
    )
    if counts != collections.Counter({name: 3 for name in balanced.benchmark.AGENTS}):
        raise RuntimeError("Repair requires a complete original task cohort")
    for result in originals:
        agent = json.loads(result.read_text())["agent_info"]["name"]
        if agents is not None and agent not in agents:
            continue
        trial = str(result.parent)
        if trial not in existing:
            audit["affected_trials"].append(
                {
                    "suite": suite,
                    "task": task,
                    "agent": agent,
                    "trial": trial,
                    "trial_name": result.parent.name,
                    "reason": "Approved environment repair replaces this task cohort.",
                    "original_result_sha256": balanced.digest(result),
                    "replacement_directory": str(directory / "native-repair"),
                }
            )
    balanced.save(path, audit)


def accept_repair(output: Path, suite: str, task: str, results: list[dict]) -> None:
    """Assign replacements by agent and identifier, independently of their scores."""
    path = output / "environment-repair-audit.json"
    audit = json.loads(path.read_text())
    originals = [
        row for row in audit["affected_trials"]
        if row["suite"] == suite and row["task"] == task
    ]
    directory = output / "jobs" / f"{suite}--{task}" / "native-repair"
    counts = collections.Counter(row["agent_info"]["name"] for row in results)
    expected = collections.Counter(row["agent"] for row in originals)
    if not expected or counts != expected:
        raise RuntimeError("Replacement coverage must match the audited agent slots")
    for agent in expected:
        old = sorted(
            (row for row in originals if row["agent"] == agent),
            key=lambda row: row["trial_name"],
        )
        new = sorted(
            (row for row in results if row["agent_info"]["name"] == agent),
            key=lambda row: row["trial_name"],
        )
        for original, replacement in zip(old, new, strict=True):
            trial = directory / replacement["trial_name"]
            if inspect_trial(output, trial)["requires_review"]:
                raise RuntimeError("Replacement needs validity review before acceptance")
            original.update(
                replacement_trial=str(trial),
                replacement_result_sha256=balanced.digest(trial / "result.json"),
            )
    audit["updated_at"] = datetime.now(timezone.utc).isoformat()
    balanced.save(path, audit)
    issues_path = output / "grader-issues.json"
    issues = json.loads(issues_path.read_text())
    for issue in issues:
        if issue["suite"] == suite and issue["task"] == task:
            issue["exclude_from_ranking"] = False
            issue["resolution"] = "Complete replacement cohort passed native evidence checks."
    balanced.save(issues_path, issues)
