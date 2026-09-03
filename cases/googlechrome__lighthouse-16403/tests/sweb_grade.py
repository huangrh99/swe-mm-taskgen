"""Frozen functional verifier for Lighthouse PR 16403."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time


APP = Path("/testbed")
TESTS = Path("/tests")
LOGS = Path("/logs/verifier")


def write_result(result: dict) -> None:
    (LOGS / "test_results.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    )
    (LOGS / "reward.txt").write_text(str(result.get("reward", 0)) + "\n")


def run(command: list[str], log_name: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
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
    (LOGS / log_name).write_text(completed.stdout)
    return completed


def parse_tests(output: str, test_ids: list[str]) -> list[dict]:
    rows = []
    for test_id in test_ids:
        matching = [line.strip() for line in output.splitlines() if f"[{test_id}]" in line]
        passed = [line for line in matching if "✔" in line or "✓" in line]
        failed = [line for line in matching if re.search(r"(?:^|\s)\d+\)\s+\[", line) or line.startswith("=")]
        if passed and not failed:
            status = "pass"
        elif failed:
            status = "fail"
        else:
            status = "missing"
        rows.append({"test_id": test_id, "status": status, "evidence_lines": matching[:8]})
    return rows


def main() -> None:
    started = time.time()
    result = {
        "schema": "lighthouse-16403-harbor-result-v1",
        "status": "invalid",
        "reward": 0,
        "failure_ledger": "technical",
        "tests": [],
        "started_at_unix": started,
    }
    try:
        config = json.loads((TESTS / "config.json").read_text())
        required = config["FAIL_TO_PASS"] + config["PASS_TO_PASS"]
        if not config["FAIL_TO_PASS"] or not config["PASS_TO_PASS"]:
            raise ValueError("empty_transition_group")
        if len(required) != len(set(required)):
            raise ValueError("duplicate_test_id")

        relative_test = Path(config["test_file"])
        target = APP / relative_test
        if target.exists() or target.is_symlink():
            raise ValueError("agent_created_or_replaced_hidden_test")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(TESTS / "payload/treemap-design-test-pptr.js", target)

        env = dict(os.environ)
        env.update({
            "CI": "1",
            "NO_COLOR": "1",
            "FORCE_COLOR": "0",
            "CHROME_BIN": "/usr/local/bin/chromium-no-sandbox",
            "PUPPETEER_EXECUTABLE_PATH": "/usr/local/bin/chromium-no-sandbox",
        })
        build = run(["yarn", "build-treemap"], "01_build.log", env)
        if build.returncode != 0:
            result["reason"] = "treemap_build_failed"
            return
        tests = run([
            "yarn", "mocha", "--testMatch", config["test_file"],
            "--timeout", "35000",
        ], "02_tests.log", env)
        rows = parse_tests(tests.stdout, required)
        result["tests"] = rows
        counts = {name: sum(row["status"] == name for row in rows)
                  for name in ("pass", "fail", "missing")}
        result["counts"] = counts
        result["runner_exit_code"] = tests.returncode
        if counts["missing"]:
            result.update(status="invalid", failure_ledger="evidence",
                          reason="required_test_not_observed")
        elif counts["fail"]:
            result.update(status="test_failure", failure_ledger="semantic")
        elif tests.returncode != 0:
            result.update(status="invalid", failure_ledger="technical",
                          reason="nonzero_exit_with_all_tests_reported_pass")
        else:
            result.update(status="passed", reward=1, failure_ledger=None)
        result["FAIL_TO_PASS"] = config["FAIL_TO_PASS"]
        result["PASS_TO_PASS"] = config["PASS_TO_PASS"]
    except subprocess.TimeoutExpired:
        result["reason"] = "verifier_timeout"
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        result["reason"] = f"{type(exc).__name__}: {exc}"
    finally:
        result["finished_at_unix"] = time.time()
        write_result(result)


if __name__ == "__main__":
    main()
