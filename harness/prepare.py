"""Stage the recorded harness and selection without launching agents or copying scores."""

import argparse
import json
import shutil
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
REFERENCE = REPOSITORY / "wildclawbench/runs/2026-10-12"


def prepare(workspace):
    """Require an empty destination so a reproduction cannot overwrite accepted evidence."""
    if workspace.exists() and any(workspace.iterdir()):
        raise ValueError("The reproduction workspace must be empty.")
    workspace.mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPOSITORY / "harness/src/scripts", workspace / "scripts")
    campaign = workspace / "campaign"
    campaign.mkdir()
    for name in ["selection.json", "runtime-provenance.json", "scheduling-policy.json"]:
        shutil.copyfile(
            REFERENCE / "provenance/campaign-snapshots" / name, campaign / name
        )
    plan = json.loads((REFERENCE / "conditions.json").read_text())
    plan["status"] = (
        "Prepared; dependency, fixture, path and subscription preflights required"
    )
    (campaign / "campaign.json").write_text(json.dumps(plan, indent=2) + "\n")
    for name in ["runtime.json", "mounts.json"]:
        shutil.copyfile(
            REFERENCE / "provenance/campaign-snapshots" / name,
            campaign / (name + ".template"),
        )
    print(
        "Staged offline. No authentication, container creation or inference was performed."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    arguments = parser.parse_args()
    prepare(arguments.workspace.resolve())
