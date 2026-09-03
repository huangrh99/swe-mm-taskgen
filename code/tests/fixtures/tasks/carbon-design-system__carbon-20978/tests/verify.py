#!/usr/bin/env python3
import hashlib
import json
import os
import subprocess
from pathlib import Path

def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

tests_root = Path(os.environ.get("HARBOR_TEST_ROOT", "/tests"))
app_root = Path(os.environ.get("HARBOR_APP_ROOT", "/testbed"))
manifest_path = tests_root / "test_manifest.json"
inventory = json.loads((tests_root / "frozen_inventory.json").read_text())
manifest = json.loads(manifest_path.read_text())
expected = inventory["expected_tests"]
expected_ids = [item["test_id"] for item in expected]
actual = manifest.get("tests", [])
actual_ids = [item.get("test_id") for item in actual]
contract_errors = []
if manifest.get("candidate_id") != inventory.get("candidate_id"):
    contract_errors.append({"code": "candidate_id_mismatch"})
if sha256(manifest_path) != inventory.get("test_manifest_sha256"):
    contract_errors.append({"code": "manifest_hash_mismatch"})
if len(actual_ids) != len(set(actual_ids)):
    contract_errors.append({"code": "duplicate_test_id", "actual_ids": actual_ids})
if actual_ids != expected_ids:
    contract_errors.append({"code": "test_inventory_mismatch", "expected_ids": expected_ids,
                            "actual_ids": actual_ids})
for commit in inventory.get("forbidden_git_commits", []):
    probe = subprocess.run(["git", "-C", str(app_root), "cat-file", "-e", f"{commit}^{{commit}}"],
                           text=True, capture_output=True, check=False)
    if probe.returncode == 0:
        contract_errors.append({"code": "source_git_history_leak", "commit": commit})
remotes = subprocess.run(["git", "-C", str(app_root), "remote"], text=True,
                         capture_output=True, check=False)
if remotes.returncode != 0 or remotes.stdout.strip():
    contract_errors.append({"code": "source_git_remote_leak",
                            "remotes": remotes.stdout.splitlines()})

functional_results = {}
functional_error = None
execution = manifest.get("execution")
if execution:
    try:
        completed = subprocess.run(execution["command"], text=True, capture_output=True,
                                   timeout=150, check=False)
        if completed.returncode:
            functional_error = f"runner exited {completed.returncode}: {completed.stderr[-4000:]}"
        else:
            payload = json.loads(completed.stdout)
            runner_items = payload.get("results", [])
            runner_ids = [item.get("test_id") for item in runner_items]
            expected_functional_ids = [item["test_id"] for item in actual
                                       if item.get("kind") == "functional_result"]
            if runner_ids != expected_functional_ids or len(runner_ids) != len(set(runner_ids)):
                contract_errors.append({"code": "functional_result_inventory_mismatch",
                                        "expected_ids": expected_functional_ids,
                                        "actual_ids": runner_ids})
            functional_results = {item["test_id"]: item for item in runner_items
                                  if item.get("test_id") is not None}
    except Exception as exc:
        functional_error = f"{type(exc).__name__}: {exc}"
    if functional_error:
        contract_errors.append({"code": "functional_runner_failed", "error": functional_error})

by_id = {item.get("test_id"): item for item in actual if item.get("test_id") is not None}
results = []
for frozen in expected:
    test_id = frozen["test_id"]
    test = by_id.get(test_id)
    if test is None:
        results.append({"test_id": test_id, "class": frozen["class"], "status": "missing",
                        "failure_class": "missing_test_id", "missing": [], "forbidden": [],
                        "error": "required test ID is absent from the manifest"})
        continue
    if test.get("class") != frozen["class"]:
        contract_errors.append({"code": "test_class_mismatch", "test_id": test_id,
                                "expected": frozen["class"], "actual": test.get("class")})
        results.append({"test_id": test_id, "class": frozen["class"], "status": "error",
                        "failure_class": "test_class_mismatch", "missing": [], "forbidden": [],
                        "error": "test class differs from the frozen inventory"})
        continue
    if test.get("enabled", True) is not True:
        results.append({"test_id": test_id, "class": frozen["class"], "status": "skip",
                        "failure_class": "required_test_disabled", "missing": [], "forbidden": [],
                        "error": "required test was disabled and did not execute"})
        continue
    if test.get("kind") == "functional_result":
        observed = functional_results.get(test_id)
        status = observed.get("status") if observed else "error"
        if status not in {"pass", "fail", "skip", "error"}:
            status = "error"
        results.append({"test_id": test_id, "class": frozen["class"], "status": status,
                        "failure_class": None if status == "pass" else (
                            "functional_assertion_mismatch" if status == "fail" else
                            "functional_test_skipped" if status == "skip" else "functional_runner_error"),
                        "missing": [], "forbidden": [],
                        "error": None if status in {"pass", "fail", "skip"} else functional_error})
        continue
    relative = Path(test["path"])
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        results.append({"test_id": test_id, "class": frozen["class"], "status": "error",
                        "failure_class": "unsafe_test_path", "missing": [], "forbidden": [],
                        "error": "test path escapes the frozen /testbed source root"})
        continue
    target = app_root / relative
    try:
        resolved_root = app_root.resolve(strict=True)
        cursor = resolved_root
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ValueError("test path contains a symlink")
        resolved_target = target.resolve(strict=True)
        if not resolved_target.is_relative_to(resolved_root):
            raise ValueError("test path resolves outside /testbed")
        content = resolved_target.read_text().replace("\r\n", "\n")
        missing = [i for i, value in enumerate(test["contains_all"]) if value not in content]
        forbidden = [i for i, value in enumerate(test.get("contains_none", [])) if value in content]
        passed = not missing and not forbidden
        error, failure_class = None, None if passed else "assertion_mismatch"
    except FileNotFoundError as exc:
        passed, missing, forbidden = False, [], []
        error, failure_class = f"{type(exc).__name__}: {exc}", "missing_source_file"
    except Exception as exc:
        passed, missing, forbidden = False, [], []
        error, failure_class = f"{type(exc).__name__}: {exc}", "test_execution_error"
    status = "pass" if passed else ("error" if failure_class in {"missing_source_file", "test_execution_error"} else "fail")
    results.append({"test_id": test_id, "class": frozen["class"], "status": status,
                    "failure_class": failure_class, "missing": missing, "forbidden": forbidden,
                    "error": error})
for test_id in actual_ids:
    if test_id not in expected_ids:
        test = by_id[test_id]
        results.append({"test_id": test_id, "class": test.get("class"), "status": "error",
                        "failure_class": "unexpected_test_id", "missing": [], "forbidden": [],
                        "error": "test ID is not present in the frozen inventory"})

counts = {name: sum(item["status"] == name for item in results)
          for name in ("pass", "fail", "skip", "missing", "error")}
reward = 1 if (not contract_errors and len(results) == len(expected)
               and counts["pass"] == len(expected)) else 0
record = {"schema_version": "harbor-source-verifier-v2", "reward": reward, "results": results,
          "summary": {"expected": len(expected), **counts}, "contract_errors": contract_errors,
          "scope": "frozen executable source assertions; independent human semantic calibration remains external"}
logs = Path("/logs/verifier"); logs.mkdir(parents=True, exist_ok=True)
(logs / "test_results.json").write_text(json.dumps(record, indent=2) + "\n")
(logs / "reward.txt").write_text(f"{reward}\n")
