"""Stage the recorded harness and selection without launching agents or copying scores."""

import argparse
import json
import shutil
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
REFERENCE = REPOSITORY / "wildclawbench/runs/2026-10-12"


def prepare(workspace, reference=REFERENCE):
    """Require an empty destination so a reproduction cannot overwrite accepted evidence."""
    if workspace.exists() and any(workspace.iterdir()):
        raise ValueError("The reproduction workspace must be empty.")
    workspace.mkdir(parents=True, exist_ok=True)
    captured = reference / "harness/scripts"
    if not captured.is_dir():
        captured = REPOSITORY / "harness/src/scripts"
    shutil.copytree(captured, workspace / "scripts")
    (workspace / "scripts/__init__.py").touch(exist_ok=True)
    campaign = workspace / "campaign"
    campaign.mkdir()
    for name in ["selection.json", "runtime-provenance.json", "scheduling-policy.json"]:
        shutil.copyfile(
            reference / "provenance/campaign-snapshots" / name, campaign / name
        )
    plan = json.loads((reference / "conditions.json").read_text())
    plan["status"] = (
        "Prepared; dependency, fixture, path and subscription preflights required"
    )
    (campaign / "campaign.json").write_text(json.dumps(plan, indent=2) + "\n")
    for name in ["runtime.json", "mounts.json"]:
        shutil.copyfile(
            reference / "provenance/campaign-snapshots" / name,
            campaign / (name + ".template"),
        )
    print(
        "Staged offline. No authentication, container creation or inference was performed."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument(
        "--run",
        type=Path,
        default=REFERENCE,
        help="Dated published run whose code and conditions should be staged",
    )
    arguments = parser.parse_args()
    prepare(arguments.workspace.resolve(), arguments.run.resolve())
