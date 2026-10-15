---
id: pinch-native-session-smoke
name: Native conversation and workspace preflight
category: preflight
grading_type: automated
timeout_seconds: 180
workspace_files: []
sessions:
- prompt: 'Remember my codeword: 3061eade1cce49449690c68bf0ec3d6d. Create session-contract.txt
    containing exactly that codeword. Reply with the codeword.'
- prompt: What is my codeword from our conversation? Reply with it. Do not read files
    for this turn.
- new_session: true
  prompt: Read session-contract.txt in the current directory and reply with its contents.
---

## Prompt

Remember my codeword: 3061eade1cce49449690c68bf0ec3d6d. Create session-contract.txt containing exactly that codeword. Reply with the codeword.

## Automated Checks

```python
SECRET = '3061eade1cce49449690c68bf0ec3d6d'
def grade(transcript, workspace_path):
    import json
    from pathlib import Path
    logs = Path("/logs/agent")
    turns = json.loads((logs / "pinch-turns.json").read_text())["turns"]
    assert len(turns) == 3 and all(row["completed"] for row in turns)
    folders = sorted((logs / "turns").iterdir())
    assert (Path(workspace_path) / "session-contract.txt").read_text().strip() == SECRET
    leon = [folder / "leon-result.json" for folder in folders]
    hermes = [folder / "hermes-result.json" for folder in folders]
    if all(path.exists() for path in leon):
        records = [json.loads(path.read_text()) for path in leon]
        ids = [record["sessionId"] for record in records]
        assert ids[0] == ids[1] and ids[1] != ids[2]
        assert all(SECRET in json.dumps(record["result"]) for record in records)
    elif all(path.exists() for path in hermes):
        records = [json.loads(path.read_text()) for path in hermes]
        assert records[1]["current_turn_user_idx"] > records[0]["current_turn_user_idx"]
        assert records[2]["current_turn_user_idx"] == records[0]["current_turn_user_idx"]
        assert all(SECRET in record.get("final_response", "") for record in records)
    else:
        histories = [[json.loads(line) for line in (folder / "native-transcript.jsonl").read_text().splitlines() if line.strip()] for folder in folders]
        counts = [sum(row.get("message", {}).get("role") == "user" for row in history) for history in histories]
        assert counts[1] > counts[0] and counts[2] == counts[0]
        for history in histories:
            assistants = [row for row in history if row.get("message", {}).get("role") == "assistant"]
            assert assistants and SECRET in json.dumps(assistants[-1])
    return {"native_sessions_and_workspace": 1.0}
```
