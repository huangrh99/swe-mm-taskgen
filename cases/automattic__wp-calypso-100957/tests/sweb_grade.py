"""Strict functional grader for wp-calypso PR 100957."""
import json
import os
from pathlib import Path
import subprocess

APP = Path("/testbed")
TESTS = Path("/tests")
RESULTS = Path("/results")
LOGS = Path("/logs/verifier")


def write_result(payload):
    LOGS.mkdir(parents=True, exist_ok=True)
    (LOGS / "reward.txt").write_text(f"{payload['reward']}\n", encoding="utf-8")
    (LOGS / "test_results.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def main():
    config = json.loads((TESTS / "config.json").read_text(encoding="utf-8"))
    RESULTS.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    jest_json = RESULTS / "jest_results.json"
    command = [
        "yarn",
        "jest",
        "-c",
        "packages/global-styles/jest.config.js",
        "packages/global-styles/src/components/global-styles-variations/__tests__/preview.test.tsx",
        "--runInBand",
        "--no-cache",
        "--json",
        f"--outputFile={jest_json}",
    ]
    env = dict(os.environ)
    env["TZ"] = "UTC"
    completed = subprocess.run(
        command,
        cwd=APP,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=600,
        check=False,
    )
    (LOGS / "framework.log").write_text(completed.stdout, encoding="utf-8")
    if not jest_json.exists():
        write_result(
            {
                "schema_version": "sweb-grade-jest-v1",
                "status": "error",
                "reward": 0,
                "reason": "jest_result_missing",
                "runner_exit_code": completed.returncode,
            }
        )
        return

    raw = json.loads(jest_json.read_text(encoding="utf-8"))
    observed = {}
    for suite in raw.get("testResults", []):
        for assertion in suite.get("assertionResults", []):
            status = assertion.get("status", "missing")
            if status == "pending":
                status = "skip"
            observed[assertion.get("fullName", "")] = status

    f2p = config["FAIL_TO_PASS"]
    p2p = config["PASS_TO_PASS"]
    selected = f2p + p2p
    tests = [
        {
            "test_id": test_id,
            "classification": "F2P" if test_id in f2p else "P2P",
            "status": observed.get(test_id, "missing"),
        }
        for test_id in selected
    ]
    reward = int(all(test["status"] == "passed" for test in tests))
    write_result(
        {
            "schema_version": "sweb-grade-jest-v1",
            "status": "passed" if reward else "failed",
            "reward": reward,
            "runner_exit_code": completed.returncode,
            "counts": {
                status: sum(test["status"] == status for test in tests)
                for status in ("passed", "failed", "skip", "missing", "error")
            },
            "tests": tests,
        }
    )


if __name__ == "__main__":
    main()
