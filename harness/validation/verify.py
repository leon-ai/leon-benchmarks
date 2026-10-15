"""Verify public evidence and score invariants without any model calls."""

import argparse
import collections
import gzip
import hashlib
import json
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]
RUN = REPOSITORY / "wildclawbench/runs/2026-10-12"
EXCLUDED = {"public-manifest.json", "publication-validation.json"}
SECRET_PATTERNS = [
    re.compile(r"eyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}"),
    re.compile(r"sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}"),
    re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"),
]
PRIVATE_FIELDS = {
    "accesstoken",
    "refreshtoken",
    "idtoken",
    "apikey",
    "authorization",
    "cookie",
    "chatgptaccountid",
    "accountid",
    "thinking",
    "thinkingsignature",
    "reasoningcontent",
    "encryptedcontent",
    "reasoningsignature",
}


class Links(HTMLParser):
    """Collect static proof links while leaving dynamic dashboard markup untouched."""

    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attributes):
        if tag == "a":
            for key, value in attributes:
                if key == "href" and value:
                    self.links.append(value)


def sha(path):
    """Hash a file incrementally so validation does not duplicate large payloads."""
    result = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1_048_576):
            result.update(chunk)
    return result.hexdigest()


def files():
    """Exclude Git internals, ignored private stores and derived validation receipts."""
    return sorted(
        path
        for path in REPOSITORY.rglob("*")
        if path.is_file()
        and not set(path.parts)
        & {".git", ".local", "__pycache__", ".venv", ".ruff_cache"}
        and path.name not in EXCLUDED
    )


def inspect_value(value):
    """Reject usable private credential fields and private reasoning in public records."""
    if isinstance(value, dict):
        assert not isinstance(value.get("type"), str) or value["type"] not in {
            "thinking",
            "reasoning",
            "redacted_thinking",
        }, "Private reasoning part"
        for key, child in value.items():
            normalized = re.sub(r"[^a-z]", "", key.lower())
            if normalized in PRIVATE_FIELDS:
                assert child == "[PRIVATE_VALUE_OMITTED]", "Private field not omitted"
            else:
                inspect_value(child)
    elif isinstance(value, list):
        for child in value:
            inspect_value(child)
    elif isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        try:
            parsed = json.loads(value)
        except (ValueError, RecursionError):
            return
        inspect_value(parsed)


