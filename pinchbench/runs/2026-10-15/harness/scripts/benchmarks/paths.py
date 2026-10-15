"""Resolve archived evidence paths without changing original result files."""

import json
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def path_mappings() -> dict[str, str]:
    """Load the immutable relocation receipt once for repeated report lookups."""
    receipt = ROOT / "relocation.json"
    if not receipt.exists():
        return {}

    return json.loads(receipt.read_text())["path_mappings"]


def resolve_saved_path(value: str | Path) -> Path:
    """Apply recorded workspace moves to historical absolute evidence references."""
    path = Path(value)
    mappings = path_mappings()
    for source in sorted(mappings, key=len, reverse=True):
        if path.is_relative_to(source):
            return Path(mappings[source]) / path.relative_to(source)

    return path
