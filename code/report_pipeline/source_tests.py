"""Execute frozen source-level assertions against an immutable Git commit."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path


def _git_blob(repo: Path, commit: str, path: str) -> str:
    result = subprocess.run(["git", "-C", str(repo.resolve()), "show", f"{commit}:{path}"],
                            text=True, capture_output=True, check=False)
    if result.returncode:
        raise ValueError(f"missing test input {commit}:{path}: {result.stderr.strip()}")
    return result.stdout.replace("\r\n", "\n")


def run(manifest_path: Path, repo: Path, commit: str) -> dict:
    raw = manifest_path.resolve().read_bytes()
    manifest = json.loads(raw)
    results = []
    for test in manifest["tests"]:
        content = _git_blob(repo, commit, test["path"])
        missing = [fragment for fragment in test["contains_all"] if fragment not in content]
        forbidden = [fragment for fragment in test.get("contains_none", []) if fragment in content]
        results.append({
            "test_id": test["test_id"], "class": test["class"],
            "status": "pass" if not missing and not forbidden else "fail",
            "missing_assertion_indexes": [test["contains_all"].index(item) for item in missing],
            "forbidden_assertion_indexes": [test.get("contains_none", []).index(item) for item in forbidden],
        })
    return {
        "schema_version": "source-test-run-v1", "commit": commit,
        "test_manifest_sha256": hashlib.sha256(raw).hexdigest(), "results": results,
        "summary": {"pass": sum(x["status"] == "pass" for x in results),
                    "fail": sum(x["status"] == "fail" for x in results)},
        "semantic_calibration": "pending_human_review",
        "scope": "executable source semantics; not pixel-render equivalence",
    }


def compare(manifest_path: Path, baseline: dict, reference: dict) -> dict:
    manifest = json.loads(manifest_path.read_text())
    before = {item["test_id"]: item["status"] for item in baseline["results"]}
    after = {item["test_id"]: item["status"] for item in reference["results"]}
    transitions = []
    for test in manifest["tests"]:
        actual = f"{before[test['test_id']]}->{after[test['test_id']]}"
        transitions.append({"test_id": test["test_id"], "class": test["class"],
                            "expected": test["expected_transition"], "actual": actual,
                            "matches": actual == test["expected_transition"]})
    return {"schema_version": "f2p-p2p-measurement-v1", "transitions": transitions,
            "all_transitions_match": all(item["matches"] for item in transitions),
            "semantic_calibration": "pending_human_review",
            "pixel_oracle_present": False}
