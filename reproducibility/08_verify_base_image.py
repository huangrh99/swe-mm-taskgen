#!/usr/bin/env python3
"""Verify or restore the selected task's content-bound Docker base image."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


WORKSPACE = Path(__file__).resolve().parents[2]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inspect(reference: str) -> str | None:
    completed = subprocess.run(
        ["docker", "image", "inspect", reference, "--format", "{{.Id}}"],
        text=True,
        capture_output=True,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def verify(binding_path: Path, *, restore: bool = False) -> dict:
    binding_path = binding_path.resolve()
    if not binding_path.is_relative_to(WORKSPACE) or binding_path.name != "base_image.json":
        raise ValueError("binding must be a workspace base_image.json")
    binding = json.loads(binding_path.read_text())
    archive = WORKSPACE / binding["offline_archive"]
    if not archive.is_file():
        raise ValueError(f"offline archive missing: {archive}")
    observed_archive_sha = _sha256(archive)
    if observed_archive_sha != binding["offline_archive_sha256"]:
        raise ValueError("offline archive checksum mismatch")
    reference = binding["build_reference"]
    observed_image_id = _inspect(reference)
    restored = False
    if observed_image_id is None and restore:
        subprocess.run(["docker", "load", "--input", str(archive)], check=True)
        observed_image_id = _inspect(reference)
        restored = True
    if observed_image_id != binding["image_id"]:
        raise ValueError(
            f"Docker image binding mismatch: expected {binding['image_id']}, observed {observed_image_id}"
        )
    return {
        "status": "verified",
        "binding": binding_path.relative_to(WORKSPACE).as_posix(),
        "archive": binding["offline_archive"],
        "archive_sha256": observed_archive_sha,
        "image_reference": reference,
        "image_id": observed_image_id,
        "restored": restored,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binding", type=Path, required=True)
    parser.add_argument("--restore", action="store_true", help="load the bound archive if the tag is absent")
    args = parser.parse_args()
    print(json.dumps(verify(args.binding, restore=args.restore), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
