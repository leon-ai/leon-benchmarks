"""Start PinchBench's seeded mock service inside one disposable task environment."""

import json
import os
import sys
from pathlib import Path

ENVIRONMENT_FILE = Path("/run/pinchbench-mock.json")
SOURCE_SCRIPTS = Path("/opt/pinch-dependencies/pinch-scripts")


def main():
    """Keep Google Workspace operations mocked and subscription inference direct."""
    sys.path.insert(0, str(SOURCE_SCRIPTS))
    from lib_fws import MOCK_GWS_ENV_KEYS, start_fws

    os.environ["FWS_DATA_DIR"] = "/tmp/pinchbench-fws"
    start_fws()
    environment = {
        key: os.environ[key].strip("\"'")
        for key in MOCK_GWS_ENV_KEYS
        if key in os.environ
    }
    environment["FWS_DATA_DIR"] = os.environ["FWS_DATA_DIR"]
    environment["NO_PROXY"] = "chatgpt.com,localhost,127.0.0.1"
    environment["no_proxy"] = environment["NO_PROXY"]
    ENVIRONMENT_FILE.write_text(json.dumps(environment) + "\n")
    print(json.dumps(environment))


if __name__ == "__main__":
    main()
