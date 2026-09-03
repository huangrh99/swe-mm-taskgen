#!/usr/bin/env python3
"""Refresh the closed pipeline-freeze inventory after reviewed code changes."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path


HERE = Path(__file__).resolve().parent
WORKSPACE = HERE.parent
sys.path.insert(0, str(WORKSPACE / "code"))

from report_pipeline.workflow import REQUIRED_FREEZE_CODE, REQUIRED_FREEZE_SCHEMAS  # noqa: E402
from report_pipeline.atomic import write_json  # noqa: E402


MANIFEST = HERE / "09_pipeline_freeze_manifest.json"


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def inventory(paths: set[str]) -> list[dict[str, str]]:
    missing = [path for path in sorted(paths) if not (WORKSPACE / path).is_file()]
    if missing:
        raise ValueError(f"freeze input is missing: {missing[0]}")
    return [{"path": path, "sha256": digest(WORKSPACE / path)} for path in sorted(paths)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--formal-ready", action="store_true")
    args = parser.parse_args()
    value = json.loads(MANIFEST.read_text())
    if args.formal_ready:
        if value.get("dependencies", {}).get("clean_hash_locked_resolution") is not True:
            raise ValueError("formal-ready freeze requires clean hash-locked dependencies")
        if value.get("limitations"):
            raise ValueError("formal-ready freeze cannot retain unresolved limitations")
    dependencies = value["dependencies"]
    docker = value["docker"]
    harbor = value["harbor"]
    value["formal_promotion_ready"] = {
        "status": "ready" if args.formal_ready else "blocked",
        "clean_hash_locked_resolution": dependencies.get("clean_hash_locked_resolution") is True,
        "blocking_limitations": [] if args.formal_ready else list(value.get("limitations", [])),
        "runtime_bindings": {
            "harbor_runtime_snapshot_sha256":
                dependencies["harbor_runtime"]["installed_snapshot"]["sha256"],
            "verifier_runtime_snapshot_sha256":
                dependencies["verifier_runtime"]["installed_snapshot"]["sha256"],
        },
        "docker_binding": {key: docker.get(key) for key in (
            "client_version", "compose_version", "daemon_version", "daemon_observed_at")},
        "harbor_binding": {key: harbor.get(key) for key in ("version", "task_schema")},
    }
    value["frozen_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    value["code"] = inventory(REQUIRED_FREEZE_CODE)
    value["schemas"] = inventory(REQUIRED_FREEZE_SCHEMAS)
    write_json(MANIFEST, value)
    print(json.dumps({"manifest": str(MANIFEST), "code": len(value["code"]),
                      "schemas": len(value["schemas"]),
                      "formal_promotion_ready": value["formal_promotion_ready"]["status"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
