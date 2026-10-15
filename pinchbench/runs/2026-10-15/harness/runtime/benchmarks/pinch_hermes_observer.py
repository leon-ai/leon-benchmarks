"""Record actual nested Hermes dispatches without changing native tool behavior."""

import json
import threading
import time
from pathlib import Path
from uuid import uuid4


def install(directory: Path):
    """Expose executed RPC calls that native conversation messages summarize as code."""
    from tools import code_execution_rpc

    original = code_execution_rpc._handle_rpc_request
    lock = threading.Lock()
    path = directory / "nested-tool-calls.jsonl"
    directory.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)

    def observe(request, **kwargs):
        dispatch = kwargs["dispatch"]

        def recorded_dispatch(name, arguments):
            started = time.time()
            captured = json.loads(json.dumps(arguments, default=str))
            result = None
            error = None
            try:
                result = dispatch(name, arguments)
                return result
            except Exception as exception:
                error = type(exception).__name__
                raise
            finally:
                row = {
                    "id": "nested-" + uuid4().hex,
                    "name": name,
                    "arguments": captured,
                    "result": result,
                    "error": error,
                    "started_at": started,
                    "finished_at": time.time(),
                    "source": "native execute_code RPC dispatch",
                }
                with lock:
                    with path.open("a") as journal:
                        journal.write(json.dumps(row, default=str) + "\n")

        return original(request, **(kwargs | {"dispatch": recorded_dispatch}))

    code_execution_rpc._handle_rpc_request = observe