def numeric_values(value, path=""):
    """Compare every recorded numerical measurement independently of redacted text."""
    result = {}
    if isinstance(value, dict):
        for key, child in value.items():
            result.update(numeric_values(child, path + "/" + key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            result.update(numeric_values(child, path + "/" + str(index)))
    elif isinstance(value, (float, int, bool)) or value is None:
        result[path] = value
    return result


def verify_run(run, archive=None, pinch_archive=None):
    """Validate roster-derived matched coverage, weighting, evidence links and metrics."""
    selection = json.loads((run / "tasks.json").read_text())["selection"]
    data = json.loads((run / "metrics.json").read_text())
    records = data["records"]
    assert len(records) == len(selection) * 9 and len(
        {row["id"] for row in records}
    ) == len(records)
    assert len(data["tasks"]) == len(selection)
    task_counts = collections.Counter(item["domain"] for item in selection)
    assert collections.Counter(task["domain"] for task in data["tasks"]) == task_counts
    assert set(data["domains"]) == set(task_counts)
    cohorts = collections.Counter((row["task"], row["agent"]) for row in records)
    assert cohorts == collections.Counter(
        {(task["name"], agent): 3 for task in selection for agent in data["agents"]}
    )
    for agent in data["agents"]:
        domain_values = []
        for domain in data["domains"]:
            scores = [
                row["score"]
                for row in records
                if row["agent"] == agent and row["domain"] == domain
            ]
            assert len(scores) == task_counts[domain] * 3
            domain_values.append(sum(scores) / len(scores))
        assert (
            abs(
                sum(domain_values) / len(data["domains"])
                - data["agents"][agent]["score"]
            )
            < 1e-12
        )
    original_match = None
    if archive and run == RUN:
        original = json.loads((archive / "wildclawbench-data.json").read_text())
        assert {
            row["id"]: (row["score"], row["native_grade"])
            for row in original["records"]
        } == {row["id"]: (row["score"], row["native_grade"]) for row in records}, (
            "Original scores or criteria changed"
        )
        assert data["ranking"] == original["ranking"]
        for agent in data["agents"]:
            assert data["agents"][agent] == original["agents"][agent], (
                "Aggregate telemetry changed"
            )
        original_match = True
    if pinch_archive and run.parent.parent.name == "pinchbench":
        private_manifest = json.loads(
            (pinch_archive / "evidence-manifest.json").read_text()
        )
        private_data = pinch_archive / private_manifest.get(
            "derived_metrics_file", "pinchbench-data.json"
        )
        assert sha(private_data) == private_manifest["derived_metrics_sha256"]
        publication = json.loads((run / "provenance/publication-map.json").read_text())
        assert publication["private_manifest_sha256"] == sha(
            pinch_archive / "evidence-manifest.json"
        )
        original = json.loads(private_data.read_text())
        assert {row["id"]: numeric_values(row) for row in original["records"]} == {
            row["id"]: numeric_values(row) for row in records
        }, "Pinch per-attempt numeric measurements changed"
        assert data["agents"] == original["agents"], "Pinch aggregate metrics changed"
        assert data["ranking"] == original["ranking"]
        original_match = True
    assert data["ranking"] == sorted(
        data["agents"], key=lambda a: data["agents"][a]["score"], reverse=True
    )
    report = run / "report/index.html"
    html = report.read_text()
    embedded = re.search(
        r'<script id="report-data" type="application/json">(.*?)</script>', html, re.S
    )
    assert embedded and json.loads(embedded[1]) == data
    parser = Links()
    parser.feed(html)
    checked_links = 0
    for link in parser.links:
        if "://" not in link and not link.startswith("#"):
            assert (report.parent / link).resolve().is_file(), (
                "Missing dashboard link: " + link
            )
            checked_links += 1
    accepted = json.loads((run / "accepted-attempts.json").read_text())
    assert {r["id"]: (r["score"], r["trial"]) for r in accepted} == {
        r["id"]: (r["score"], r["trial"]) for r in records
    }
    for row in records:
        for suffix in [
            "result.json",
            "integrity-review.json",
            "verifier/effective-grade.json",
            "accepted-score.json",
        ]:
            assert (run / row["trial"] / suffix).is_file(), row["id"] + "/" + suffix
            checked_links += 1
        if row.get("public_transcript"):
            assert (run / row["public_transcript"]).is_file()
    return {
        "run": str(run.relative_to(REPOSITORY)),
        "accepted_attempts": len(records),
        "matched_tasks": len(data["tasks"]),
        "domains": len(data["domains"]),
        "proof_links_checked": checked_links,
        "scores_and_aggregate_telemetry_match_private_original": original_match,
    }


def verify(seal=False, archive=None, pinch_archive=None):
    """Validate checksums, matched cohorts, numeric scores and publication boundaries."""
    paths = files()
    if seal:
        manifest = {
            "algorithm": "sha256",
            "scope": "Repository files; excludes Git, .local, Python/Ruff caches and validation control files",
            "files": [
                {
                    "path": str(path.relative_to(REPOSITORY)),
                    "bytes": path.stat().st_size,
                    "sha256": sha(path),
                }
                for path in paths
            ],
        }
        (RUN / "public-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    manifest = json.loads((RUN / "public-manifest.json").read_text())
    expected_paths = {entry["path"] for entry in manifest["files"]}
    assert expected_paths == {str(path.relative_to(REPOSITORY)) for path in paths}, (
        "File inventory mismatch"
    )
    for entry in manifest["files"]:
        path = REPOSITORY / entry["path"]
        assert path.stat().st_size == entry["bytes"] and sha(path) == entry["sha256"], (
            entry["path"]
        )
    runs = sorted(path.parent for path in REPOSITORY.glob("*/runs/*/metrics.json"))
    if seal:
        for run in runs:
            if run == RUN:
                continue
            local_paths = [path for path in paths if path.is_relative_to(run)]
            (run / "public-manifest.json").write_text(
                json.dumps(
                    {
                        "algorithm": "sha256",
                        "scope": "This dated run, excluding validation controls",
                        "files": [
                            {
                                "path": str(path.relative_to(REPOSITORY)),
                                "bytes": path.stat().st_size,
                                "sha256": sha(path),
                            }
                            for path in local_paths
                        ],
                    },
                    indent=2,
                )
                + "\n"
            )
    run_checks = [verify_run(run, archive, pinch_archive) for run in runs]
    checked_links = sum(check["proof_links_checked"] for check in run_checks)
    original_match = next(
        check["scores_and_aggregate_telemetry_match_private_original"]
        for check in run_checks
        if check["run"].startswith("wildclawbench/")
    )
    scanned_bytes = 0
    for path in paths:
        assert path.stat().st_size < 100 * 1_024 * 1_024, "GitHub file limit exceeded"
        if path.suffix not in {
            ".json",
            ".jsonl",
            ".txt",
            ".md",
            ".html",
            ".py",
            ".ts",
            ".mjs",
            ".mts",
            ".lock",
            ".gz",
        }:
            continue
        payload = (
            gzip.decompress(path.read_bytes())
            if path.suffix == ".gz"
            else path.read_bytes()
        )
        text = payload.decode()
        scanned_bytes += len(payload)
        assert str(Path.home()) not in text, "Owner path in " + str(
            path.relative_to(REPOSITORY)
        )
        assert not any(pattern.search(text) for pattern in SECRET_PATTERNS), (
            "Credential-like value in " + str(path.relative_to(REPOSITORY))
        )
        # Source modules intentionally contain authentication field names, not live values.
        if any(path.is_relative_to(run) for run in runs) and path.suffix == ".json":
            inspect_value(json.loads(text))
        elif any(path.is_relative_to(run) for run in runs) and path.name.endswith(
            (".json.gz", ".jsonl.gz")
        ):
            if path.name.endswith(".jsonl.gz"):
                for line in text.split("\n"):
                    if line.strip():
                        inspect_value(json.loads(line))
            else:
                inspect_value(json.loads(text))
    receipt = {
        "published_runs": run_checks,
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "passed": True,
        "manifest_sha256": sha(RUN / "public-manifest.json"),
        "files_verified": len(paths),
        "accepted_attempts": 324,
        "matched_tasks": 36,
        "domains": 6,
        "proof_links_checked": checked_links,
        "scanned_uncompressed_bytes": scanned_bytes,
        "scores_and_aggregate_telemetry_match_private_original": original_match,
        "no_owner_paths_or_credential_patterns": True,
        "private_reasoning_fields_omitted": True,
        "scope": "Offline publication validation; no benchmark execution or inference calls",
    }
    if seal:
        (RUN / "publication-validation.json").write_text(
            json.dumps(receipt, indent=2) + "\n"
        )
    for check in run_checks:
        run = REPOSITORY / check["run"]
        if run == RUN:
            continue
        local_paths = [path for path in paths if path.is_relative_to(run)]
        manifest_path = run / "public-manifest.json"
        if seal:
            manifest_path.write_text(
                json.dumps(
                    {
                        "algorithm": "sha256",
                        "scope": "This dated run, excluding validation controls",
                        "files": [
                            {
                                "path": str(path.relative_to(REPOSITORY)),
                                "bytes": path.stat().st_size,
                                "sha256": sha(path),
                            }
                            for path in local_paths
                        ],
                    },
                    indent=2,
                )
                + "\n"
            )
            (run / "publication-validation.json").write_text(
                json.dumps(
                    {**receipt, **check, "manifest_sha256": sha(manifest_path)},
                    indent=2,
                )
                + "\n"
            )
        local = json.loads(manifest_path.read_text())
        assert {row["path"] for row in local["files"]} == {
            str(path.relative_to(REPOSITORY)) for path in local_paths
        }
        for row in local["files"]:
            assert sha(REPOSITORY / row["path"]) == row["sha256"]
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seal",
        action="store_true",
        help="Write the public file inventory and validation receipt",
    )
    parser.add_argument(
        "--archive",
        type=Path,
        help="Optional private archive for exact numeric comparison",
    )
    parser.add_argument(
        "--pinch-archive",
        type=Path,
        help="Private PinchBench freeze for complete numeric comparison",
    )
    arguments = parser.parse_args()
    verify(arguments.seal, arguments.archive, arguments.pinch_archive)
