"""Strict functional verifier for Excalidraw PR 9002."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import resource
import shutil
import subprocess
import time


APP = Path("/testbed")
TESTS = Path("/tests")
LOGS = Path("/logs/verifier")
REPORT = Path("/tmp/excalidraw-9002-vitest.json")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def production_inventory(root: Path, injected_test: Path) -> dict[str, str]:
    inventory: dict[str, str] = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        current = Path(directory)
        dirs[:] = [name for name in dirs if name not in {".git", "node_modules"}]
        for name in files:
            path = current / name
            if path == injected_test or path.is_symlink():
                continue
            inventory[path.relative_to(root).as_posix()] = digest(path)
    return inventory


def limits() -> None:
    resource.setrlimit(resource.RLIMIT_FSIZE, (32 * 1024 * 1024, 32 * 1024 * 1024))


def main() -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    (LOGS / "reward.txt").write_text("0\n")
    result: dict = {
        "schema_version": "harbor-functional-test-result-v1",
        "status": "invalid",
        "reward": 0,
        "tests": [],
        "started_at_unix": time.time(),
    }
    try:
        config = json.loads((TESTS / "config.json").read_text())
        f2p = config["FAIL_TO_PASS"]
        p2p = config["PASS_TO_PASS"]
        check(bool(f2p) and bool(p2p), "empty_transition_group")
        expected = f2p + p2p
        check(len(expected) == len(set(expected)), "duplicate_or_overlapping_test_ids")

        target = APP / config["test_file"]
        check(not target.exists(), "injected_test_path_already_exists")
        before = production_inventory(APP, target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(TESTS / "payload/elbowArrowFontSize.test.tsx", target)

        environment = os.environ.copy()
        environment.update({"CI": "1", "NO_COLOR": "1", "FORCE_COLOR": "0"})
        command = [
            "yarn", "test:app", "--run", config["test_file"],
            "--reporter=json", f"--outputFile={REPORT}",
        ]
        with (LOGS / "test_stdout.log").open("wb") as stdout, (LOGS / "test_stderr.log").open("wb") as stderr:
            completed = subprocess.run(
                command,
                cwd=APP,
                env=environment,
                stdout=stdout,
                stderr=stderr,
                timeout=900,
                check=False,
                preexec_fn=limits,
            )
        check(REPORT.is_file() and REPORT.stat().st_size < 32 * 1024 * 1024, "missing_vitest_json")
        raw = json.loads(REPORT.read_text())
        assertions = [
            assertion
            for test_result in raw.get("testResults", [])
            for assertion in test_result.get("assertionResults", [])
        ]
        rows = []
        for assertion in assertions:
            title = assertion.get("title")
            status = assertion.get("status")
            check(title in expected, f"unexpected_test_id:{title}")
            check(status in {"passed", "failed"}, f"invalid_test_status:{status}")
            rows.append({
                "test_id": title,
                "class": "F2P" if title in f2p else "P2P",
                "status": "pass" if status == "passed" else "fail",
                "duration_ms": assertion.get("duration"),
                "failure_messages": assertion.get("failureMessages", []),
            })
        check(len(rows) == len(expected), "missing_or_duplicate_test_results")
        check({row["test_id"] for row in rows} == set(expected), "missing_required_test_ids")
        check(completed.returncode in {0, 1}, f"unexpected_runner_exit:{completed.returncode}")
        check((completed.returncode == 0) == all(row["status"] == "pass" for row in rows), "exit_status_mismatch")
        check(production_inventory(APP, target) == before, "test_execution_modified_production_files")

        counts = {status: sum(row["status"] == status for row in rows) for status in ("pass", "fail")}
        result.update(
            status="passed" if counts["fail"] == 0 else "test_failure",
            reward=int(counts["fail"] == 0),
            tests=rows,
            counts=counts,
            runner_exit_code=completed.returncode,
            f2p=f2p,
            p2p=p2p,
            config_sha256=digest(TESTS / "config.json"),
            test_payload_sha256=digest(TESTS / "payload/elbowArrowFontSize.test.tsx"),
        )
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        result.update(status="invalid", reward=0, reason=f"{type(exc).__name__}: {exc}")
    result["finished_at_unix"] = time.time()
    (LOGS / "test_results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    (LOGS / "reward.txt").write_text(f"{result['reward']}\n")


if __name__ == "__main__":
    main()
