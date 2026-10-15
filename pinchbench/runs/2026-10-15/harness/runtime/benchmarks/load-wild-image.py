"""Stream the official image without retaining an uncompressed image archive."""

import argparse
import contextlib
import gzip
import hashlib
import http.client
import json
import shutil
import signal
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

IMAGE = "wildclawbench-ubuntu:v1.3"
IDENTITY = "sha256:88cd069e8ead5f4497093671a6d9e39e80259d75d4524f230b52ce439fcb2870"
REVISION = "75f945578aa00cbdb8f46e4d42e4f4e98f704b4f"
ARCHIVE_SHA256 = "22bd175c0e9a62a9e5c68cb7cb723c731403142b5946bff4421e8e1b495dab2e"
ARCHIVE_PATH = "Images/wildclawbench-ubuntu_v1.3.tar"
ARCHIVE_SIZE = 13_479_787_008
CHUNK_SIZE = 1_048_576
RESERVE = 3 * 1_024**3
NETWORK_RETRIES = 5
NETWORK_BACKOFF_SECONDS = 2
NETWORK_TIMEOUT_SECONDS = 120
GZIP_MAGIC = b"\x1f\x8b"


class ResumableReader:
    """Resume interrupted archive reads without replaying bytes or storing the tar."""

    def __init__(self, url):
        self.url = url
        self.offset = 0
        self.response = None

    def read(self, size):
        """Deliver each byte once so downstream hashing retains the upstream identity."""
        if self.offset == ARCHIVE_SIZE:
            return b""
        size = min(size, ARCHIVE_SIZE - self.offset)
        for attempt in range(NETWORK_RETRIES + 1):
            try:
                if self.response is None:
                    request = urllib.request.Request(
                        self.url + f"?stream_offset={self.offset}",
                        headers={"Range": f"bytes={self.offset}-"},
                    )
                    response = urllib.request.urlopen(
                        request, timeout=NETWORK_TIMEOUT_SECONDS
                    )
                    if response.status == 206:
                        content_range = response.headers.get("Content-Range", "")
                        if content_range.split("-", 1)[
                            0
                        ] != f"bytes {self.offset}" or content_range.rsplit("/", 1)[
                            -1
                        ] != str(ARCHIVE_SIZE):
                            response.close()
                            raise RuntimeError(
                                "Image host did not honor the resume offset"
                            )
                    elif response.status != 200 or self.offset:
                        response.close()
                        raise RuntimeError("Image host did not honor the resume offset")
                    self.response = response
                data = self.response.read(size)
                if not data:
                    raise ConnectionResetError(
                        "Archive stream ended before its pinned size"
                    )
                self.offset += len(data)
                return data
            except (OSError, urllib.error.URLError, http.client.IncompleteRead):
                self.close()
                if attempt == NETWORK_RETRIES:
                    raise
                print(f"Resuming archive at byte {self.offset}", flush=True)
                time.sleep(NETWORK_BACKOFF_SECONDS * (attempt + 1))
        raise RuntimeError("Archive read exhausted its retries")

    def close(self):
        """Release a broken or completed HTTP connection."""
        if self.response is not None:
            self.response.close()
            self.response = None


class VerifiedReader:
    """Hash the original download and stop before disk reserves are exhausted."""

    def __init__(self, source, directory):
        self.source = source
        self.directory = directory
        self.sha256 = hashlib.sha256()

    def read(self, size=-1):
        if (
            min(shutil.disk_usage(p).free for p in (Path("/"), self.directory))
            < RESERVE
        ):
            raise RuntimeError("Disk reserve reached while loading the official image")
        data = self.source.read(size)
        self.sha256.update(data)
        return data


