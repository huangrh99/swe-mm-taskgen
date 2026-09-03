"""Frozen functional verifier for Carbon PR 22019."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import time


APP = Path("/testbed")
TESTS = Path("/tests")
LOGS = Path("/logs/verifier")
RESULTS = Path("/results")


def write_result(result: dict) -> None:
    (LOGS / "test_results.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    )
    (LOGS / "reward.txt").write_text(str(result.get("reward", 0)) + "\n")


def main() -> None:
    result = {
        "schema": "carbon-22019-harbor-result-v1",
        "status": "invalid",
        "reward": 0,
        "failure_ledger": "technical",
        "tests": [],
        "started_at_unix": time.time(),
    }
    try:
        config = json.loads((TESTS / "config.json").read_text())
        manifest = json.loads((TESTS / "test_manifest.json").read_text())
        f2p = config["FAIL_TO_PASS"]
        p2p = config["PASS_TO_PASS"]
        required_ids = f2p + p2p
        if not f2p or not p2p:
            raise ValueError("empty_transition_group")
        if len(required_ids) != len(set(required_ids)):
            raise ValueError("duplicate_or_overlapping_test_id")

        mappings = manifest["tests"]
        mapped_ids = [item["test_id"] for item in mappings]
        mapped_names = [item["jest_full_name"] for item in mappings]
        if set(mapped_ids) != set(required_ids) or len(mapped_ids) != len(required_ids):
            raise ValueError("manifest_config_test_id_mismatch")
        if len(mapped_names) != len(set(mapped_names)):
            raise ValueError("duplicate_jest_full_name")

        RESULTS.mkdir(parents=True, exist_ok=True)
        framework_result = RESULTS / "carbon-22019-jest.json"
        if framework_result.exists() or framework_result.is_symlink():
            framework_result.unlink()

        environment = dict(os.environ)
        environment.update({"CI": "1", "NO_COLOR": "1", "FORCE_COLOR": "0"})
        command = [
            "yarn",
            "test",
            "packages/react/src/components/ExpandableSearch/ExpandableSearch-test.js",
            "--runInBand",
            "--watch=false",
            "--reporters=default",
            "--json",
            f"--outputFile={framework_result}",
        ]
        completed = subprocess.run(
            command,
            cwd=APP,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=600,
            check=False,
        )
        (LOGS / "02_test_stdout.log").write_text(completed.stdout)
        (LOGS / "02_test_stderr.log").write_text(completed.stderr)
        if not framework_result.is_file() or framework_result.is_symlink():
            result["reason"] = "missing_jest_json"
            return

        report = json.loads(framework_result.read_text())
        (LOGS / "framework_results.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        )
        if report.get("numRuntimeErrorTestSuites", 0) != 0:
            result["reason"] = "jest_runtime_error"
            return

        assertions = [
            assertion
            for suite in report.get("testResults", [])
            for assertion in suite.get("assertionResults", [])
        ]
        by_name = {}
        for assertion in assertions:
            by_name.setdefault(assertion.get("fullName"), []).append(assertion)
        if set(by_name) != set(mapped_names):
            result.update(failure_ledger="evidence", reason="missing_or_unexpected_jest_tests")
            return
        if any(len(items) != 1 for items in by_name.values()):
            result.update(failure_ledger="evidence", reason="duplicate_jest_observation")
            return

        status_map = {"passed": "pass", "failed": "fail", "pending": "skip", "todo": "skip"}
        rows = []
        for mapping in mappings:
            assertion = by_name[mapping["jest_full_name"]][0]
            status = status_map.get(assertion.get("status"), "error")
            rows.append(
                {
                    "test_id": mapping["test_id"],
                    "source": mapping["source"],
                    "status": status,
                    "jest_full_name": mapping["jest_full_name"],
                    "failure_messages": assertion.get("failureMessages", [])[:2],
                }
            )
        result["tests"] = rows
        counts = {
            status: sum(row["status"] == status for row in rows)
            for status in ("pass", "fail", "skip", "error")
        }
        result["counts"] = counts
        result["runner_exit_code"] = completed.returncode
        result["FAIL_TO_PASS"] = f2p
        result["PASS_TO_PASS"] = p2p

        if counts["skip"] or counts["error"]:
            result.update(failure_ledger="evidence", reason="unusable_required_test_status")
        elif counts["fail"]:
            if completed.returncode == 0 or report.get("success") is not False:
                result.update(failure_ledger="evidence", reason="jest_failure_exit_mismatch")
            else:
                result.update(status="test_failure", failure_ledger="semantic")
        elif completed.returncode != 0 or report.get("success") is not True:
            result.update(reason="jest_success_exit_mismatch")
        else:
            result.update(status="passed", reward=1, failure_ledger=None)
    except subprocess.TimeoutExpired:
        result["reason"] = "verifier_timeout"
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        result["reason"] = f"{type(exc).__name__}: {exc}"
    finally:
        result["finished_at_unix"] = time.time()
        write_result(result)


if __name__ == "__main__":
    main()
