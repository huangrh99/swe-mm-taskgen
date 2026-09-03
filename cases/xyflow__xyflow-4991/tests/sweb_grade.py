import json
import os
from pathlib import Path
import re
import subprocess
import time

APP = Path("/testbed")
TESTS = Path("/tests")
LOGS = Path("/logs/verifier")
ID_PATTERN = re.compile(r"\[(xyflow-4991-focus-scroll-\d+)\]")


class BehaviorFailure(Exception):
    """A candidate-caused failure that must count as a model failure."""


def collect(report):
    rows = []

    def visit(suites):
        for suite in suites:
            for spec in suite.get("specs", []):
                match = ID_PATTERN.search(spec.get("title", ""))
                if not match:
                    continue
                tests = spec.get("tests", [])
                results = tests[0].get("results", []) if tests else []
                raw = results[-1].get("status", "missing") if results else "missing"
                status = {"passed": "pass", "failed": "fail", "timedOut": "error",
                          "skipped": "skip", "interrupted": "error"}.get(raw, "error")
                rows.append({"test_id": match.group(1), "status": status, "raw_status": raw})
            visit(suite.get("suites", []))

    visit(report.get("suites", []))
    return rows


def main():
    LOGS.mkdir(parents=True, exist_ok=True)
    result = {
        "schema_version": "xyflow-playwright-verifier-v2",
        "status": "invalid",
        "reward": 0,
        "failure_ledger": "infrastructure",
        "retryable": True,
        "started_at_unix": time.time(),
    }
    try:
        config = json.loads((TESTS / "config.json").read_text())
        expected = config["FAIL_TO_PASS"] + config["PASS_TO_PASS"]
        if not config["FAIL_TO_PASS"] or not config["PASS_TO_PASS"] or len(expected) != len(set(expected)):
            raise ValueError("invalid_transition_groups")
        env = dict(os.environ)
        env["CI"] = "1"
        env["CHROME_PATH"] = "/usr/bin/chromium"
        build = subprocess.run(
            ["pnpm", "--filter", "@xyflow/react", "build"],
            cwd=APP, env=env, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=600,
        )
        (LOGS / "10_rebuild_react.log").write_text(build.stdout)
        if build.returncode != 0:
            raise BehaviorFailure(f"candidate_react_package_build_failed:{build.returncode}")
        command = [
            "pnpm", "--dir", "tests/playwright", "exec", "playwright", "test",
            "-c", "playwright.harbor.config.ts", "--project=chromium",
            "e2e/focus.spec.ts", "--reporter=json",
        ]
        proc = subprocess.run(command, cwd=APP, env=env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600)
        (LOGS / "15_test_stdout.json").write_text(proc.stdout)
        (LOGS / "15_test_stderr.log").write_text(proc.stderr)
        report = json.loads(proc.stdout)
        rows = collect(report)
        ids = [row["test_id"] for row in rows]
        if len(ids) != len(set(ids)) or set(ids) != set(expected):
            raise ValueError(f"missing_or_duplicate_tests:{ids}")
        if any(row["status"] in {"skip", "missing", "error"} for row in rows):
            raise ValueError(f"non_result_status:{rows}")
        reward = int(proc.returncode == 0 and all(row["status"] == "pass" for row in rows))
        result.update(
            status="passed" if reward else "test_failure",
            reward=reward,
            failure_ledger="none" if reward else "behavior",
            retryable=False,
            tests=rows,
            counts={
                "pass": sum(row["status"] == "pass" for row in rows),
                "fail": sum(row["status"] == "fail" for row in rows),
                "skip": sum(row["status"] == "skip" for row in rows),
                "error": sum(row["status"] == "error" for row in rows),
            },
            runner_exit_code=proc.returncode,
            f2p=config["FAIL_TO_PASS"],
            p2p=config["PASS_TO_PASS"],
        )
    except BehaviorFailure as exc:
        result.update(status="test_failure", reward=0, failure_ledger="behavior",
                      retryable=False, reason=str(exc))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError,
            json.JSONDecodeError) as exc:
        result.update(status="invalid", reward=0, failure_ledger="infrastructure",
                      retryable=True, reason=f"{type(exc).__name__}: {exc}")
    result["finished_at_unix"] = time.time()
    (LOGS / "test_results.json").write_text(json.dumps(result, indent=2) + "\n")
    (LOGS / "reward.txt").write_text(str(result["reward"]) + "\n")


if __name__ == "__main__":
    main()
