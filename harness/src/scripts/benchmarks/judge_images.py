"""Fit native judge images within the subscription endpoint's input limits."""

import base64
import hashlib
import io
import json
import math
import subprocess
import sys
from pathlib import Path

PATCH_SIZE = 32
PATCH_LIMIT = 30_000
DIMENSION_LIMIT = 65_535
AUDIT_PATH = Path("/logs/verifier/judge-image-normalizations.jsonl")
DEPENDENCY_MANIFEST = Path(__file__).with_name("grader-dependencies.json")
DEPENDENCY_DIRECTORY = Path("/tmp/leon-benchmark-grader-dependencies")


def image_module():
    """Provide the image adapter dependency without task-time network downloads."""
    try:
        from PIL import Image

        return Image
    except ModuleNotFoundError as error:
        if error.name != "PIL":
            raise

    dependency = json.loads(DEPENDENCY_MANIFEST.read_text())["image_dependency"]
    wheel = DEPENDENCY_MANIFEST.parent / dependency["wheel"]
    if hashlib.sha256(wheel.read_bytes()).hexdigest() != dependency["sha256"]:
        raise RuntimeError("Grader dependency identity changed")

    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            "--target",
            str(DEPENDENCY_DIRECTORY),
            str(wheel),
        ],
        check=True,
    )
    sys.path.insert(0, str(DEPENDENCY_DIRECTORY))
    from PIL import Image

    return Image


def patch_count(width: int, height: int) -> int:
    """Count complete patches, including partial image-edge patches."""
    return math.ceil(width / PATCH_SIZE) * math.ceil(height / PATCH_SIZE)


def prepare_image(image: dict) -> dict:
    """Preserve native images unless original detail would exceed input limits."""
    result = {
        "type": "input_image",
        "image_url": image["url"],
        "detail": image.get("detail", "auto"),
    }
    if result["detail"] not in {"auto", "original"}:
        return result
    if not image["url"].startswith("data:image/"):
        return result

    Image = image_module()

    original = base64.b64decode(image["url"].split(",", 1)[1], validate=True)
    with Image.open(io.BytesIO(original)) as source:
        width, height = source.size
        scale = min(1.0, DIMENSION_LIMIT / max(width, height))
        resized_width = max(1, math.floor(width * scale))
        resized_height = max(1, math.floor(height * scale))
        if patch_count(resized_width, resized_height) > PATCH_LIMIT:
            scale = min(
                scale,
                math.sqrt(PATCH_LIMIT * PATCH_SIZE**2 / (width * height)),
            )
            resized_width = max(1, math.floor(width * scale))
            resized_height = max(1, math.floor(height * scale))
            # Whole edge patches can exceed the budget after initial scaling.
            # Reduce the common scale to the next complete patch boundary.
            while patch_count(resized_width, resized_height) > PATCH_LIMIT:
                scale *= min(
                    (math.ceil(resized_width / PATCH_SIZE) - 1)
                    * PATCH_SIZE
                    / resized_width,
                    (math.ceil(resized_height / PATCH_SIZE) - 1)
                    * PATCH_SIZE
                    / resized_height,
                )
                resized_width = max(1, math.floor(width * scale))
                resized_height = max(1, math.floor(height * scale))

        if (resized_width, resized_height) == (width, height):
            return result

        with source.resize(
            (resized_width, resized_height), Image.Resampling.LANCZOS
        ) as resized:
            buffer = io.BytesIO()
            resized.save(buffer, format="PNG")
            encoded = buffer.getvalue()

    result["image_url"] = "data:image/png;base64," + base64.b64encode(encoded).decode()
    with AUDIT_PATH.open("a") as audit:
        audit.write(
            json.dumps(
                {
                    "policy": "aspect_ratio_preserving_subscription_image_limit",
                    "patch_limit": PATCH_LIMIT,
                    "patch_size": PATCH_SIZE,
                    "detail": result["detail"],
                    "original_dimensions": [width, height],
                    "submitted_dimensions": [resized_width, resized_height],
                    "original_patches": patch_count(width, height),
                    "submitted_patches": patch_count(resized_width, resized_height),
                    "original_sha256": hashlib.sha256(original).hexdigest(),
                    "submitted_sha256": hashlib.sha256(encoded).hexdigest(),
                }
            )
            + "\n"
        )
    return result
