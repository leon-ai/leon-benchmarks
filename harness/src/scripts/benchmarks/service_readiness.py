"""Check native mock service startup without consuming business requests."""

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

READINESS_TIMEOUT_SECONDS = 60
POLL_SECONDS = 0.1


def check(specification: Path, receipt: Path) -> None:
    """Wait on error-exempt OpenAPI routes before native warmup deletes seed files."""
    services = json.loads(specification.read_text())
    deadline = time.monotonic() + READINESS_TIMEOUT_SECONDS
    pending = list(services)
    verified = []
    while pending and time.monotonic() < deadline:
        for service in list(pending):
            try:
                with urllib.request.urlopen(service["url"], timeout=1) as response:
                    document = json.load(response)
                if document.get("openapi") and document.get("paths"):
                    verified.append({**service, "routes": sorted(document["paths"])})
                    pending.remove(service)
            except (OSError, ValueError, urllib.error.URLError):
                continue
        if pending:
            time.sleep(POLL_SECONDS)
    if pending:
        raise RuntimeError("Native mock service startup did not become ready")
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(
        json.dumps(
            {"passed": True, "services": verified, "business_calls": 0}, indent=2
        )
        + "\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("specification", type=Path)
    parser.add_argument("receipt", type=Path)
    args = parser.parse_args()
    check(args.specification, args.receipt)
