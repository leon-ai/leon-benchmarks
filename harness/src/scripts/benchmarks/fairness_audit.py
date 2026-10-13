"""Bind suite-transition reviews to the exact accepted native evidence."""

import collections
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from scripts.benchmarks import balanced, metrics, task_integrity
from scripts.benchmarks.regrading import verified_receipt

EVIDENCE_FILES = (
    "result.json",
    "agent/requests.jsonl",
    "agent/image-requests.jsonl",
    "agent/tool-schemas.jsonl",
    "agent/task-skills.json",
    "agent/openclaw-config.json",
    "agent/hermes-config.yaml",
    "agent/openclaw-media-completion.json",
    "agent/native-transcript.jsonl",
    "agent/openclaw.session.jsonl",
    "agent/trajectory.json",
    "agent/trajectory-events.jsonl",
    "verifier/wildclaw_score.json",
    "verifier/requests.jsonl",
    "verifier/judge-reliability.json",
    "verifier/artifact-regrade.json",
    "artifacts/manifest.json",
)


def inventory(output: Path, selection: dict, suite: str) -> dict:
    """Collect local evidence and review triggers without rerunning candidates or judges."""
    from scripts.benchmarks.campaign import report

    snapshot = report(output, selection)
    selected = {item["name"] for item in selection[suite]}
    matched = set(snapshot["summaries"][suite]["matched_tasks"])
    rows = sorted(
        (
            row
            for row in snapshot["trials"]
            if row["suite"] == suite
            and row["task"] in selected
            and row["score"] is not None
        ),
        key=lambda row: (row["task"], row["agent"], row["trial"]),
    )
    counts = collections.Counter((row["task"], row["agent"]) for row in rows)
    coverage = matched == selected and counts == collections.Counter(
        {(task, agent): 3 for task in selected for agent in balanced.benchmark.AGENTS}
    )
    evidence = []
    reviews = []
    for row in rows:
        trial = Path(row["trial"])
        files = {
            name: balanced.digest(trial / name)
            for name in EVIDENCE_FILES
            if (trial / name).is_file()
        }
        replay = verified_receipt(trial)
        if replay:
            for key in ("grade_file", "requests_file", "validation_file"):
                if replay.get(key):
                    files["regrade/" + replay[key]] = balanced.digest(
                        output / replay[key]
                    )
        evidence.append(
            {
                "task": row["task"],
                "agent": row["agent"],
                "trial": row["trial"],
                "score": row["score"],
                "files_sha256": files,
            }
        )
        review = task_integrity.inspect_trial(output, trial)
        result = json.loads((trial / "result.json").read_text())
        calls = metrics.lines(trial / "agent/requests.jsonl")
        failed_calls = [
            {
                "inference_id": call.get("inferenceId"),
                "status": call.get("status"),
                "completed": call.get("completed"),
                "error_type": call.get("errorType"),
            }
            for call in calls
            if not call.get("completed") or not 200 <= call.get("status", 0) < 300
        ]
        if review["review_triggered"] or failed_calls:
            reviews.append(
                {
                    "task": row["task"],
                    "agent": row["agent"],
                    "trial": row["trial"],
                    "score": row["score"],
                    "integrity": review,
                    "exception_type": (result.get("exception_info") or {}).get(
                        "exception_type"
                    ),
                    "unsuccessful_inference_calls": failed_calls,
                }
            )
    known = []
    for task, filename in (
        (
            "06_Safety_Alignment_task_9_misinformation",
            "misinformation-native-wording-review.json",
        ),
    ):
        path = output / filename
        if task in selected and path.exists():
            known.append(
                {
                    "task": task,
                    "evidence_file": filename,
                    "sha256": balanced.digest(path),
                    "reason": "Native wording checks require contextual review of quoted and negated claims.",
                }
            )
    identity = {
        "selection_sha256": balanced.digest(output / "selection.json"),
        "suite": suite,
        "evidence": evidence,
        "known_rubric_review": known,
        "task_contracts": {
            name: {
                "prepared_config": balanced.digest(
                    output / "tasks" / suite / name / "task.toml"
                ),
                "grader": balanced.digest(
                    output / "tasks" / suite / name / "tests/checks.py"
                ),
                "input_receipt": balanced.digest(
                    output / "inputs" / f"{suite}-{name}.json"
                ),
            }
            for name in selected
        },
    }
    fingerprint = hashlib.sha256(
        json.dumps(identity, sort_keys=True).encode()
    ).hexdigest()
    value = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "suite": suite,
        "complete_matched_coverage": coverage,
        "matched_tasks": len(matched),
        "selected_tasks": len(selected),
        "accepted_attempts": len(rows),
        "evidence_fingerprint": fingerprint,
        "evidence": evidence,
        "failure_reviews": reviews,
        "task_contracts": identity["task_contracts"],
        "known_rubric_review": known,
        "additional_model_calls": 0,
        "status": "awaiting contextual fairness review",
    }
    balanced.save(output / f"{suite}-fairness-audit.json", value)
    return value


def require_review(output: Path, selection: dict, suite: str) -> None:
    """Prevent a later suite from starting before its predecessor's review is current."""
    policy = json.loads((output / "campaign.json").read_text())
    if suite not in policy.get("required_suite_reviews", []):
        return
    audit = inventory(output, selection, suite)
    path = output / f"{suite}-fairness-review.json"
    review = json.loads(path.read_text()) if path.exists() else {}
    if (
        not audit["complete_matched_coverage"]
        or review.get("status") != "accepted"
        or review.get("evidence_fingerprint") != audit["evidence_fingerprint"]
        or set(review.get("reviewed_tasks", []))
        != {item["name"] for item in selection[suite]}
        or set(review.get("reviewed_failure_trials", []))
        != {row["trial"] for row in audit["failure_reviews"]}
        or set(review.get("reviewed_rubric_tasks", []))
        != {row["task"] for row in audit["known_rubric_review"]}
        or review.get("unresolved_validity_findings") != []
    ):
        raise RuntimeError(
            f"{suite} post-suite fairness audit requires review before advancing"
        )
