"""Export immutable benchmark evidence without exposing local credentials or reasoning."""

import argparse
import collections
import gzip
import hashlib
import json
import re
import shutil
from pathlib import Path

PRIVATE_FIELDS = {
    "accesstoken",
    "refreshtoken",
    "idtoken",
    "apikey",
    "authorization",
    "cookie",
    "chatgptaccountid",
    "accountid",
    "email",
    "thinking",
    "thinkingsignature",
    "reasoningcontent",
    "encryptedcontent",
    "reasoningsignature",
}
PRIVATE_PARTS = {"thinking", "reasoning", "redacted_thinking"}
SECRET_PATTERNS = [
    re.compile(r"eyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}"),
    re.compile(r"sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}"),
    re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"),
]
INLINE_IMAGE = re.compile(r"data:image/[^;,]+;base64,[A-Za-z0-9+/=\r\n]+")
RECORD_FILES = [
    "result.json",
    "config.json",
    "metrics.json",
    "integrity-review.json",
    "agent/requests.jsonl",
    "agent/inference-events.jsonl",
    "agent/image-requests.jsonl",
    "agent/model-tool-calls.jsonl",
    "agent/tool-schemas.jsonl",
    "agent/task-skills.json",
    "agent/openclaw-config.json",
    "agent/hermes-config.json",
    "agent/transcript-export.json",
    "agent/service-readiness.json",
    "agent/environment-healthcheck.jsonl",
    "agent/media-completion.json",
    "verifier/wildclaw_score.json",
    "verifier/requests.jsonl",
    "verifier/inference-events.jsonl",
    "verifier/judge-reliability.json",
    "verifier/artifact-regrade.json",
    "artifacts/manifest.json",
]


def digest(payload):
    """Identify both private originals and transformed exports by their bytes."""
    return hashlib.sha256(payload).hexdigest()


