"""Produce promotion-grade nop/oracle Harbor control evidence."""

from __future__ import annotations

import json
from pathlib import Path

from report_pipeline.atomic import write_json
from report_pipeline.harbor_export import validate_publication
from report_pipeline.paths import REPORT_ROOT, TMP_ROOT
from report_pipeline.workflow import (
    _expected_harbor_binding, _expected_test_rows, _portable, _require_formal_pass5_config,
    _sha256, _task_inventory, _validate_schema, _validate_verifier_details,
)


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def _trial(job: Path) -> tuple[Path, Path, Path, dict, dict]:
    candidates = sorted(path.parent.parent for path in job.glob("*/verifier/test_results.json"))
    if len(candidates) != 1:
        raise ValueError(f"expected exactly one completed trial in {job}, found {len(candidates)}")
    trial = candidates[0]
    result_path = trial / "result.json"
    verifier_path = trial / "verifier/test_results.json"
    if (job.is_symlink() or trial.is_symlink() or result_path.is_symlink()
            or verifier_path.is_symlink()):
        raise ValueError("Harbor control evidence contains a symlink")
    return trial, result_path, verifier_path, _load(result_path), _load(verifier_path)


def audit(task: Path, baseline_job: Path, oracle_job: Path, output: Path,
          mode: str = "real", negative_controls: Path | None = None,
          pass5_config: Path | None = None) -> dict:
    task, output = task.resolve(), output.absolute()
    if mode not in {"simulation", "real"}:
        raise ValueError("control evidence mode is invalid")
    if mode == "real" and (not output.is_relative_to((REPORT_ROOT / "evidence").absolute())
                            or any(path.resolve().is_relative_to(TMP_ROOT.resolve())
                                   for path in (baseline_job, oracle_job))):
        raise ValueError("real control evidence and raw runs must be durable")
    publication = validate_publication(task)
    task_sha256, _ = _task_inventory(task)
    if publication.get("task_material_sha256") != task_sha256:
        raise ValueError("control task publication binding changed")
    if negative_controls is None:
        raise ValueError("negative_controls_required")
    from report_pipeline.workflow import _validate_negative_controls
    negative_binding = {"path": _portable(negative_controls.resolve()),
                        "sha256": _sha256(negative_controls.resolve())}
    expected_harbor = None
    if mode == "real":
        if pass5_config is None:
            raise ValueError("pass5_config_required_for_real_controls")
        config = _load(pass5_config.resolve())
        _validate_schema(config, "frozen_pass5_config_v1.schema.json", "pass5_config_invalid")
        _require_formal_pass5_config(config)
        expected_harbor = _expected_harbor_binding(config)
    _validate_negative_controls(negative_binding, task_sha256, mode == "simulation",
                                expected_harbor)
    judge = _load(task / "tests/config.json")
    manifest = _load(task / "tests/test_manifest.json")
    expected_rows = _expected_test_rows(
        judge["FAIL_TO_PASS"], judge["PASS_TO_PASS"])
    if manifest.get("tests") != expected_rows:
        raise ValueError("judge and frozen manifest inventories differ")

    runs = []
    harbor_task_checksum = None
    for role, agent, job, expected_reward in (
            ("baseline_nop", "nop", baseline_job, 0),
            ("oracle", "oracle", oracle_job, 1)):
        _trial_dir, result_path, verifier_path, native, details = _trial(job)
        reward, statuses = _validate_verifier_details(details, expected_rows)
        expected_statuses = (["fail"] * len(judge["FAIL_TO_PASS"])
                             + ["pass"] * len(judge["PASS_TO_PASS"])
                             if role == "baseline_nop"
                             else ["pass"] * len(expected_rows))
        native_checksum = native.get("task_checksum")
        if (native.get("exception_info") is not None
                or native.get("agent_info", {}).get("name") != agent
                or native.get("config", {}).get("task", {}).get("path") != _portable(task)
                or native.get("verifier_result", {}).get("rewards", {}).get("reward") != reward
                or reward != expected_reward or statuses != expected_statuses
                or not isinstance(native_checksum, str)):
            raise ValueError(f"{role} raw Harbor control semantics changed")
        if harbor_task_checksum is None:
            harbor_task_checksum = native_checksum
        elif native_checksum != harbor_task_checksum:
            raise ValueError("nop and oracle task checksums differ")
        runs.append({
            "role": role, "agent": agent, "task_checksum": native_checksum,
            "reward": reward,
            "result": {"path": _portable(result_path), "sha256": _sha256(result_path)},
            "verifier_result": {"path": _portable(verifier_path),
                                "sha256": _sha256(verifier_path)},
        })
    result = {
        "schema_version": "pipeline-harbor-controls-v1", "mode": mode,
        "instance_id": judge["instance_id"], "task_sha256": task_sha256,
        "harbor_task_checksum": harbor_task_checksum,
        "empty_reward": 0, "gold_reward": 1, "exception_count": 0,
        "negative_controls": negative_binding,
        "runs": runs,
    }
    _validate_schema(result, "pipeline_harbor_controls_v1.schema.json",
                     "harbor_controls_evidence_schema_invalid")
    write_json(output.resolve(), result)
    return result
