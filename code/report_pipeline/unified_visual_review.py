"""Merge a legacy V3 review bundle and a frozen V4 capability pool."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import shutil
import tempfile

from report_pipeline.atomic import write_json
from report_pipeline.category_audit import CATEGORY_LABELS, _qualification_from_source
from report_pipeline.paths import WORKSPACE_ROOT
from report_pipeline.pre_review_classification import classify_change_scale
from report_pipeline.v3_v4_conversion import _convert_annotation


INDEX_SCHEMA = "visual-review-unified-index-v1"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json_hash(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()


def _portable(path: Path) -> str:
    return Path(os.path.relpath(path.resolve(), WORKSPACE_ROOT.resolve())).as_posix()


def _path(value: str) -> Path:
    path = Path(value)
    return (path if path.is_absolute() else WORKSPACE_ROOT / path).resolve(strict=True)


def _binding(path: Path) -> dict:
    return {"path": _portable(path), "sha256": _sha(path)}


def _capability(value: dict, *, source_kind: str, source: dict,
                warnings: list[str] | None = None) -> dict:
    capabilities = value.get("visual_capabilities") or []
    if not capabilities or not any(item.get("importance") == "core"
                                   for item in capabilities):
        raise ValueError("V4 capability suggestion lacks a core capability")
    return {
        "schema_version": "visual-capability-classifier-v4",
        "source_kind": source_kind,
        "status": "complete",
        "visual_capabilities": capabilities,
        "multi_label": len(capabilities) > 1,
        "source": source,
        "warnings": warnings or [],
    }


def _from_v3(case: dict) -> dict:
    legacy = case.get("v3") or {}
    annotation = {
        "schema_version": "visual-capability-classifier-v3",
        "strict_multimodal_admission": legacy.get("strict_multimodal_admission"),
        "human_review_required": False,
        "atomic_visual_constraints": legacy.get("atomic_visual_constraints") or [],
    }
    status, capabilities, reasons = _convert_annotation(annotation)
    if status != "converted":
        raise ValueError(f"{case['case_id']}: V3 cannot be deterministically mapped: "
                         + "; ".join(reasons))
    source = {
        "classification": case["source_bindings"]["classification"],
        "classification_sha256": case["source_bindings"]["classification_sha256"],
    }
    return _capability(
        {"visual_capabilities": capabilities},
        source_kind="deterministic_v3_conversion", source=source)


def _load_pool(pool_run: Path) -> tuple[dict, Path]:
    manifest_path = pool_run / "16_11_07_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if (manifest.get("schema_version") != "capability-candidate-pool-manifest-v2"
            or manifest.get("data") != "16_11_05_candidate_pool.json"):
        raise ValueError("capability pool manifest is invalid")
    data_path = pool_run / manifest["data"]
    if _sha(data_path) != manifest.get("data_sha256"):
        raise ValueError("capability pool data changed")
    value = json.loads(data_path.read_text())
    rows = value.get("records") or []
    if (value.get("schema_version") != "capability-candidate-pool-v2"
            or len({row.get("case_id") for row in rows}) != len(rows)):
        raise ValueError("capability pool inventory is invalid")
    return value, manifest_path


def _pool_capability(row: dict, manifest_path: Path) -> dict:
    return _capability(
        {"visual_capabilities": row["visual_capabilities"]},
        source_kind=("deterministic_v3_conversion" if row["migrated_from_v3"]
                     else "native_v4_verifier"),
        source={
            "pool_manifest": _portable(manifest_path),
            "pool_manifest_sha256": _sha(manifest_path),
            "classification": _portable(_path(row["classification"])),
            "classification_sha256": row["classification_sha256"],
            "packet": _portable(_path(row["packet"])),
            "packet_sha256": row["packet_sha256"],
        },
        warnings=row.get("warnings") or [],
    )


def _v3_case(row: dict, staging: Path, index: int, translations: dict) -> dict:
    from report_pipeline.visual_gate_ui import _case_payload

    classification_path = _path(row["classification"])
    if _sha(classification_path) != row["classification_sha256"]:
        raise ValueError(f"{row['case_id']}: V3 classification changed")
    manifest = json.loads(classification_path.read_text())
    source_run = _path(manifest["source_run"])
    matches = [(position, record) for position, record in enumerate(
        manifest.get("records") or [], 1) if record.get("case_id") == row["case_id"]]
    if len(matches) != 1:
        raise ValueError(f"{row['case_id']}: V3 record is missing or duplicated")
    position, record = matches[0]
    qualification = _qualification_from_source(
        record, source_run / f"16_03_result_{position:04d}.json")
    if qualification.get("qualified") is not True:
        raise ValueError(f"{row['case_id']}: V3 source qualification failed")
    qualification = {
        **qualification,
        "classification": _portable(classification_path),
        "classification_sha256": _sha(classification_path),
    }
    annotation = (record.get("visual_capability") or {}).get("annotation") or {}
    distribution_row = {
        "case_id": row["case_id"],
        "source_qualification": qualification,
        "primary_visual_category": annotation.get("primary_visual_category"),
        "category_purity": annotation.get("category_purity"),
        "evidence_mode": annotation.get("evidence_mode"),
        "strict_multimodal_admission": annotation.get("strict_multimodal_admission"),
        "admission_reason": annotation.get("admission_reason"),
        "classification_reason": annotation.get("classification_reason"),
        "contributing_visual_categories": annotation.get(
            "contributing_visual_categories") or [],
    }
    case, _ = _case_payload(
        distribution_row, record, staging, index, classification_path, translations)
    return case


def _role_suggestion(image: dict) -> dict:
    relationship = image.get("task_relationship")
    return {
        "source_schema": "pr-image-role-leakage-v1",
        "solver_visible_role": image.get("role") or "unclear",
        "seed_temporal_role": image.get("role") or "unclear",
        "ocr_transcription_sufficient": "当前输入不足，无法判断",
        "task_relevance": ({"explicit": "相关", "weak": "无关"}.get(
            relationship, "当前输入不足，无法判断")),
        "observation": image.get("role_evidence") or image.get("reason") or "",
        "contains_fixed_after": image.get("contains_fixed_after"),
        "contains_solution_evidence": image.get("contains_solution_evidence"),
    }


def _media_extension(media_type: str) -> str:
    fixed = {"video/quicktime": ".mov", "video/mp4": ".mp4"}
    return fixed.get(media_type) or mimetypes.guess_extension(media_type) or ".bin"


def _native_v4_case(row: dict, staging: Path, index: int,
                    pool_manifest: Path) -> dict:
    run = _path(row["classification"])
    result_path = run / "16_11_03_capability_results.json"
    if _sha(result_path) != row["classification_sha256"]:
        raise ValueError(f"{row['case_id']}: native V4 result changed")
    result = json.loads(result_path.read_text())
    matches = [item for item in result.get("records") or []
               if item.get("case_id") == row["case_id"]]
    if len(matches) != 1 or matches[0].get("status") != "complete":
        raise ValueError(f"{row['case_id']}: native V4 record is incomplete")
    record = matches[0]
    role_run = _path(record["role_run"])
    role_result_path = role_run / "08_04_03_results.json"
    if _sha(role_result_path) != record["role_results_sha256"]:
        raise ValueError(f"{row['case_id']}: image-role result changed")
    role_result = json.loads(role_result_path.read_text())
    role_matches = [item for item in role_result.get("records") or []
                    if item.get("case_id") == row["case_id"]]
    if len(role_matches) != 1 or role_matches[0].get("status") != "complete":
        raise ValueError(f"{row['case_id']}: image-role record is incomplete")
    role_record = role_matches[0]
    role_packet_path = _path(role_record["packet"])
    if _sha(role_packet_path) != role_record["packet_sha256"]:
        raise ValueError(f"{row['case_id']}: image-role packet changed")
    role_packet = json.loads(role_packet_path.read_text())
    archive_path = _path(record["source_archive"])
    if (_sha(archive_path) != record["source_archive_sha256"]
            or _sha(archive_path) != row["archive"]["sha256"]):
        raise ValueError(f"{row['case_id']}: source archive changed")
    archive = json.loads(archive_path.read_text())
    selected_roles = {item["asset_id"]: item
                      for item in role_record["annotation"]["images"]}
    case_directory = staging / "16_04_02_assets" / f"case_{index:04d}"
    case_directory.mkdir(parents=True)
    assets = []
    for asset_index, asset in enumerate(row["assets"], 1):
        source = _path(asset["path"])
        if source.is_symlink() or _sha(source) != asset["sha256"]:
            raise ValueError(f"{row['case_id']}: solver-visible asset changed")
        destination = case_directory / (
            f"asset_{asset_index:02d}_{asset['asset_id'][:12]}"
            + _media_extension(asset["media_type"]))
        shutil.copy2(source, destination)
        role = selected_roles.get(asset["asset_id"])
        if role is None:
            raise ValueError(f"{row['case_id']}: selected asset lacks role evidence")
        assets.append({
            "asset_id": asset["asset_id"],
            "path": destination.relative_to(staging).as_posix(),
            "sha256": asset["sha256"],
            "media_type": asset["media_type"],
            "source_ids": asset.get("source_ids") or [],
            "gate_suggestion": _role_suggestion(role),
        })

    bound_source_ids = {item["source_id"] for item in record.get("source_bindings") or []}
    problem_sources = []
    for document in role_packet.get("source_documents") or []:
        if document.get("source_id") not in bound_source_ids:
            continue
        problem_sources.append({
            "source_id": document["source_id"],
            "kind": "issue", "field": document["source_id"].rsplit(":", 1)[-1],
            "text": document.get("text") or "", "issue_url": document.get("url"),
        })
    pull = archive["sections"]["pull_request"]["data"]
    packet_path = _path(row["packet"])
    packet = json.loads(packet_path.read_text())
    source_bindings = {
        "classification": _portable(result_path),
        "classification_sha256": _sha(result_path),
        "classification_packet": _portable(packet_path),
        "classification_packet_sha256": _sha(packet_path),
        "image_role_result": _portable(role_result_path),
        "image_role_result_sha256": _sha(role_result_path),
        "image_role_packet": _portable(role_packet_path),
        "image_role_packet_sha256": _sha(role_packet_path),
        "source_archive": _portable(archive_path),
        "source_archive_sha256": _sha(archive_path),
    }
    capabilities = row["visual_capabilities"]
    case = {
        "case_id": row["case_id"], "position": index,
        "repository": pull["base"]["repo"]["full_name"],
        "pr_number": pull["number"], "pr_url": pull["html_url"],
        "pr_title": pull.get("title"), "pr_body_curator_only": pull.get("body") or "",
        "source_route": "issue_derived", "problem_statement": packet["problem_statement"],
        "problem_statement_sha256": hashlib.sha256(
            packet["problem_statement"].encode()).hexdigest(),
        "pr_title_zh": "", "problem_statement_zh": "",
        "translation": {"status": "missing", "curator_only": True},
        "problem_sources": problem_sources,
        "change_scale": classify_change_scale(archive["sections"]["files"]["items"]),
        "category": None,
        "category_purity": ("多标签能力题" if len(capabilities) > 1 else "单一能力题"),
        "evidence_mode": ("GIF、视频或交互时序" if any(
            asset["media_type"].startswith("video/") for asset in assets)
                          else "视觉资产"),
        "v3": {"status": "not_available_for_native_v4"},
        "v4": _pool_capability(row, pool_manifest),
        "assets": assets, "source_bindings": source_bindings,
    }
    case["translation"]["source_text_sha256"] = hashlib.sha256((
        case["case_id"] + "\0" + (case["pr_title"] or "") + "\0"
        + case["problem_statement"]).encode()).hexdigest()
    case["candidate_binding_sha256"] = _json_hash({
        "case_id": case["case_id"], "source_route": case["source_route"],
        "problem_statement_sha256": case["problem_statement_sha256"],
        "assets": [{"asset_id": item["asset_id"], "source_ids": item["source_ids"]}
                   for item in assets],
        "change_scale": case["change_scale"], "source_bindings": source_bindings,
    })
    return case


def write_index(base_bundle: Path, pool_run: Path, output: Path) -> dict:
    from report_pipeline.visual_gate_ui import audit

    base_bundle, pool_run = base_bundle.resolve(strict=True), pool_run.resolve(strict=True)
    audit(base_bundle)
    _, pool_manifest = _load_pool(pool_run)
    value = {
        "schema_version": INDEX_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "base_bundle": _binding(base_bundle / "16_04_04_review_manifest.json"),
        "capability_pool": _binding(pool_manifest),
        "translations": [],
        "gate_passed": False,
    }
    write_json(output, value)
    return value


def render_index(index_path: Path, output: Path) -> dict:
    from report_pipeline.visual_gate_ui import (
        RUNNER_VERSION, SCHEMA, _page, _validate_manifest, audit,
    )

    index_path, output = index_path.resolve(strict=True), output.resolve()
    index = json.loads(index_path.read_text())
    if index.get("schema_version") != INDEX_SCHEMA:
        raise ValueError("unified visual-review index is invalid")
    base_manifest_path = _path(index["base_bundle"]["path"])
    pool_manifest_path = _path(index["capability_pool"]["path"])
    if (_sha(base_manifest_path) != index["base_bundle"]["sha256"]
            or _sha(pool_manifest_path) != index["capability_pool"]["sha256"]):
        raise ValueError("unified visual-review source binding changed")
    base_bundle = base_manifest_path.parent
    _, base_payload = _validate_manifest(base_bundle)
    pool_run = pool_manifest_path.parent
    pool, checked_pool_manifest = _load_pool(pool_run)
    if checked_pool_manifest != pool_manifest_path:
        raise ValueError("unified capability pool path changed")

    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".16_14_unified_review_", dir=output.parent))
    try:
        shutil.copy2(Path(__import__(
            "report_pipeline.visual_gate_ui", fromlist=["x"]).__file__),
                     staging / "16_04_00_visual_gate_ui.py")
        shutil.copy2(SCHEMA, staging / "16_04_00_visual_gate_review.schema.json")
        shutil.copytree(base_bundle / "16_04_02_assets", staging / "16_04_02_assets")
        cases = deepcopy(base_payload["cases"])
        by_case = {case["case_id"]: case for case in cases}
        for case in cases:
            for asset in case["assets"]:
                asset.setdefault("media_type", mimetypes.guess_type(asset["path"])[0]
                                 or "application/octet-stream")
                asset.setdefault("gate_suggestion", asset.get("v3_suggestion") or {})
            case["v4"] = _from_v3(case)

        pool_rows = {row["case_id"]: row for row in pool["records"]}
        for case_id, case in by_case.items():
            if case_id in pool_rows:
                case["v4"] = _pool_capability(pool_rows[case_id], pool_manifest_path)
        translations = {}
        next_index = len(cases) + 1
        for row in pool["records"]:
            if row["case_id"] in by_case:
                continue
            if row["classification_version"] == "visual-capability-classifier-v3":
                case = _v3_case(row, staging, next_index, translations)
                case["v4"] = _pool_capability(row, pool_manifest_path)
            else:
                case = _native_v4_case(row, staging, next_index, pool_manifest_path)
            cases.append(case)
            by_case[case["case_id"]] = case
            next_index += 1

        for position, case in enumerate(cases, 1):
            case["position"] = position
        assets = [{"case_id": case["case_id"], "asset_id": asset["asset_id"],
                   "path": asset["path"], "sha256": asset["sha256"]}
                  for case in cases for asset in case["assets"]]
        distribution_binding = _binding(index_path)
        payload = deepcopy(base_payload)
        payload.update(
            schema_version="visual-gate-review-payload-v2",
            distribution=distribution_binding,
            translations=[], cases=cases,
        )
        source_manifest_sha256 = _json_hash({
            "distribution": distribution_binding, "translations": [],
            "cases": [{"case_id": case["case_id"],
                       "candidate_binding_sha256": case["candidate_binding_sha256"]}
                      for case in cases],
            "assets": assets,
        })
        payload["source_manifest_sha256"] = source_manifest_sha256
        write_json(staging / "16_04_01_review_payload.json", payload)
        (staging / "16_04_03_visual_gate_review.html").write_text(
            _page(payload, source_manifest_sha256), encoding="utf-8")
        counts = {}
        for category in CATEGORY_LABELS:
            counts[CATEGORY_LABELS[category]] = sum(any(
                item["category"] == category for item in case["v4"]["visual_capabilities"])
                for case in cases)
        manifest = {
            "schema_version": RUNNER_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "ready_for_visual_human_review",
            "source_manifest_sha256": source_manifest_sha256,
            "distribution": distribution_binding, "translations": [],
            "runner_sha256": _sha(staging / "16_04_00_visual_gate_ui.py"),
            "schema_sha256": _sha(staging / "16_04_00_visual_gate_review.schema.json"),
            "payload_sha256": _sha(staging / "16_04_01_review_payload.json"),
            "html_sha256": _sha(staging / "16_04_03_visual_gate_review.html"),
            "candidate_count": len(cases),
            "case_ids": [case["case_id"] for case in cases],
            "category_counts": counts,
            "multi_label_count": sum(case["v4"]["multi_label"] for case in cases),
            "assets": assets, "boundary": payload["boundary"],
        }
        write_json(staging / "16_04_04_review_manifest.json", manifest)
        audit(staging)
        os.replace(staging, output)
        return {**manifest, "output": str(output), "audit": audit(output)}
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def migrate_decisions(base_bundle: Path, new_bundle: Path, state_root: Path,
                      audit_path: Path) -> dict:
    from report_pipeline.visual_gate_ui import _validate_manifest, validate_human_export

    old_manifest, old_payload = _validate_manifest(base_bundle.resolve(strict=True))
    new_manifest, new_payload = _validate_manifest(new_bundle.resolve(strict=True))
    decisions_root = state_root.resolve() / "16_04_06_human_decisions"
    old_cases = {case["case_id"]: case for case in old_payload["cases"]}
    new_cases = {case["case_id"]: case for case in new_payload["cases"]}
    latest = {}
    for path in sorted(decisions_root.glob("16_04_06_decisions_*.json"), reverse=True):
        value = json.loads(path.read_text())
        if value.get("source_manifest_sha256") != old_manifest["source_manifest_sha256"]:
            continue
        validate_human_export(base_bundle, path)
        for row in value.get("rows") or []:
            latest.setdefault(row["case_id"], (row, path))
    migrated = []
    evidence = []
    for case_id, (row, path) in latest.items():
        old_case, new_case = old_cases.get(case_id), new_cases.get(case_id)
        if (old_case and new_case and old_case["candidate_binding_sha256"]
                == new_case["candidate_binding_sha256"] == row["candidate_binding_sha256"]):
            migrated.append(row)
            evidence.append({"case_id": case_id, "source": _binding(path)})
    export = {
        "schema_version": "visual-gate-human-export-v1",
        "source_manifest_sha256": new_manifest["source_manifest_sha256"],
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "rows": sorted(migrated, key=lambda item: item["case_id"]),
    }
    decisions_root.mkdir(exist_ok=True)
    digest = _json_hash(export)
    destination = decisions_root / (
        datetime.now(timezone.utc).strftime("16_04_06_decisions_%Y%m%dT%H%M%S%fZ_")
        + digest[:12] + ".json")
    write_json(destination, export)
    validation = validate_human_export(new_bundle, destination)
    result = {
        "schema_version": "visual-review-decision-migration-v1",
        "status": "complete", "migrated_count": len(migrated),
        "new_candidate_count": len(new_cases), "destination": _binding(destination),
        "evidence": evidence, "validation": validation,
    }
    write_json(audit_path, result)
    return result


def build(base_bundle: Path, pool_run: Path, output: Path,
          state_root: Path | None = None) -> dict:
    output = output.resolve()
    if output.exists():
        raise ValueError(f"unified visual-review output already exists: {output}")
    output.mkdir(parents=True)
    index_path = output / "16_14_01_unified_index.json"
    write_index(base_bundle, pool_run, index_path)
    bundle = output / "16_14_02_visual_gate_review"
    manifest = render_index(index_path, bundle)
    migration = None
    if state_root is not None:
        migration = migrate_decisions(
            base_bundle, bundle, state_root,
            output / "16_14_03_decision_migration.json")
    result = {
        "schema_version": "unified-visual-review-build-v1",
        "status": "complete", "index": _binding(index_path),
        "bundle": _binding(bundle / "16_04_04_review_manifest.json"),
        "candidate_count": manifest["candidate_count"],
        "category_counts": manifest["category_counts"],
        "multi_label_count": manifest["multi_label_count"],
        "decision_migration": migration,
    }
    write_json(output / "16_14_04_build_audit.json", result)
    return result