def save_json(path, data):
    """Write a consistent, inspectable representation of public records."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


class Sanitizer:
    """Keep observable actions while removing authentication and private reasoning."""

    def __init__(self, archive):
        self.counts = collections.Counter()
        campaign = archive.parent
        workspace = campaign.parent
        self.paths = [
            (str(archive), "__PRIVATE_ARCHIVE__"),
            (str(campaign), "__WORKSPACE__"),
            (str(workspace / "environments"), "__SOURCES__"),
            (str(workspace), "__PRIVATE_WORKSPACE__"),
            (str(workspace.parent.parent.parent), "__HOME__"),
        ]

    def text(self, value):
        """Redact encoded secrets and inline images, including embedded JSON outputs."""
        for pattern in SECRET_PATTERNS:
            value, count = pattern.subn("[REDACTED_CREDENTIAL]", value)
            self.counts["credential_patterns"] += count
        value, count = INLINE_IMAGE.subn(
            lambda match: "[INLINE_IMAGE sha256=" + digest(match[0].encode()) + "]",
            value,
        )
        self.counts["inline_images"] += count
        for source, replacement in self.paths:
            value, count = value.replace(source, replacement), value.count(source)
            self.counts["private_paths"] += count
        if value.lstrip().startswith(("{", "[")):
            try:
                parsed = json.loads(value)
            except (ValueError, RecursionError):
                return value
            value = json.dumps(self.transform(parsed), ensure_ascii=False)
        return value

    def transform(self, value):
        """Recursively sanitize structured native transcripts and evidence records."""
        if isinstance(value, dict):
            if isinstance(value.get("type"), str) and value["type"] in PRIVATE_PARTS:
                self.counts["private_reasoning_parts"] += 1
                return {"type": "private_reasoning_omitted"}
            output = {}
            for key, child in value.items():
                normalized = re.sub(r"[^a-z0-9]", "", key.lower())
                public_key = self.text(key)
                if normalized in PRIVATE_FIELDS:
                    self.counts["private_fields"] += 1
                    output[public_key] = "[PRIVATE_VALUE_OMITTED]"
                elif (
                    (key in ("data", "b64_json") or normalized.endswith("base64"))
                    and isinstance(child, str)
                    and len(child) > 4_096
                ):
                    self.counts["large_inline_payloads"] += 1
                    output[public_key] = {
                        "omitted": True,
                        "bytes": len(child.encode()),
                        "sha256": digest(child.encode()),
                    }
                else:
                    output[public_key] = self.transform(child)
            return output
        if isinstance(value, list):
            return [self.transform(child) for child in value]
        if isinstance(value, str):
            return self.text(value)
        return value


def export_file(source, destination, sanitizer, mapping, compress=False):
    """Save a sanitized record with a reversible provenance reference, not its secrets."""
    before = sanitizer.counts.copy()
    original = source.read_bytes()
    if source.suffix == ".jsonl":
        lines = []
        for line_number, line in enumerate(original.decode().split("\n"), 1):
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                if source.name != "openclaw.session.jsonl":
                    raise
                # Preserve a capture defect without inventing or exposing partial reasoning.
                sanitizer.counts["unparseable_source_lines"] += 1
                parsed = {
                    "type": "unparseable_source_line_omitted",
                    "line_number": line_number,
                    "bytes": len(line.encode()),
                    "sha256": digest(line.encode()),
                }
            lines.append(json.dumps(sanitizer.transform(parsed), ensure_ascii=False))
        payload = ("\n".join(lines) + "\n").encode()
    elif source.suffix == ".json":
        payload = (
            json.dumps(
                sanitizer.transform(json.loads(original)), ensure_ascii=False, indent=2
            )
            + "\n"
        ).encode()
    else:
        payload = sanitizer.text(original.decode()).encode()
    if compress:
        destination = destination.with_suffix(destination.suffix + ".gz")
        payload = gzip.compress(payload, mtime=0)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)
    mapping.append(
        {
            "source": sanitizer.text(str(source)),
            "destination": str(destination),
            "original_sha256": digest(original),
            "export_sha256": digest(payload),
            "transformations": dict(sanitizer.counts - before),
        }
    )
    return destination


def export(archive, repo):
    """Assemble a dated public result without modifying the owner's frozen records."""
    campaign = archive.parent
    run = repo / "wildclawbench/runs/2026-10-12"
    run.mkdir(parents=True, exist_ok=True)
    sanitizer = Sanitizer(archive)
    mapping = []
    data = sanitizer.transform(
        json.loads((archive / "wildclawbench-data.json").read_text())
    )
    plan = data["metrics_plan"]
    plan["agreement_provenance"] = [
        {"timestamp": row["timestamp"], "role": row["role"]}
        for row in plan.pop("original_messages")
    ]
    save_json(run / "metrics-plan.json", plan)
    selection = json.loads((archive / "snapshots/selection.json").read_text())
    save_json(
        run / "tasks.json",
        {
            "revision": selection["dataset_revisions"]["wild"],
            "selection": sanitizer.transform(selection["wild"]),
            "scored_tasks": [
                {"id": task["id"], "domain": task["domain"]} for task in data["tasks"]
            ],
        },
    )
    save_json(run / "conditions.json", data["conditions"])
    for record in data["records"]:
        private_trial = campaign / record["trial"]
        public_trial = (
            Path("attempts") / record["agent"] / record["task"] / record["id"]
        )
        record["source_trial"] = record["trial"]
        record["trial"] = str(public_trial)
        target = run / public_trial
        for name in RECORD_FILES:
            source = private_trial / name
            if source.exists():
                export_file(
                    source,
                    target / name,
                    sanitizer,
                    mapping,
                    compress=source.suffix == ".jsonl",
                )
        # Publish one complete available action trace; duplicate captures stay in the raw archive.
        for name in [
            "agent/native-transcript.jsonl",
            "agent/trajectory.json",
            "agent/trajectory-events.jsonl",
            "agent/openclaw.session.jsonl",
        ]:
            source = private_trial / name
            if source.exists():
                exported = export_file(
                    source, target / name, sanitizer, mapping, compress=True
                )
                record["public_transcript"] = str(exported.relative_to(run))
                if source.name == "openclaw.session.jsonl":
                    record["public_transcript_note"] = (
                        "Partial original native-session fallback: malformed source lines "
                        "omitted with hashes; historical normalized metrics unchanged."
                    )
                break
        if not (target / "integrity-review.json").exists():
            save_json(
                target / "integrity-review.json",
                {
                    "availability": "No per-trial integrity file captured",
                    "diagnosis": record["diagnosis"],
                },
            )
        save_json(
            target / "verifier/effective-grade.json",
            {
                "accepted_score": record["score"],
                "native_criteria": record["native_grade"],
                "note": "Accepted grade after recorded repairs/regrades; not an invented original grader output.",
            },
        )
        save_json(
            target / "accepted-score.json",
            {
                "id": record["id"],
                "agent": record["agent"],
                "task": record["task"],
                "score": record["score"],
                "source_result_sha256": digest(
                    (private_trial / "result.json").read_bytes()
                ),
                "diagnosis": record["diagnosis"],
            },
        )
    save_json(run / "metrics.json", data)
    save_json(
        run / "accepted-attempts.json",
        [
            {
                key: row[key]
                for key in ["id", "agent", "task", "domain", "score", "trial"]
            }
            for row in data["records"]
        ],
    )
    for name in [
        "freeze-verification.json",
        "frozen-agent-sources.json",
        "report-validation.json",
    ]:
        export_file(archive / name, run / "provenance" / name, sanitizer, mapping)
    for source in sorted((archive / "snapshots").glob("*.json")):
        export_file(
            source,
            run / "provenance" / "campaign-snapshots" / source.name,
            sanitizer,
            mapping,
        )
    # The original hash inventory is large; compression preserves every entry without Git bloat.
    export_file(
        archive / "evidence-manifest.json",
        run / "provenance/private-evidence-inventory.json",
        sanitizer,
        mapping,
        compress=True,
    )
    export_file(
        archive / "REPRODUCTION_PROMPT.txt",
        run / "provenance/original-reproduction.txt",
        sanitizer,
        mapping,
    )
    source_manifest = []
    for source in sorted((archive / "snapshots/harness").iterdir()):
        if not source.is_file():
            continue
        destination = repo / "harness/src/scripts/benchmarks" / source.name
        payload = source.read_bytes()
        if source.name == "native_transcript.py":
            payload = re.sub(
                rb"/home/[^/]+/\.hermes/tools/[^/]+/bin/python3",
                b"/opt/hermes-python/bin/python3",
                payload,
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        source_manifest.append(
            {
                "path": str(destination.relative_to(repo)),
                "original_sha256": digest(source.read_bytes()),
                "packaged_sha256": digest(payload),
                "packaging_change": "Portable Hermes fallback path"
                if payload != source.read_bytes()
                else None,
            }
        )
    for name in ["__init__.py"]:
        (repo / "harness/src/scripts" / name).touch()
        (repo / "harness/src/scripts/benchmarks" / name).touch()
    save_json(repo / "harness/reference-source-manifest.json", source_manifest)
    for suite, folder in [("pinch", "pinchbench"), ("terminal", "terminal-bench-4.0")]:
        save_json(
            repo / folder / "frozen-plan.json",
            {
                "status": "Paused; no accepted scored runs in this repository",
                "revision": selection["dataset_revisions"][suite],
                "tasks": sanitizer.transform(selection[suite]),
                "policy": sanitizer.transform(
                    selection.get(
                        suite + "_selection", selection.get("terminal_speed_selection")
                    )
                ),
                "attempts_per_agent_per_task": 3,
                "agents": data["conditions"]["agents"],
            },
        )
    presentation = repo / "harness/reporting/wild_report.html"
    template = presentation.read_text()
    palette = json.loads((presentation.parent / "agent-colors.json").read_text())
    template = template.replace(
        "__AGENT_COLORS__",
        json.dumps({agent: palette[agent] for agent in ("leon", "hermes", "openclaw")}),
    )
    replacements = {
        'href="evidence-manifest.json"': 'href="../public-manifest.json"',
        'href="freeze-verification.json"': 'href="../provenance/freeze-verification.json"',
        'href="metrics-plan.json"': 'href="../metrics-plan.json"',
        'href="wildclawbench-data.json"': 'href="../metrics.json"',
        'href="accepted-attempts.json"': 'href="../accepted-attempts.json"',
        'href="REPRODUCTION_PROMPT.txt"': 'href="../reproduction.txt"',
        'href="frozen-agent-sources.json"': 'href="../provenance/frozen-agent-sources.json"',
        'href="../wild-fairness-review.json"': 'href="../provenance/campaign-snapshots/wild-fairness-review.json"',
        "/verifier/wildclaw_score.json": "/verifier/effective-grade.json",
        ">Original result<": ">Exported result<",
        "D.metrics_plan.original_messages.map": "D.metrics_plan.agreement_provenance.map",
        "+esc(x.text)+": '+esc("Private discussion omitted; the recovered metric contract is preserved above.")+',
    }
    for old, new in replacements.items():
        template = template.replace(old, new)
    template = template.replace(
        "__REPORT_DATA__", json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    )
    (run / "report").mkdir(exist_ok=True)
    (run / "report/index.html").write_text(template)
    for row in mapping:
        row["destination"] = str(Path(row["destination"]).relative_to(repo))
    save_json(
        run / "provenance/publication-map.json",
        {
            "policy": "Credentials, private reasoning, owner paths and inline images removed; scores unchanged.",
            "private_manifest_sha256": digest(
                (archive / "evidence-manifest.json").read_bytes()
            ),
            "transformations": dict(sanitizer.counts),
            "files": mapping,
        },
    )
    shutil.copyfile(repo.parent / "leon/LICENSE.md", repo / "LICENSE")
    print(
        json.dumps(
            {
                "run": str(run),
                "records": len(data["records"]),
                "sanitization_counts": dict(sanitizer.counts),
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--repository", required=True, type=Path)
    arguments = parser.parse_args()
    export(arguments.archive.resolve(), arguments.repository.resolve())
