#!/usr/bin/env python3
import hashlib
import json
import sys
from pathlib import Path

expected = json.loads(sys.argv[1])
mismatches = []
for path, expected_sha in expected.items():
    try:
        actual_sha = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        if actual_sha != expected_sha:
            mismatches.append({"path": path, "expected_sha256": expected_sha,
                               "actual_sha256": actual_sha, "error": None})
    except Exception as exc:
        mismatches.append({"path": path, "expected_sha256": expected_sha,
                           "actual_sha256": None,
                           "error": f"{type(exc).__name__}: {exc}"})
if mismatches:
    logs = Path("/logs/verifier"); logs.mkdir(parents=True, exist_ok=True)
    record = {"schema_version": "harbor-source-verifier-v2", "reward": 0, "results": [],
              "summary": {"expected": 0, "pass": 0, "fail": 0, "skip": 0,
                          "missing": 0, "error": len(mismatches)},
              "contract_errors": [{"code": "frozen_test_tamper", "mismatches": mismatches}],
              "scope": "frozen test integrity check failed before execution"}
    (logs / "test_results.json").write_text(json.dumps(record, indent=2) + "\n")
    (logs / "reward.txt").write_text("0\n")
    raise SystemExit(1)
