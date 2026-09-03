"""Normalize repeated Harbor baseline/reference trials into promotion evidence."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from report_pipeline.workflow import (_expected_test_rows, _portable, _sha256,
                                      _task_inventory, _task_test_ids,
                                      _validate_schema, _validate_verifier_details)


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def _binding(path: Path) -> dict:
    return {"path": _portable(path), "sha256": _sha256(path)}


def validate_oracle_quality(path: Path, candidate: Path, dossier: dict) -> dict:
    path = path.resolve()
    value = json.loads(path.read_text())
    _validate_schema(value, "oracle_quality_validation_v1.schema.json",
                     "oracle_quality_validation_schema_invalid")
    manifest = candidate / "tests/test_manifest.json"
    if (value["instance_id"] != dossier["candidate_id"]
            or value["test_manifest_sha256"] != _sha256(manifest)):
        raise ValueError("oracle_quality_validation_binding_invalid")
    frozen_ids = set(_task_test_ids(candidate)[0] + _task_test_ids(candidate)[1])
    negative_ids = set()
    controls = [*value["negative_variants"], value["equivalent_positive_variant"]]
    for control in controls:
        result = Path(control["result"]["path"])
        if not result.is_absolute():
            result = (path.parent / result).resolve()
        if not result.is_file() or _sha256(result) != control["result"]["sha256"]:
            raise ValueError("oracle_quality_validation_result_binding_invalid")
        negative_ids.update(control.get("failed_test_ids", []))
    if not negative_ids or not negative_ids <= frozen_ids:
        raise ValueError("oracle_quality_negative_test_identity_invalid")
    if set(value["equivalent_positive_variant"]["passed_test_ids"]) != frozen_ids:
        raise ValueError("oracle_quality_equivalent_variant_did_not_pass_all_tests")
    return value


def _side_run(candidate: Path, dossier: dict, side: str, repetition: int,
              result_path: Path, output: Path) -> tuple[dict, Path]:
    result_path = result_path.resolve()
    verifier_path = result_path.parent / "verifier/test_results.json"
    if (result_path.name != "result.json" or result_path.is_symlink()
            or verifier_path.is_symlink() or not verifier_path.is_file()):
        raise ValueError("harbor_measurement_trial_layout_invalid")
    result = json.loads(result_path.read_text())
    verifier = json.loads(verifier_path.read_text())
    f2p, p2p = _task_test_ids(candidate)
    reward, statuses = _validate_verifier_details(verifier, _expected_test_rows(f2p, p2p))
    expected = (["fail"] * len(f2p) + ["pass"] * len(p2p)
                if side == "baseline" else ["pass"] * (len(f2p) + len(p2p)))
    expected_agent = "nop" if side == "baseline" else "oracle"
    expected_reward = 0 if side == "baseline" else 1
    if (statuses != expected or result.get("exception_info") is not None
            or result.get("config", {}).get("task", {}).get("path") != _portable(candidate)
            or not isinstance(result.get("task_checksum"), str)
            or len(result["task_checksum"]) != 64
            or result.get("agent_info", {}).get("name") != expected_agent
            or result.get("verifier_result", {}).get("rewards", {}).get("reward") != reward
            or reward != expected_reward
            or not result.get("started_at") or not result.get("finished_at")):
        raise ValueError("harbor_measurement_trial_semantics_invalid")
    raw_path = output / "raw" / f"{side}_{repetition:02d}_verifier.json"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(verifier_path, raw_path)
    manifest = candidate / "tests/test_manifest.json"
    base_image = json.loads((candidate / "environment/base_image.json").read_text())
    summary = dict(verifier["summary"])
    summary.update({"flaky": 0, "unexecuted": 0})
    record = {
        "schema_version": "pipeline-test-side-run-v1",
        "side": side,
        "repetition": repetition,
        "trial_id": result["id"],
        "job_id": result["config"]["job_id"],
        "native_task_checksum": result["task_checksum"],
        "agent": expected_agent,
        "native_reward": reward,
        "repository": dossier["repository"],
        "baseline_commit": dossier["git"]["baseline_sha"],
        "reference_commit": dossier["git"]["reference_sha"],
        "tested_commit": dossier["git"][f"{'baseline' if side == 'baseline' else 'reference'}_sha"],
        "task_sha256": _task_inventory(candidate)[0],
        "test_manifest_sha256": _sha256(manifest),
        "test_payload_sha256": _task_inventory(candidate / "tests")[0],
        "command": ["/tests/test.sh"],
        "environment": {"kind": "harbor", "image_id": base_image["image_id"],
                        "platform": result.get("config", {}).get("environment", {}).get("type", "docker")},
        "started_at": result["started_at"],
        "finished_at": result["finished_at"],
        "exit_code": 0,
        "harbor_result": _binding(result_path),
        "raw_output": _binding(raw_path),
        "results": verifier["results"],
        "summary": summary,
    }
    _validate_schema(record, "pipeline_test_side_run_v1.schema.json",
                     "harbor_measurement_side_run_schema_invalid")
    path = output / f"{side}_{repetition:02d}.json"
    _write(path, record)
    return record, path


def build(candidate: Path, dossier_path: Path, baseline_results: list[Path],
          reference_results: list[Path], oracle_quality_path: Path, output: Path) -> dict:
    candidate, dossier_path, output = candidate.resolve(), dossier_path.resolve(), output.resolve()
    if output.exists():
        raise ValueError("harbor_measurement_output_exists")
    if len(baseline_results) < 2 or len(reference_results) < 2:
        raise ValueError("harbor_measurement_requires_two_runs_per_side")
    dossier = json.loads(dossier_path.read_text())
    if dossier.get("candidate_id") is None:
        raise ValueError("harbor_measurement_dossier_invalid")
    oracle_quality = validate_oracle_quality(oracle_quality_path, candidate, dossier)
    output.mkdir(parents=True)
    records: dict[str, list[tuple[dict, Path]]] = {"baseline": [], "reference": []}
    try:
        for side, paths in (("baseline", baseline_results), ("reference", reference_results)):
            for repetition, path in enumerate(paths, 1):
                records[side].append(_side_run(candidate, dossier, side, repetition, path, output))
        native_ids = [record["trial_id"] for side in records for record, _ in records[side]]
        if len(set(native_ids)) != len(native_ids):
            raise ValueError("harbor_measurement_native_trial_reused")
        native_checksums = {
            record["native_task_checksum"] for side in records for record, _ in records[side]}
        if len(native_checksums) != 1:
            raise ValueError("harbor_measurement_native_task_checksum_mismatch")
        f2p, p2p = _task_test_ids(candidate)
        transitions = [
            {"test_id": test_id, "class": klass,
             "expected": "fail->pass" if klass == "F2P" else "pass->pass",
             "actual": "fail->pass" if klass == "F2P" else "pass->pass",
             "matches": True}
            for test_id, klass in [(item, "F2P") for item in f2p] + [(item, "P2P") for item in p2p]
        ]
        measurement = {
            "schema_version": "pipeline-test-measurement-v1", "mode": "real",
            "instance_id": dossier["candidate_id"], "task_sha256": _task_inventory(candidate)[0],
            "test_manifest": _binding(candidate / "tests/test_manifest.json"),
            "baseline_runs": [_binding(path) for _, path in records["baseline"]],
            "reference_runs": [_binding(path) for _, path in records["reference"]],
            "transitions": transitions, "all_transitions_match": True,
            "FAIL_TO_PASS": f2p, "PASS_TO_PASS": p2p,
            "oracle_quality_validation": _binding(oracle_quality_path.resolve()),
            "rationale": "Repeated Harbor baseline/reference trials normalized from bound verifier artifacts.",
        }
        measurement_path = output / "measurement.json"
        _validate_schema(measurement, "pipeline_test_measurement_v1.schema.json",
                         "harbor_measurement_schema_invalid")
        _write(measurement_path, measurement)
        return {"measurement": _binding(measurement_path),
                "baseline_run_count": len(records["baseline"]),
                "reference_run_count": len(records["reference"])}
    except Exception:
        shutil.rmtree(output, ignore_errors=True)
        raise
