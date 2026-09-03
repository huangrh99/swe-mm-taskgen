#!/usr/bin/env python3
"""Run the formal report suite without importing same-named workspace packages."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path


REPORT_ROOT = Path(__file__).resolve().parent
CODE_ROOT = REPORT_ROOT / "code"
WORKSPACE_ROOT = REPORT_ROOT.parent


def _resolved(value: str) -> Path:
    return Path(value or ".").resolve()


sys.path[:] = [str(CODE_ROOT), str(CODE_ROOT / "tests")] + [
    item
    for item in sys.path
    if _resolved(item) not in {WORKSPACE_ROOT, REPORT_ROOT, CODE_ROOT}
]


def _flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _flatten(item)
        else:
            yield item


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _inventory_sha(manifest: dict) -> str:
    entries = [
        {"section": section, "path": item["path"], "sha256": item["sha256"]}
        for section in ("code", "schemas") for item in manifest.get(section, [])
    ]
    return hashlib.sha256(json.dumps(entries, separators=(",", ":")).encode()).hexdigest()


class _Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, value):
        for stream in self.streams:
            stream.write(value)
        return len(value)

    def flush(self):
        for stream in self.streams:
            stream.flush()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", action="store_true",
                        help="atomically publish the bound formal test record")
    args = parser.parse_args()
    expected_executable = REPORT_ROOT / ".runtime/venv/bin/python"
    if (Path(sys.executable).absolute() != expected_executable.absolute()
            or os.environ.get("PYTHONDONTWRITEBYTECODE") != "1"
            or os.environ.get("PYTHONPATH") != "code"):
        parser.error("formal tests require the pinned venv and exact isolation environment")
    suite = unittest.defaultTestLoader.discover(str(CODE_ROOT / "tests"))
    test_ids = sorted(test.id() for test in _flatten(suite))
    buffer = io.StringIO()
    stream = _Tee(sys.stderr, buffer)
    started_at = datetime.now(timezone.utc).isoformat()
    result = unittest.TextTestRunner(verbosity=2, stream=stream).run(suite)
    finished_at = datetime.now(timezone.utc).isoformat()
    if args.evidence:
        from report_pipeline.atomic import write_bytes, write_json

        evidence_root = REPORT_ROOT / "evidence"
        log_path = evidence_root / "final_full_test_run.log"
        record_path = evidence_root / "final_full_test_run.json"
        freeze_path = REPORT_ROOT / "reproducibility/09_pipeline_freeze_manifest.json"
        freeze = json.loads(freeze_path.read_text())
        write_bytes(log_path.resolve(), buffer.getvalue().encode())
        failed = len(result.failures)
        errors = len(result.errors)
        skipped = len(result.skipped)
        write_json(record_path.resolve(), {
            "schema_version": "formal-test-run-v1",
            "status": "passed" if result.wasSuccessful() else "failed",
            "started_at": started_at,
            "finished_at": finished_at,
            "command": [
                "PYTHONDONTWRITEBYTECODE=1", "PYTHONPATH=code",
                ".runtime/venv/bin/python", "test.py", "--evidence",
            ],
            "tests_run": result.testsRun,
            "passed": result.testsRun - failed - errors - skipped,
            "failed": failed,
            "errors": errors,
            "skipped": skipped,
            "test_ids": test_ids,
            "runtime": {
                "executable": expected_executable.relative_to(WORKSPACE_ROOT).as_posix(),
                "resolved_executable_sha256": _sha(Path(sys.executable).resolve()),
                "python_version": sys.version,
                "pythonpath": os.environ["PYTHONPATH"],
                "dont_write_bytecode": os.environ["PYTHONDONTWRITEBYTECODE"],
            },
            "git_head": subprocess.check_output(
                ["git", "-C", str(WORKSPACE_ROOT), "rev-parse", "HEAD"],
                text=True).strip(),
            "test_runner_sha256": _sha(Path(__file__).resolve()),
            "pipeline_freeze_sha256": _sha(freeze_path),
            "formal_inventory_sha256": _inventory_sha(freeze),
            "raw_log": {"path": "report/evidence/final_full_test_run.log",
                        "sha256": _sha(log_path)},
        })
    raise SystemExit(0 if result.wasSuccessful() else 1)
