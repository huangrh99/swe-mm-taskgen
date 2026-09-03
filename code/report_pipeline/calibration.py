"""Validate and apply a two-axis human calibration record to a measured dossier."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path


GATES = ("multimodal_necessity", "f2p_p2p_semantic_validity")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def task_directory_checksum(root: Path) -> str:
    """Return the canonical checksum used by the formal task inventory."""
    root = root.resolve()
    rows = [
        {
            "path": path.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in sorted(root.rglob("*"))
        if path.is_file()
    ]
    material = json.dumps(
        rows, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    return hashlib.sha256(material).hexdigest()


def _timestamp(value: object, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid {label} timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} timestamp must include timezone")
    return parsed


def validate_human_gate_audit(record: dict, gate: str, *, text_first: bool = False) -> None:
    """Validate audit identity and chronology shared by apply and promotion."""
    if record.get("state") == "pending":
        return
    if not all(record.get(key) for key in ("reason", "reviewed_at")):
        raise ValueError(f"completed calibration lacks audit fields: {gate}")
    if not str(record.get("reviewer") or "").strip():
        raise ValueError(f"completed calibration lacks reviewer: {gate}")
    reviewed = _timestamp(record["reviewed_at"], "reviewed_at")
    if not text_first:
        return
    if (not str(record.get("text_only_notes") or "").strip()
            or not record.get("text_only_sufficiency")
            or not record.get("text_first_recorded_at")
            or not record.get("images_revealed_at")):
        raise ValueError("completed visual calibration lacks text-first evidence")
    recorded = _timestamp(record["text_first_recorded_at"], "text_first_recorded_at")
    revealed = _timestamp(record["images_revealed_at"], "images_revealed_at")
    if recorded > revealed:
        raise ValueError("images were revealed before text-first evidence was recorded")
    if revealed > reviewed:
        raise ValueError("visual review was completed before images were revealed")


def _decision(value: dict, gate: str, *, text_first: bool = False) -> dict:
    record = value.get(gate)
    if not isinstance(record, dict) or record.get("state") not in {"pending", "approved", "rejected"}:
        raise ValueError(f"invalid calibration state: {gate}")
    validate_human_gate_audit(record, gate, text_first=text_first)
    return {
        "gate": gate,
        **record,
        "decision": record["state"] if record["state"] != "pending" else None,
    }


def _validate_v2_details(
    decision: dict, dossier: dict, manifest: dict
) -> None:
    visual = decision[GATES[0]]
    safe_ids = {
        item["asset_id"] for item in dossier["leakage_policy"]["safe_agent_assets"]
    }
    selected_ids = visual.get("evidence_asset_ids") or []
    if len(selected_ids) != len(set(selected_ids)) or not set(selected_ids) <= safe_ids:
        raise ValueError("visual calibration cites unknown or duplicate assets")
    if visual["state"] == "approved":
        if visual.get("text_only_sufficiency") != "insufficient":
            raise ValueError("visual approval requires text-only insufficiency")
        if visual.get("ocr_replaceable") != "no":
            raise ValueError("visual approval requires non-OCR-replaceable evidence")
        if not str(visual.get("non_text_visual_fact") or "").strip() or not selected_ids:
            raise ValueError("visual approval requires a visual fact and cited assets")

    semantics = decision[GATES[1]]
    reviews = semantics.get("test_reviews") or []
    expected = [(item["test_id"], item["class"]) for item in manifest["tests"]]
    observed = [(item.get("test_id"), item.get("class")) for item in reviews]
    if observed and observed != expected:
        raise ValueError("semantic calibration test inventory changed")
    if semantics["state"] == "approved":
        if semantics.get("coverage") != "complete":
            raise ValueError("semantic approval requires complete coverage")
        if observed != expected:
            raise ValueError("semantic approval requires every frozen test")
        if any(
            item.get("decision") != "valid" or not str(item.get("reason") or "").strip()
            for item in reviews
        ):
            raise ValueError("semantic approval requires reasoned valid decisions for every test")
        if str(semantics.get("missing_behaviors") or "").strip():
            raise ValueError("semantic approval cannot declare missing behaviors")


def apply(
    dossier_path: Path,
    measurement_path: Path,
    decision_path: Path,
    output: Path,
    manifest_path: Path | None = None,
    task_path: Path | None = None,
    test_context_path: Path | None = None,
) -> dict:
    dossier = json.loads(dossier_path.read_text())
    measurement_record = json.loads(measurement_path.read_text())
    measurement = measurement_record.get("measurement", measurement_record)
    decision = json.loads(decision_path.read_text())
    schema_version = decision.get("schema_version")
    if schema_version not in {"dual-human-calibration-v1", "dual-human-calibration-v2"}:
        raise ValueError("unsupported calibration schema")
    if decision.get("candidate_id") != dossier["candidate_id"]:
        raise ValueError("calibration candidate mismatch")
    if decision.get("dossier_sha256") != _sha(dossier_path) or decision.get("measurement_sha256") != _sha(measurement_path):
        raise ValueError("calibration input binding changed")
    if not measurement.get("all_transitions_match"):
        raise ValueError("cannot calibrate an invalid measurement as this candidate")
    if schema_version == "dual-human-calibration-v2":
        if manifest_path is None or task_path is None or test_context_path is None:
            raise ValueError("v2 calibration requires manifest, task, and test context bindings")
        manifest = json.loads(manifest_path.read_text())
        test_context = json.loads(test_context_path.read_text())
        if manifest.get("candidate_id") != dossier["candidate_id"]:
            raise ValueError("calibration manifest candidate mismatch")
        if decision.get("test_manifest_sha256") != _sha(manifest_path):
            raise ValueError("calibration test manifest binding changed")
        if (decision.get("test_review_context_sha256") != _sha(test_context_path)
                or test_context.get("source_test_manifest_sha256") != _sha(manifest_path)):
            raise ValueError("calibration test review context binding changed")
        expected_context = [(item["test_id"], item["class"]) for item in manifest["tests"]]
        observed_context = [(item.get("test_id"), item.get("class")) for item in test_context.get("tests", [])]
        if test_context.get("candidate_id") != dossier["candidate_id"] or observed_context != expected_context:
            raise ValueError("calibration test review context inventory changed")
        if decision.get("task_directory_checksum") != task_directory_checksum(task_path):
            raise ValueError("calibration task binding changed")
        _validate_v2_details(decision, dossier, manifest)
    visual = _decision(decision, GATES[0], text_first=schema_version == "dual-human-calibration-v2")
    semantics = _decision(decision, GATES[1])
    dossier["visual_admission"]["human_calibration_state"] = visual["state"]
    dossier["visual_admission"]["human_calibration"] = visual
    test = dossier["test_calibration"]
    test.update({"f2p_generation_state": "measured_transition_verified",
                 "p2p_generation_state": "measured_transition_verified",
                 "measurement_state": "executed_all_transitions_match",
                 "measurement_path": str(measurement_path.resolve()),
                 "measurement_sha256": _sha(measurement_path),
                 "human_semantic_calibration_state": semantics["state"],
                 "human_semantic_calibration": semantics})
    approved = (schema_version == "dual-human-calibration-v2"
                and visual["state"] == semantics["state"] == "approved")
    blockers = [gate for gate, value in zip(GATES, (visual, semantics))
                if value["state"] != "approved"]
    if schema_version == "dual-human-calibration-v1":
        blockers.append("dual_human_calibration_v2_required")
    dossier["benchmark_eligibility"] = {
        "current_stage": "final_taskset" if approved else "executable_candidate",
        "may_construct_and_measure_tests": True,
        "may_enter_final_taskset": approved,
        "blocking_human_gates": blockers,
        "rule": ("final admission requires bound v2 text-first calibration and both human gates"
                 if schema_version == "dual-human-calibration-v2"
                 else "legacy v1 calibration is review-only and must be migrated to bound v2"),
    }
    dossier["calibration_binding"] = {"decision_path": str(decision_path.resolve()),
                                      "decision_sha256": _sha(decision_path)}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(dossier, ensure_ascii=False, indent=2) + "\n")
    return dossier
