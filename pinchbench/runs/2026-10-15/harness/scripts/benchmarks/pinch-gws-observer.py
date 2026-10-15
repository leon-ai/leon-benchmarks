#!/usr/bin/python3
"""Observe actual CLI invocations before delegating to the pinned native binary."""

import json
import os
import shlex
import sys
import time
from pathlib import Path
from uuid import uuid4


def main():
    """Preserve native argv, process identity and I/O while recording execution intent."""
    path = Path("/logs/agent/gws-invocations.jsonl")
    command = shlex.join(["gws", *sys.argv[1:]])
    row = {
        "id": "gws-" + uuid4().hex,
        "argv": ["gws", *sys.argv[1:]],
        "command": command,
        "timestamp": time.time(),
        "source": "observed native CLI invocation; not a model function call",
    }
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(descriptor, (json.dumps(row) + "\n").encode())
    finally:
        os.close(descriptor)
    os.execv(str(Path(__file__).with_name("gws.native")), sys.argv)


if __name__ == "__main__":
    main()