def archive_manifest(url):
    """Read the archive's small manifest without storing its layer payloads."""
    request = urllib.request.Request(
        url + "?download=true",
        headers={"Range": f"bytes={ARCHIVE_SIZE - 65_536}-{ARCHIVE_SIZE - 1}"},
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        if response.status != 206:
            raise RuntimeError(
                "Image host does not support bounded archive metadata reads"
            )
        tail = response.read(65_536)
    for offset in range(0, len(tail) - 512, 512):
        try:
            header = tarfile.TarInfo.frombuf(
                tail[offset : offset + 512], "utf8", "strict"
            )
        except tarfile.HeaderError:
            continue
        if header.name == "manifest.json":
            return json.loads(tail[offset + 512 : offset + 512 + header.size])
    raise RuntimeError("Official archive has no bounded Docker manifest")


def repack(source, destination, temporary_directory, layers):
    """Compress embedded layers while preserving Docker's uncompressed layer IDs."""
    # Docker's archive loader decompresses individual layers and verifies each
    # uncompressed diffID against the untouched image configuration.
    with (
        tarfile.open(fileobj=source, mode="r|") as original,
        tarfile.open(fileobj=destination, mode="w|") as packed,
    ):
        for member in original:
            payload = original.extractfile(member) if member.isfile() else None
            if member.name in {"index.json", "oci-layout"}:
                continue
            if member.isfile() and member.name in layers:
                with tempfile.TemporaryFile(dir=temporary_directory) as layer:
                    prefix = payload.read(len(GZIP_MAGIC))
                    # Official archives can mix raw and compressed layers.
                    # Docker unwraps one compression layer before reading tar.
                    if prefix == GZIP_MAGIC:
                        layer.write(prefix)
                        shutil.copyfileobj(payload, layer, length=CHUNK_SIZE)
                    else:
                        with gzip.GzipFile(
                            fileobj=layer, mode="wb", compresslevel=1, mtime=0
                        ) as compressed:
                            compressed.write(prefix)
                            shutil.copyfileobj(payload, compressed, length=CHUNK_SIZE)
                    member.size = layer.tell()
                    # PAX size overrides the tar header, so regenerate it from
                    # the compressed payload to preserve member boundaries.
                    member.pax_headers.pop("size", None)
                    layer.seek(0)
                    packed.addfile(member, layer)
            else:
                packed.addfile(member, payload)
            print(f"Transferred {member.name}", flush=True)


def main():
    """Load only the pinned official image and verify its content identity."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--temporary-directory", type=Path, required=True)
    args = parser.parse_args()
    args.temporary_directory.mkdir(parents=True, exist_ok=True)

    def interrupted(signum, frame):
        raise KeyboardInterrupt("Image loading interrupted")

    signal.signal(signal.SIGTERM, interrupted)
    existing = subprocess.run(
        ["docker", "image", "inspect", IMAGE, "--format", "{{.Id}}"],
        capture_output=True,
        text=True,
    )
    if existing.returncode == 0:
        if existing.stdout.strip() != IDENTITY:
            raise RuntimeError("Existing image tag has an unexpected identity")
        return
    url = f"https://huggingface.co/datasets/internlm/WildClawBench/resolve/{REVISION}/{ARCHIVE_PATH}"
    driver = subprocess.check_output(
        ["docker", "info", "--format", "{{.Driver}}"], text=True
    ).strip()
    if driver != "overlay2":
        raise RuntimeError(
            "Layer compression requires Docker's verified legacy archive loader"
        )
    manifest = archive_manifest(url)
    if len(manifest) != 1 or manifest[0]["RepoTags"] != [IMAGE]:
        raise RuntimeError("Official archive manifest has unexpected images")
    layers = set(manifest[0]["Layers"])
    with (args.temporary_directory / "docker-load.log").open("w") as log:
        loader = subprocess.Popen(
            ["docker", "image", "load", "--quiet"],
            stdin=subprocess.PIPE,
            stdout=log,
            stderr=log,
        )
        try:
            with contextlib.closing(ResumableReader(url)) as response:
                reader = VerifiedReader(response, args.temporary_directory)
                repack(reader, loader.stdin, args.temporary_directory, layers)
                while reader.read(CHUNK_SIZE):
                    pass
                if reader.sha256.hexdigest() != ARCHIVE_SHA256:
                    raise RuntimeError("Official image archive checksum mismatch")
            loader.stdin.close()
            if loader.wait(timeout=600):
                raise RuntimeError("Docker rejected the official image archive")
            identity = subprocess.check_output(
                ["docker", "image", "inspect", IMAGE, "--format", "{{.Id}}"], text=True
            ).strip()
            if identity != IDENTITY:
                raise RuntimeError("Loaded official image identity mismatch")
            print(f"Verified {IMAGE}: {identity}", flush=True)
        finally:
            if loader.poll() is None:
                loader.terminate()
                try:
                    loader.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    loader.kill()
                    loader.wait()


if __name__ == "__main__":
    main()
