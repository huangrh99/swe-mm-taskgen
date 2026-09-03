"""Build one provenance-bound candidate dossier with independent calibrations."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(verifier_path: Path, archive_path: Path,
          classification_path: Path | None = None, *,
          allow_legacy_migration: bool = False) -> dict:
    if classification_path is None and not allow_legacy_migration:
        raise ValueError("formal candidate dossiers require a bound V3 classification")
    if classification_path is not None and allow_legacy_migration:
        raise ValueError("V3 classification and legacy migration are mutually exclusive")
    verifier_path, archive_path = verifier_path.resolve(), archive_path.resolve()
    verifier = json.loads(verifier_path.read_text())
    archive = json.loads(archive_path.read_text())
    packet_path = Path(verifier["packet"]).resolve()
    if _sha(packet_path) != verifier["packet_sha256"]:
        raise ValueError("verifier packet hash changed")
    packet = json.loads(packet_path.read_text())
    curator_path = Path(verifier["curator_assets"]).resolve()
    if _sha(curator_path) != verifier["curator_assets_sha256"]:
        raise ValueError("curator asset manifest hash changed")
    curator = json.loads(curator_path.read_text())
    identity = (archive["repo"], archive["number"], archive["instance_id"])
    verifier_identity = (verifier["repository"], verifier["pr_number"], verifier["case_id"])
    packet_identity = (packet["repository"], packet["pr_number"], packet["case_id"])
    if identity != verifier_identity or identity != packet_identity:
        raise ValueError("verifier, packet, and archive identities differ")
    if curator.get("case_id") != verifier["case_id"]:
        raise ValueError("curator assets belong to a different case")
    provenance = packet["provenance"]
    if Path(provenance["source_archive"]).resolve() != archive_path or provenance["source_archive_sha256"] != _sha(archive_path):
        raise ValueError("packet is not bound to this source archive")
    pr = archive["sections"]["pull_request"]["data"]
    merge = archive["sections"]["merge_commit"]["data"]
    if archive["sections"]["merge_anchor_evidence"]["resolved_sha"] != merge["sha"]:
        raise ValueError("merge evidence does not bind the reference commit")
    visual = verifier["visual_verifier"]
    annotation = verifier.get("annotation") or {}
    text_decision = verifier.get("text_decision") or {}
    legacy_text_evidence_available = bool(annotation and text_decision)
    issue_sources = {item["source_id"] for item in packet["problem_sources"] if item.get("kind") == "issue"}
    if len(issue_sources) != len(packet["problem_sources"]):
        raise ValueError("agent problem sources must all be linked Issues")
    archive_assets = archive["sections"]["assets"]["items"]
    archive_by_id = {item.get("sha256"): item for item in archive_assets}
    if None in archive_by_id or len(archive_by_id) != len(archive_assets):
        raise ValueError("archive asset IDs are missing or duplicated")
    safe_assets = []
    logical_archive_asset_root = archive_path.parent / "11_http_archive"
    if logical_archive_asset_root.is_symlink():
        raise ValueError("archive asset root must not be a symlink")
    archive_asset_root = logical_archive_asset_root.resolve(strict=True)
    for item in curator["assets"]:
        if item["status"] != "available":
            continue
        sources = set(item.get("source_ids", []))
        archived = archive_by_id.get(item.get("asset_id"))
        archived_local = Path((archived or {}).get("local_path", "__missing__"))
        if archived_local.is_absolute() or ".." in archived_local.parts:
            raise ValueError("unsafe archive asset path")
        expected_path = archive_asset_root / archived_local
        try:
            resolved_expected = expected_path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError("unsafe or missing archive asset") from exc
        archived_sources = {value.removeprefix("issue:") for value in (archived or {}).get("sources", [])}
        if (item.get("sha256") != item.get("asset_id") or not sources or not sources <= issue_sources
                or archived is None or sources != archived_sources
                or not resolved_expected.is_relative_to(archive_asset_root)
                or expected_path.is_symlink()
                or Path(item.get("local_path", "")).resolve() != resolved_expected
                or not resolved_expected.is_file() or _sha(resolved_expected) != item["asset_id"]):
            raise ValueError("unsafe or unbound curator asset")
        safe_assets.append(item)
    safe_ids = {item["asset_id"] for item in safe_assets}
    if len(safe_ids) != len(safe_assets):
        raise ValueError("duplicate safe agent assets")
    curator_only = [item for item in archive_assets if item.get("sha256") not in safe_ids]
    separable = bool(safe_assets) and all(item.get("sha256") for item in curator_only)
    legacy_eligible = (
        verifier["status"] == "complete"
        and archive["status"] == "complete"
        and pr["merged_at"] is not None
        and pr["base"]["ref"] == pr["base"]["repo"]["default_branch"]
        and visual["decision"]["bucket"] == "visual_necessary"
        and annotation.get("confidence") == "high"
        and text_decision.get("bucket") == "visual_candidate"
        and separable
    )
    classification_record = None
    capability = {}
    capability_annotation = {}
    v3_eligible = False
    if classification_path is not None:
        classification_path = classification_path.resolve()
        classification = json.loads(classification_path.read_text())
        from report_pipeline.pre_review_classification import validate_classification_run
        classification = validate_classification_run(
            Path(classification.get("source_run", "")), classification_path)
        records = [item for item in classification.get("records", [])
                   if item.get("case_id") == verifier["case_id"]]
        if len(records) != 1:
            raise ValueError("pre-review classification identity is missing or duplicated")
        classification_record = records[0]
        if (classification_record.get("source_result_sha256") != _sha(verifier_path)
                or classification_record.get("source_packet_sha256") != _sha(packet_path)
                or classification_record.get("source_archive_sha256") != _sha(archive_path)):
            raise ValueError("pre-review classification source binding changed")
        capability = classification_record.get("visual_capability", {})
        capability_annotation = capability.get("annotation") or {}
        v3_eligible = (
            capability.get("status") == "complete"
            and capability_annotation.get("strict_multimodal_admission")
            == "非文字视觉信息候选不可替代"
            and capability_annotation.get("human_review_required") is False
            and archive["status"] == "complete"
            and pr["merged_at"] is not None
            and pr["base"]["ref"] == pr["base"]["repo"]["default_branch"]
            and separable
        )
    if classification_path is not None:
        eligible = v3_eligible
        admission_route = ("v3_strict_nontext_visual" if v3_eligible
                           else "v3_review_or_exclude")
    else:
        eligible = False
        admission_route = "legacy_migration_review_only"
    changed = [
        {key: item.get(key) for key in ("filename", "status", "additions", "deletions")}
        for item in archive["sections"]["files"]["items"]
    ]
    return {
        "schema_version": "visual-harbor-candidate-v1",
        "candidate_id": verifier["case_id"],
        "status": "admitted_to_test_construction" if eligible else "review_or_exclude",
        "repository": archive["repo"],
        "pr_number": archive["number"],
        "url": pr["html_url"],
        "title": pr["title"],
        "source_bindings": {
            "archive_path": str(archive_path), "archive_sha256": _sha(archive_path),
            "verifier_path": str(verifier_path), "verifier_sha256": _sha(verifier_path),
            "packet_path": str(packet_path), "packet_sha256": _sha(packet_path),
            "curator_assets_path": str(curator_path), "curator_assets_sha256": _sha(curator_path),
            **({"classification_path": str(classification_path),
                "classification_sha256": _sha(classification_path)}
               if classification_path is not None else {}),
        },
        "git": {
            "default_branch": pr["base"]["ref"],
            "api_base_sha_observation": pr["base"]["sha"],
            "pr_head_sha": pr["head"]["sha"],
            "reference_sha": merge["sha"],
            "baseline_sha": merge["parents"][0]["sha"],
            "merge_method": "single-parent squash commit",
        },
        "changed_files": changed,
        "author_test_change_detected": any("test" in item["filename"].lower() for item in changed),
        "visual_admission": {
            "decision": "auto_admit_v3_strict_nontext_visual" if eligible else "not_auto_admitted",
            "admission_scope": "test_construction_and_measurement_only" if eligible else "none",
            "admission_route": admission_route,
            "selection_policy": ({
                "policy_id": "visual-v3-strict-nontext-v1",
                "required_v3_label": "非文字视觉信息候选不可替代",
                "requires_v3_human_review": False,
                "requires_complete_source_archive": True,
                "requires_separable_agent_safe_assets": True,
            } if classification_path is not None else {
                "policy_id": "legacy-migration-review-only-v1",
                "formal_admission_prohibited": True,
                "legacy_signal_would_have_admitted": legacy_eligible,
            }),
            "human_calibration_state": "pending",
            "human_calibration": {
                "gate": "multimodal_necessity",
                "state": "pending",
                "decision": None,
                "reviewer": None,
                "reason": None,
                "reviewed_at": None,
            },
            "model_evidence_is_not_human_confirmation": True,
            "visual_bucket": visual["decision"]["bucket"],
            "text_only_bucket": text_decision.get(
                "bucket", "unavailable_due_upstream_technical_failure"),
            "upstream_text_verifier": {
                "status": verifier.get("status"),
                "evidence_available": legacy_text_evidence_available,
                "technical_failure": (
                    verifier.get("status") == "failed" and not legacy_text_evidence_available),
                "error": verifier.get("error"),
            },
            "confidence": (None if classification_path is not None
                           else annotation.get("confidence")),
            "confidence_semantics": (
                "v3_classifier_has_no_confidence_field"
                if classification_path is not None
                else "legacy_verifier_metadata_only_not_formal_admission"),
            "reason": (("v3:" + str(capability_annotation.get(
                "strict_multimodal_admission") or capability.get("status")))
                       if classification_path is not None
                       else "legacy_migration:" + verifier["reconciliation"]["reason_code"]),
            "v3_classification": ({
                "status": capability.get("status"),
                "reason": capability.get("reason"),
                "strict_multimodal_admission": capability_annotation.get(
                    "strict_multimodal_admission"),
                "human_review_required": capability_annotation.get(
                    "human_review_required"),
            } if classification_record is not None else None),
            "raw_model_evidence": visual["result_path"],
        },
        "test_calibration": {
            "f2p_generation_state": "pending_generated_executable_tests",
            "p2p_generation_state": "pending_independent_inference",
            "measurement_state": "not_executed",
            "human_semantic_calibration_state": "pending",
            "human_semantic_calibration": {
                "gate": "f2p_p2p_semantic_validity",
                "state": "pending",
                "decision": None,
                "reviewer": None,
                "reason": None,
                "reviewed_at": None,
            },
            "independent_from_visual_calibration": True,
        },
        "benchmark_eligibility": {
            "current_stage": "executable_candidate" if eligible else "screening_review",
            "may_construct_and_measure_tests": eligible,
            "may_enter_final_taskset": False,
            "blocking_human_gates": [
                "multimodal_necessity",
                "f2p_p2p_semantic_validity",
            ] if eligible else [],
            "rule": "final admission requires both independent human gates to be approved",
        },
        "leakage_policy": {
            "risk": visual["annotation"]["quality"]["leakage_risk"],
            "separable": separable,
            "safe_agent_assets": safe_assets,
            "curator_only_asset_ids": [item["sha256"] for item in curator_only],
            "safe_agent_source_ids": [item["source_id"] for item in packet["problem_sources"]],
            "excluded_source_categories": packet["withheld"],
            "exclude_from_agent_problem": ["reference patch", "reference commit", "PR changelog/solution narrative", "PR/comment images"],
            "enforce_again_at_harbor_export": True,
        },
        "next_gate": "construct identical executable tests, measure baseline/reference, then obtain independent F2P/P2P semantic calibration",
    }


def write(verifier_path: Path, archive_path: Path, output: Path,
          classification_path: Path | None = None, *,
          allow_legacy_migration: bool = False) -> dict:
    result = build(verifier_path, archive_path, classification_path,
                   allow_legacy_migration=allow_legacy_migration)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result
