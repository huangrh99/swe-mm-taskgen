"""Build and execute isolated Harbor control variants for one frozen task."""

from __future__ import annotations

import hashlib
import json
import fcntl
import os
import shlex
import shutil
import subprocess
from pathlib import Path

from report_pipeline.harbor_export import refresh_test_contract
from report_pipeline.paths import WORKSPACE_ROOT
from report_pipeline.atomic import assert_no_symlink_chain, write_json


CONTROL_SPECS = (
    ("18_15_01_baseline_nop", "baseline", "nop"),
    ("18_15_02_oracle", "oracle", "oracle"),
    ("18_15_03_missing_source", "missing_source", "oracle"),
    ("18_15_04_required_skip", "skip", "oracle"),
    ("18_15_05_missing_test_id", "missing_id", "oracle"),
    ("18_15_06_frozen_test_tamper", "tamper", "oracle"),
    ("18_15_07_resource_failure", "resource", "oracle"),
    ("18_15_08_preserve_candidate_change", "preservation", "oracle"),
    ("18_15_09_hidden_test_isolation", "isolation", "oracle"),
    ("18_15_10_runtime_integrity", "runtime_integrity", "oracle"),
)


def _json(path: Path) -> dict:
    return json.loads(path.read_text())


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _write_json_atomic(path: Path, value: dict) -> None:
    write_json(path.absolute(), value)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _portable(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(WORKSPACE_ROOT.resolve()).as_posix()
    except ValueError:
        raise ValueError(f"persistent control path is outside the workspace: {resolved}")


def _binding(path: Path) -> dict:
    return {"path": _portable(path), "sha256": _sha(path)}


def _material_checksum(task: Path) -> tuple[str, dict[str, str]]:
    files = {item.relative_to(task).as_posix(): _sha(item) for item in sorted(task.rglob("*"))
             if item.is_file() and item.name != "control_manifest.json"}
    entries = [{"path": name, "sha256": value} for name, value in sorted(files.items())]
    return hashlib.sha256(json.dumps(entries, separators=(",", ":")).encode()).hexdigest(), files


def _append_solution(task: Path, script: str) -> None:
    path = task / "solution/solve.sh"
    text = path.read_text()
    path.write_text(text.rstrip() + "\n" + script.rstrip() + "\n")
    path.chmod(0o755)


def _add_assertion(task: Path, test: dict) -> None:
    manifest_path = task / "tests/test_manifest.json"
    manifest = _json(manifest_path)
    manifest["tests"].append(test)
    _write_json(manifest_path, manifest)
    expected = [{"test_id": item["test_id"], "class": item["class"]} for item in manifest["tests"]]
    refresh_test_contract(task, expected)


def _make_variant(canonical: Path, variants: Path, indexed_name: str, kind: str) -> Path:
    target = variants / indexed_name
    if target.exists():
        raise ValueError(f"control variant already exists: {target}")
    shutil.copytree(canonical, target)
    manifest_path = target / "tests/test_manifest.json"
    manifest = _json(manifest_path)
    original_expected = _json(target / "tests/frozen_inventory.json")["expected_tests"]

    if kind == "missing_source":
        source = manifest["tests"][0]["path"]
        _append_solution(target, f"rm -f -- {shlex.quote('/testbed/' + source)}")
    elif kind == "skip":
        manifest["tests"][0]["enabled"] = False
        _write_json(manifest_path, manifest)
        refresh_test_contract(target, original_expected)
    elif kind == "missing_id":
        manifest["tests"] = manifest["tests"][1:]
        _write_json(manifest_path, manifest)
        refresh_test_contract(target, original_expected)
    elif kind == "tamper":
        manifest_path.write_text(manifest_path.read_text() + "\n")
    elif kind == "resource":
        task_toml = target / "task.toml"
        current = task_toml.read_text()
        if "memory_mb = 12288" not in current or "[verifier]\ntimeout_sec = 3600.0" not in current:
            raise ValueError("unexpected canonical memory setting")
        task_toml.write_text(current.replace("memory_mb = 12288", "memory_mb = 32").replace(
            "[verifier]\ntimeout_sec = 3600.0", "[verifier]\ntimeout_sec = 0.001"))
    elif kind == "preservation":
        source = manifest["tests"][-1]["path"]
        sentinel = "/* harbor-preservation-sentinel */"
        _append_solution(target, f"printf '\\n%s\\n' {shlex.quote(sentinel)} >> {shlex.quote('/testbed/' + source)}")
        _add_assertion(target, {"test_id": "control_preserve_candidate_change", "class": "P2P",
                                "path": source, "expected_transition": "control-only",
                                "contains_all": [sentinel], "contains_none": []})
    elif kind == "isolation":
        solve = target / "solution/solve.sh"
        current = solve.read_text()
        marker = 'printf "hidden tests absent during agent phase\\n" > /testbed/.harbor-hidden-tests-sentinel\n'
        probe = 'if [ -e /tests/test_manifest.json ]; then exit 73; fi\n' + marker
        solve.write_text(current.replace("cd /testbed\n", "cd /testbed\n" + probe))
        solve.chmod(0o755)
        _add_assertion(target, {"test_id": "control_hidden_tests_not_agent_visible", "class": "P2P",
                                "path": ".harbor-hidden-tests-sentinel", "expected_transition": "control-only",
                                "contains_all": ["hidden tests absent during agent phase"], "contains_none": []})
    elif kind == "runtime_integrity":
        solve = target / "solution/solve.sh"
        current = solve.read_text()
        probe = (
            "if touch /tests/agent-write-probe 2>/dev/null; then exit 74; fi\n"
            "if printf x >> /usr/bin/python3 2>/dev/null; then exit 75; fi\n"
            "printf 'runtime paths protected\\n' > /testbed/.harbor-runtime-integrity-sentinel\n"
        )
        solve.write_text(current.replace("cd /testbed\n", "cd /testbed\n" + probe))
        solve.chmod(0o755)
        _add_assertion(target, {"test_id": "control_runtime_paths_read_only", "class": "P2P",
                                "path": ".harbor-runtime-integrity-sentinel",
                                "expected_transition": "control-only",
                                "contains_all": ["runtime paths protected"], "contains_none": []})
    elif kind not in {"baseline", "oracle"}:
        raise ValueError(f"unknown control kind: {kind}")

    checksum, files = _material_checksum(target)
    parent_checksum, _ = _material_checksum(canonical)
    _write_json(target / "control_manifest.json", {
        "schema_version": "harbor-control-variant-v1",
        "status": "control_variant",
        "control_kind": kind,
        "parent_task_material_sha256": parent_checksum,
        "task_material_sha256": checksum,
        "files": files,
    })
    return target


def _trial(job: Path) -> tuple[Path | None, dict | None, dict | None]:
    trials = sorted(path.parent for path in job.glob("*/result.json"))
    if len(trials) != 1:
        return None, None, None
    trial = trials[0]
    verifier = trial / "verifier/test_results.json"
    return trial, _json(trial / "result.json"), _json(verifier) if verifier.is_file() else None


def _expected(kind: str, record: dict) -> tuple[bool, str]:
    reward, summary = record["reward"], record["summary"] or {}
    errors = record["contract_errors"] or []
    results = record.get("results")
    if kind != "resource" and (record.get("command_returncode") != 0
                               or record.get("harbor_exception") is not None
                               or not record.get("verifier_reached")):
        return False, "non-resource controls require a clean Harbor command and verifier"
    if kind not in {"tamper", "resource"}:
        expected_tests = record.get("expected_tests")
        if not isinstance(expected_tests, list) or not expected_tests:
            return False, "frozen expected test inventory is required"
        if not isinstance(results, list) or len(results) != summary.get("expected"):
            return False, "detailed result inventory must equal summary inventory"
        expected_pairs = [(item.get("test_id"), item.get("class")) for item in expected_tests]
        actual_pairs = [(item.get("test_id"), item.get("class"))
                        if isinstance(item, dict) else (None, None) for item in results]
        if len(set(expected_pairs)) != len(expected_pairs) or actual_pairs != expected_pairs:
            return False, "detailed test IDs/classes must equal the ordered frozen inventory"
        observed = {status: 0 for status in ("pass", "fail", "skip", "missing", "error")}
        for item in results:
            status = item.get("status") if isinstance(item, dict) else None
            if status not in observed:
                return False, "every detailed result must have a recognized status"
            observed[status] += 1
        if any(observed[status] != summary.get(status) for status in observed):
            return False, "detailed result statuses must equal summary counts"
    if kind == "baseline":
        exact = all((item["status"], item.get("failure_class")) ==
                    (("fail", "functional_assertion_mismatch") if item["class"] == "F2P"
                     else ("pass", None)) for item in results)
        return reward == 0 and exact, "every F2P fails functionally and every P2P passes"
    if kind == "oracle":
        return reward == 1 and all(item["status"] == "pass" for item in results), "complete canonical inventory passes"
    if kind == "missing_source":
        exact = all((item["status"], item.get("failure_class")) ==
                    ("error", "functional_runner_error") for item in results)
        return reward == 0 and exact, "missing source is a structured error for every required test"
    if kind == "skip":
        skipped = [item for item in results if item["status"] == "skip"]
        exact = len(skipped) == 1 and skipped[0].get("failure_class") == "required_test_disabled"
        return reward == 0 and exact, "exactly one required skip cannot reward"
    if kind == "missing_id":
        missing = [item for item in results if item["status"] == "missing"]
        exact = len(missing) == 1 and missing[0].get("failure_class") == "missing_test_id"
        return reward == 0 and exact, "exactly one missing required ID cannot reward"
    if kind == "tamper":
        detected = any(item.get("code") == "frozen_test_tamper" for item in errors)
        no_tests_executed = results == [] and summary.get("expected") == 0
        return reward == 0 and detected and no_tests_executed, "frozen material tamper is rejected before tests execute"
    if kind == "resource":
        exception = record.get("harbor_exception") or {}
        exact = (record.get("command_returncode") == 0 and not record.get("verifier_reached")
                 and reward is None and results is None
                 and exception.get("exception_type") == "VerifierTimeoutError"
                 and "0.001 seconds" in exception.get("exception_message", ""))
        return exact, "the forced verifier timeout is infrastructure-invalid, not behavioral failure"
    if kind in {"preservation", "isolation", "runtime_integrity"}:
        return reward == 1 and all(item["status"] == "pass" for item in results), "control assertion and canonical inventory pass"
    return False, "unknown control"


def _run_unlocked(canonical: Path, harbor: Path, variants: Path, jobs: Path,
                  output: Path, *, simulation: bool,
                  pass5_config: Path | None) -> dict:
    canonical, harbor = canonical.resolve(), harbor.resolve()
    if not all((canonical / path).is_file() for path in
               ("task.toml", "instruction.md", "tests/test.sh", "solution/solve.sh")) or not harbor.is_file():
        raise ValueError("canonical task or Harbor executable is missing")
    if variants.exists() or jobs.exists() or output.exists():
        raise ValueError("control outputs must not already exist")
    safe_env = {key: value for key, value in os.environ.items() if key in {
        "PATH", "DOCKER_HOST", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL",
        "SSL_CERT_FILE", "SSL_CERT_DIR",
    }}
    harbor_version = "simulation-unpinned"
    if not simulation:
        if pass5_config is None:
            raise ValueError("negative_controls_pass5_config_required")
        from report_pipeline.workflow import (
            _require_formal_pass5_config, _validate_schema,
        )
        config_path = pass5_config.resolve()
        if config_path.is_relative_to((WORKSPACE_ROOT / "tmp").resolve()):
            raise ValueError("negative_controls_pass5_config_must_be_durable")
        config = _json(config_path)
        _validate_schema(config, "frozen_pass5_config_v1.schema.json",
                         "pass5_config_invalid")
        _require_formal_pass5_config(config)
        expected_harbor = (WORKSPACE_ROOT / config["harbor_executable"]).resolve()
        if (harbor != expected_harbor
                or _sha(harbor) != config["harbor_executable_sha256"]):
            raise ValueError("negative_controls_harbor_runtime_mismatch")
        version = subprocess.run([str(harbor), "--version"], text=True,
                                 capture_output=True, check=False, env=safe_env)
        if version.returncode or version.stdout.strip() != config["harbor_version"]:
            raise ValueError("negative_controls_harbor_version_mismatch")
        harbor_version = version.stdout.strip()
    variants.mkdir(parents=True)
    jobs.mkdir(parents=True)

    records = {}
    canonical_checksum = _material_checksum(canonical)[0]
    output.parent.mkdir(parents=True, exist_ok=True)
    base_result = {
        "schema_version": "visual-harbor-negative-controls-v1",
        "canonical_task": _portable(canonical),
        "canonical_task_material_sha256": canonical_checksum,
        "classification_policy": {
            "behavioral_pass": "verifier reached with reward 1 and exact complete inventory",
            "behavioral_failure": "verifier reached with reward 0 and structured fail/skip/missing/error",
            "infrastructure_invalid": "verifier not reached or Harbor trial has infrastructure exception",
        },
        "recovery_policy": "completed controls are atomically checkpointed; restart an interrupted batch under a new numbered output root",
    }
    _write_json_atomic(output, {**base_result, "controls": {}, "completed_controls": 0,
                                "status": "running"})
    for indexed_name, kind, agent in CONTROL_SPECS:
        task = _make_variant(canonical, variants, indexed_name, kind)
        config = {
            "job_name": indexed_name,
            "jobs_dir": str(jobs),
            "n_concurrent_trials": 1,
            "environment": {"type": "docker", "kwargs": {"keep_containers": True}},
            "tasks": [{"path": str(task)}],
        }
        if agent == "nop":
            config["agents"] = [{"name": "nop"}]
        config_path = jobs / f"{indexed_name}_config.json"
        _write_json(config_path, config)
        command = [str(harbor), "run", "-c", str(config_path)]
        completed = subprocess.run(command, text=True, capture_output=True, check=False,
                                   env=safe_env)
        from report_pipeline.workflow import _bytes_contains_secret
        combined_log = (completed.stdout + completed.stderr).encode()
        if _bytes_contains_secret(combined_log):
            raise ValueError("negative_controls_command_log_secret_detected")
        command_log = jobs / f"{indexed_name}_command.log"
        command_log.write_bytes(combined_log)
        command_receipt = jobs / f"{indexed_name}_command_receipt.json"
        _write_json(command_receipt, {
            "schema_version": "harbor-command-receipt-v1",
            "argv": command,
            "returncode": completed.returncode,
            "combined_log_sha256": _sha(command_log),
            "harbor_version": harbor_version,
        })
        job = jobs / indexed_name
        trial, trial_result, verifier_result = _trial(job)
        job_result = job / "result.json"
        from report_pipeline.workflow import _file_contains_secret
        native_paths = ([job_result, trial / "result.json"] if trial else [job_result])
        if trial and (trial / "verifier/test_results.json").is_file():
            native_paths.append(trial / "verifier/test_results.json")
        if trial and (trial / "exception.txt").is_file():
            native_paths.append(trial / "exception.txt")
        if any(path.is_file() and _file_contains_secret(path) for path in native_paths):
            raise ValueError("negative_controls_raw_artifact_secret_detected")
        exception = trial_result.get("exception_info") if trial_result else None
        public_exception = ({key: exception.get(key) for key in
                             ("exception_type", "exception_message", "occurred_at")}
                            if exception else None)
        record = {
            "indexed_name": indexed_name,
            "task": _portable(task),
            "task_material_sha256": _json(task / "control_manifest.json")["task_material_sha256"],
            "command_returncode": completed.returncode,
            "job": _portable(job),
            "trial": _portable(trial) if trial else None,
            "harbor_exception": public_exception,
            "raw_exception_log": _portable(trial / "exception.txt") if trial and (trial / "exception.txt").is_file() else None,
            "verifier_reached": verifier_result is not None,
            "reward": verifier_result.get("reward") if verifier_result else None,
            "summary": verifier_result.get("summary") if verifier_result else None,
            "contract_errors": verifier_result.get("contract_errors") if verifier_result else None,
            "results": verifier_result.get("results") if verifier_result else None,
            "expected_tests": _json(task / "tests/frozen_inventory.json")["expected_tests"],
            "raw": {
                "control_manifest": _binding(task / "control_manifest.json"),
                "frozen_inventory": _binding(task / "tests/frozen_inventory.json"),
                "job_config": _binding(config_path),
                "command_log": _binding(command_log),
                "command_receipt": _binding(command_receipt),
                "harbor_executable": _binding(harbor),
                "job_result": _binding(job_result) if job_result.is_file() else None,
                "trial_result": _binding(trial / "result.json") if trial else None,
                "verifier_result": (_binding(trial / "verifier/test_results.json")
                                    if trial and (trial / "verifier/test_results.json").is_file()
                                    else None),
                "exception_log": (_binding(trial / "exception.txt")
                                  if trial and (trial / "exception.txt").is_file()
                                  else None),
            },
        }
        passed, expectation = _expected(kind, record)
        record["expected_outcome"] = expectation
        record["control_passed"] = passed
        if not record["verifier_reached"] or record["harbor_exception"]:
            outcome_class = "infrastructure_invalid"
        elif kind in {"skip", "missing_id", "tamper"}:
            outcome_class = "test_contract_invalid"
        elif kind == "missing_source":
            outcome_class = "test_execution_invalid"
        else:
            outcome_class = "behavioral_pass" if record["reward"] == 1 else "behavioral_failure"
        record["outcome_class"] = outcome_class
        records[kind] = record
        _write_json_atomic(output, {**base_result, "controls": records,
                                    "completed_controls": len(records), "status": "running"})

    result = {
        **base_result,
        "controls": records,
        "completed_controls": len(records),
        "status": "all_controls_passed" if all(item["control_passed"] for item in records.values())
                  else "control_expectation_failed",
    }
    _write_json_atomic(output, result)
    return result


def run(canonical: Path, harbor: Path, variants: Path, jobs: Path, output: Path, *,
        simulation: bool = True, pass5_config: Path | None = None) -> dict:
    """Run one control batch at a time for one common output root."""
    variants, jobs, output = variants.absolute(), jobs.absolute(), output.absolute()
    workspace = WORKSPACE_ROOT.absolute()
    if not output.parent.is_relative_to(workspace):
        raise ValueError("negative_control_output_root_outside_workspace")
    if (variants.parent != output.parent or jobs.parent != output.parent
            or len({variants.name, jobs.name, output.name}) != 3):
        raise ValueError("negative_control_outputs_must_share_one_root")
    if output.is_symlink() or variants.is_symlink() or jobs.is_symlink():
        raise ValueError("negative_control_output_symlink")
    assert_no_symlink_chain(output.parent)
    output.parent.mkdir(parents=True, exist_ok=True)
    parent_descriptor = os.open(
        output.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        descriptor = os.open(
            ".negative-controls.lock",
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_descriptor,
        )
    finally:
        os.close(parent_descriptor)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("negative_controls_in_progress") from None
        return _run_unlocked(canonical, harbor, variants, jobs, output,
                             simulation=simulation, pass5_config=pass5_config)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
