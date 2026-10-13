"""Run the pinned PinchBench graders using subscription-only judge transport."""

import importlib.util
import json
import os
import runpy
import sys
from pathlib import Path

from pinch_transcript import collect_all

VERIFIER = Path("/logs/verifier")


def main():
    """Keep native rubrics and hybrid weights, separating infrastructure failures."""
    VERIFIER.mkdir(parents=True, exist_ok=True)
    grade = {}
    adapter = None
    clients = []
    try:
        mock = Path("/run/pinchbench-mock.json")
        if mock.exists():
            os.environ.update(json.loads(mock.read_text()))
            os.environ["PATH"] = (
                "/opt/pinch-dependencies/bin:/opt/pinch-dependencies/node_modules/.bin:/tmp/leon-benchmark/bin/node/bin:"
                + os.environ["PATH"]
            )
        sys.path.insert(0, "/tests/scripts")
        import lib_agent
        import lib_grading
        from lib_tasks import TaskLoader

        spec = importlib.util.spec_from_file_location(
            "subscription_judge", Path(__file__).with_name("run-judge.py")
        )
        adapter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(adapter)
        os.environ["LEON_BENCHMARK_AUDIT_PATH"] = str(VERIFIER / "requests.jsonl")
        runpy.run_path(str(Path(__file__).with_name("audit_openai.py")))
        client = adapter.SubscriptionJudgeClient()
        client.client = client.client.with_options(timeout=300, max_retries=0)
        clients.append(client.client)

        def judge_call(*, prompt, model, timeout_seconds):
            try:
                response = client.complete(
                    messages=[
                        {"role": "system", "content": lib_agent._JUDGE_SYSTEM_MSG},
                        {"role": "user", "content": prompt},
                    ],
                    timeout=timeout_seconds,
                )
                return {
                    "status": "success",
                    "text": response.choices[0].message.content,
                }
            except Exception as error:
                return {"status": "error", "error": type(error).__name__}

        lib_grading.call_judge_api = judge_call
        task = TaskLoader(Path("/tests/tasks")).load_task(
            Path("/tests/tasks") / f"{os.environ['PINCHBENCH_TASK']}.md"
        )
        transcript = collect_all()
        turns = Path("/logs/agent/pinch-turns.json")
        records = json.loads(turns.read_text())["turns"] if turns.exists() else []
        completed = bool(records) and all(row["completed"] for row in records)
        result = lib_grading.grade_task(
            task=task,
            execution_result={
                "transcript": transcript,
                "workspace": os.environ["PINCHBENCH_WORKSPACE"],
                "status": "success" if completed else "timeout",
            },
            skill_dir=Path("/tests"),
            judge_model="gpt-6.1-sol",
            judge_timeout_seconds=300,
            judge_backend="api",
        )
        grade = result.to_dict()
        if "LLM judge failed:" in result.notes:
            raise RuntimeError(result.notes)
        if adapter.FAILED_CALLS:
            raise RuntimeError("Native judge encountered transport failures")
        (VERIFIER / "reward.json").write_text(
            json.dumps({"reward": result.score}) + "\n"
        )
    except Exception as error:
        grade["error"] = f"{type(error).__name__}: {error}"
        # Preserve the infrastructure failure as a grading exclusion, never a zero.
        (VERIFIER / "reward.json").write_text(json.dumps({"reward": None}) + "\n")
        raise
    finally:
        (VERIFIER / "pinch_score.json").write_text(json.dumps(grade, indent=2) + "\n")
        failures = list(adapter.FAILED_CALLS) if adapter else []
        if grade.get("error"):
            failures.append(grade["error"])
        (VERIFIER / "judge-reliability.json").write_text(
            json.dumps({"failed_calls": failures}) + "\n"
        )
        for client in clients:
            client.close()


if __name__ == "__main__":
    main()
