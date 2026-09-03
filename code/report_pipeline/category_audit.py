"""Produce the four-capability, multi-label screening-pool audit."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
from collections import Counter
from pathlib import Path

from report_pipeline.paths import WORKSPACE_ROOT
from report_pipeline.pre_review_classification import validate_classification_run


CATEGORIES = (
    "rendering_appearance_understanding",
    "spatial_layout_understanding",
    "element_state_understanding",
    "interaction_temporal_understanding",
)
COUNTED_CATEGORIES = CATEGORIES
CATEGORY_LABELS = {
    "rendering_appearance_understanding": "渲染外观理解",
    "spatial_layout_understanding": "空间布局理解",
    "element_state_understanding": "元素与状态理解",
    "interaction_temporal_understanding": "交互与时序理解",
}
LEGACY_CATEGORY_MAP = {
    "外观与渲染属性理解": "rendering_appearance_understanding",
    "空间布局与几何理解": "spatial_layout_understanding",
    "元素结构与视觉状态理解": "element_state_understanding",
    "动态交互与时序理解": "interaction_temporal_understanding",
}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _portable(path: Path) -> str:
    try:
        return path.resolve().relative_to(WORKSPACE_ROOT.resolve()).as_posix()
    except ValueError:
        raise ValueError(f"category audit input is outside workspace: {path}") from None


def _load_exclusions(path: Path | None) -> tuple[dict[str, list[str]], dict | None]:
    if path is None:
        return {}, None
    value = json.loads(path.read_text())
    if value.get("schema_version") != "visual-category-exclusions-v1":
        raise ValueError("category exclusions schema is invalid")
    exclusions = value.get("exclusions")
    if not isinstance(exclusions, dict) or any(
            not isinstance(case_id, str) or not isinstance(reasons, list) or not reasons
            or any(not isinstance(reason, str) or not reason.strip() for reason in reasons)
            for case_id, reasons in exclusions.items()):
        raise ValueError("category exclusions are invalid")
    return exclusions, {"path": _portable(path), "sha256": _sha(path)}


def _qualification_from_source(record: dict, source_result_path: Path) -> dict:
    reasons = []
    source_result = json.loads(source_result_path.read_text())
    if (record.get("source_result_sha256") != _sha(source_result_path)
            or source_result.get("case_id") != record.get("case_id")):
        reasons.append("source_result_binding_invalid")
    upstream_status = source_result.get("status")
    if upstream_status not in {"prepared", "complete", "failed"}:
        reasons.append(f"source_material_status:{upstream_status}")
    packet_path = Path(source_result.get("packet", ""))
    if (not packet_path.is_file()
            or source_result.get("packet_sha256") != _sha(packet_path)):
        reasons.append("source_packet_binding_invalid")
        return {"qualified": False, "reasons": reasons}
    packet = json.loads(packet_path.read_text())
    provenance = packet.get("provenance") or {}
    archive_path = Path(provenance.get("source_archive", ""))
    if (not archive_path.is_file()
            or provenance.get("source_archive_sha256") != _sha(archive_path)
            or record.get("source_archive_sha256") != _sha(archive_path)):
        reasons.append("source_archive_binding_invalid")
        return {"qualified": False, "reasons": reasons}
    archive = json.loads(archive_path.read_text())
    archive_status = archive.get("status")
    if archive_status not in {"complete", "partial"}:
        reasons.append(f"source_archive_status:{archive_status}")
    required_sections = {
        "pull_request", "comments", "reviews", "review_comments", "commits",
        "files", "diff", "patch", "closing_issues",
        "timeline", "merge_commit", "merge_anchor_evidence",
    }
    sections = archive.get("sections") or {}
    incomplete = sorted(name for name in required_sections
                        if (sections.get(name) or {}).get("status") != "complete")
    if incomplete:
        reasons.append("source_sections_incomplete:" + ",".join(incomplete))
    problem_sources = packet.get("problem_sources") or []
    if not problem_sources:
        reasons.append("solver_visible_problem_source_missing")
    linked_items = (sections.get("linked_issues") or {}).get("items") or []
    bound_issue_keys = set()
    for source in problem_sources:
        match = re.fullmatch(r"(.+)#([1-9][0-9]*):(title|body)",
                             str(source.get("source_id", "")))
        if (not match or source.get("kind") != "issue"
                or source.get("field") != match.group(3)):
            reasons.append("solver_visible_problem_source_identity_invalid")
            continue
        repo, number, field = match.group(1).lower(), int(match.group(2)), match.group(3)
        matches = [item for item in linked_items
                   if str(item.get("repo", "")).lower() == repo
                   and item.get("number") == number]
        if len(matches) != 1:
            reasons.append(f"solver_visible_problem_source_missing_or_duplicated:{repo}#{number}")
            continue
        detail = matches[0].get("detail") or {}
        data = detail.get("data") or {}
        raw_text = data.get(field)
        if (detail.get("status") != "complete" or not isinstance(raw_text, str)
                or hashlib.sha256(raw_text.encode()).hexdigest()
                != source.get("original_text_sha256")
                or hashlib.sha256(str(source.get("text", "")).encode()).hexdigest()
                != source.get("text_sha256")):
            reasons.append(f"solver_visible_problem_source_binding_invalid:{repo}#{number}:{field}")
            continue
        bound_issue_keys.add((repo, number))
    classification_packet_path = Path(record.get("packet", ""))
    required_asset_ids = []
    if (not classification_packet_path.is_file()
            or record.get("packet_sha256") != _sha(classification_packet_path)):
        reasons.append("classification_packet_binding_invalid")
    else:
        classification_packet = json.loads(classification_packet_path.read_text())
        required_asset_ids = [item.get("asset_id")
                              for item in classification_packet.get("assets", [])]
        if (not required_asset_ids or any(not isinstance(item, str) or not item
                                          for item in required_asset_ids)
                or len(required_asset_ids) != len(set(required_asset_ids))):
            reasons.append("solver_visible_asset_binding_missing_or_duplicated")
    archived_assets = (sections.get("assets") or {}).get("items") or []
    for asset_id in required_asset_ids:
        matches = [item for item in archived_assets if item.get("sha256") == asset_id]
        if len(matches) != 1 or matches[0].get("status") != "complete":
            reasons.append(f"solver_visible_asset_incomplete:{asset_id}")
            continue
        relative = Path(matches[0].get("local_path", ""))
        logical_root = archive_path.parent / "11_http_archive"
        try:
            asset_path = (logical_root / relative).resolve(strict=True)
            root = logical_root.resolve(strict=True)
        except OSError:
            reasons.append(f"solver_visible_asset_file_missing:{asset_id}")
            continue
        if (relative.is_absolute() or ".." in relative.parts or logical_root.is_symlink()
                or not asset_path.is_relative_to(root) or not asset_path.is_file()
                or _sha(asset_path) != asset_id):
            reasons.append(f"solver_visible_asset_file_binding_invalid:{asset_id}")
    withheld = set(packet.get("withheld") or [])
    required_withheld = {"pull_request_prose", "comments", "reviews", "commits",
                         "diff", "patch", "tests", "reference_code"}
    missing_withheld = sorted(required_withheld - withheld)
    if missing_withheld:
        reasons.append("leakage_withheld_contract_missing:" + ",".join(missing_withheld))
    if any(key in packet for key in (
            "reference_patch", "gold_patch", "solution", "test_patch", "patch", "diff")):
        reasons.append("leakage_forbidden_solution_field_present")
    return {"qualified": not reasons, "reasons": reasons,
            "upstream_verifier_status": upstream_status,
            "upstream_verifier_error": source_result.get("error"),
            "source_archive_status": archive_status,
            "consistency_status": (sections.get("consistency") or {}).get("status"),
            "solver_visible_asset_count": len(required_asset_ids),
            "solver_visible_problem_source_count": len(problem_sources),
            "unbound_source_failure_count": sum(
                ((str(item.get("repo", "")).lower(), item.get("number"))
                 not in bound_issue_keys)
                and any((item.get(name) or {}).get("status") != "complete"
                        for name in ("detail", "comments", "labels", "timeline"))
                for item in linked_items),
            "unbound_asset_failure_count": sum(
                item.get("status") != "complete" and item.get("sha256") not in required_asset_ids
                for item in archived_assets),
            "source_result": _portable(source_result_path),
            "source_result_sha256": _sha(source_result_path),
            "source_packet": _portable(packet_path),
            "source_packet_sha256": _sha(packet_path),
            "classification_packet": (_portable(classification_packet_path)
                                      if classification_packet_path.is_file() else None),
            "classification_packet_sha256": (_sha(classification_packet_path)
                                             if classification_packet_path.is_file() else None),
            "source_archive": _portable(archive_path),
            "source_archive_sha256": _sha(archive_path)}


def summarize(records: list[dict], exclusions: dict[str, list[str]],
              qualifications: dict[str, dict] | None = None) -> dict:
    rows = []
    counts: Counter[str] = Counter()
    for record in records:
        capability = record.get("visual_capability") or {}
        annotation = capability.get("annotation") or {}
        case_id = record.get("case_id")
        reasons = list(exclusions.get(case_id, []))
        qualification = (qualifications or {}).get(case_id)
        if not isinstance(qualification, dict):
            reasons.append("qualification_evidence_missing")
        else:
            reasons.extend(qualification.get("reasons") or [])
            if qualification.get("qualified") is not True and not qualification.get("reasons"):
                reasons.append("qualification_failed_without_reason")
        if capability.get("status") != "complete":
            reasons.append(f"classification_status:{capability.get('status')}")
        annotation_version = annotation.get("schema_version")
        migrated_from_v3 = annotation_version == "visual-capability-classifier-v3"
        if annotation_version == "visual-capability-classifier-v4":
            capabilities = annotation.get("visual_capabilities") or []
        elif migrated_from_v3:
            capabilities = []
            seen = set()
            if annotation.get("strict_multimodal_admission") != "非文字视觉信息候选不可替代":
                reasons.append("legacy_v3_not_strict_nontext_visual")
            if annotation.get("human_review_required") is not False:
                reasons.append("legacy_v3_human_review_required")
            for constraint in annotation.get("atomic_visual_constraints") or []:
                category = LEGACY_CATEGORY_MAP.get(constraint.get("visual_category"))
                if category and category not in seen:
                    seen.add(category)
                    capabilities.append({
                        "category": category,
                        "importance": ("core" if constraint.get("decision_critical") == "是"
                                       else "supporting"),
                        "visual_evidence": constraint.get("direct_visual_evidence") or "legacy V3 evidence",
                        "task_relation": constraint.get("description") or "legacy V3 constraint",
                    })
            if not capabilities:
                reasons.append("legacy_v3_has_no_mappable_v4_capability")
        else:
            capabilities = []
            reasons.append("unsupported_capability_annotation_version")
        categories = [item.get("category") for item in capabilities]
        if (not categories or len(categories) != len(set(categories))
                or any(category not in COUNTED_CATEGORIES for category in categories)):
            reasons.append("missing_duplicated_or_invalid_visual_capability")
        if capabilities and not any(item.get("importance") == "core" for item in capabilities):
            reasons.append("missing_core_visual_capability")
        counted = not reasons
        if counted:
            for category in categories:
                counts[category] += 1
        rows.append({
            "case_id": case_id,
            "classification_status": capability.get("status"),
            "annotation_version": annotation_version,
            "migrated_from_v3": migrated_from_v3,
            "visual_capabilities": capabilities,
            "capability_categories": categories,
            "source_qualification": qualification,
            "human_visual_gate": "pending",
            "counted": counted,
            "exclusion_reasons": reasons,
        })
    distribution = [{"category": category, "count": counts[category],
                     "required": 5, "deficit": max(0, 5 - counts[category])}
                    for category in COUNTED_CATEGORIES]
    return {
        "distribution": distribution,
        "qualified_count": sum(row["counted"] for row in rows),
        "capability_membership_count": sum(counts.values()),
        "multi_label_count": sum(row["counted"] and len(row["capability_categories"]) > 1
                                 for row in rows),
        "gate_passed": all(item["count"] >= 5 for item in distribution),
        "rows": rows,
    }


def _classification_runs(classifications: Path | list[Path]) -> list[tuple[Path, Path, dict]]:
    paths = [classifications] if isinstance(classifications, Path) else classifications
    if not paths:
        raise ValueError("at least one classification run is required")
    runs = []
    seen_cases = set()
    for value in paths:
        classification = value.resolve()
        manifest = json.loads(classification.read_text())
        source_run = Path(manifest.get("source_run", "")).resolve()
        validate_classification_run(source_run, classification)
        case_ids = [item.get("case_id") for item in manifest.get("records", [])]
        duplicate = next((case_id for case_id in case_ids if case_id in seen_cases), None)
        if duplicate is not None or len(case_ids) != len(set(case_ids)):
            raise ValueError(f"classification case appears more than once: {duplicate or 'within_run'}")
        seen_cases.update(case_ids)
        runs.append((classification, source_run, manifest))
    return runs


def run(classification: Path | list[Path], output: Path,
        exclusions_path: Path | None = None) -> dict:
    runs = _classification_runs(classification)
    exclusions, exclusion_binding = _load_exclusions(
        exclusions_path.resolve() if exclusions_path else None
    )
    records = [item for _, _, manifest in runs for item in manifest["records"]]
    unknown = sorted(set(exclusions) - {item.get("case_id") for item in records})
    if unknown:
        raise ValueError(f"category exclusions contain unknown case: {unknown[0]}")
    qualifications = {}
    for classification_path, source_run, manifest in runs:
        for index, item in enumerate(manifest["records"], 1):
            qualification = _qualification_from_source(
                item, source_run / f"16_03_result_{index:04d}.json")
            qualification["classification"] = _portable(classification_path)
            qualification["classification_sha256"] = _sha(classification_path)
            qualifications[item["case_id"]] = qualification
    summary = summarize(records, exclusions, qualifications)
    record = {
        "schema_version": "visual-capability-distribution-v4",
        "classifications": [
            {"path": _portable(classification_path),
             "sha256": _sha(classification_path),
             "source_run": _portable(source_run)}
            for classification_path, source_run, _ in runs
        ],
        "exclusions": exclusion_binding,
        **summary,
    }
    if output.exists():
        raise ValueError(f"category audit output exists: {output}")
    output.mkdir(parents=True)
    (output / "16_03_09_02_category_distribution.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n"
    )
    cards = "".join(
        f'<div class="card"><b>{html.escape(CATEGORY_LABELS[item["category"]])}</b>'
        f'<span>{item["count"]}/5</span><small>缺口 {item["deficit"]}</small></div>'
        for item in record["distribution"]
    )
    def link(path: str | None, label: str) -> str:
        if not path:
            return "—"
        href = os.path.relpath(WORKSPACE_ROOT / path, output)
        return f'<a href="{html.escape(href, quote=True)}">{html.escape(label)}</a>'

    rendered_rows = []
    for item in record["rows"]:
        qualification = item.get("source_qualification") or {}
        result = "保留" if item["counted"] else "筛除"
        evidence = " · ".join((
            link(qualification.get("classification_packet"), "VLM输入"),
            link(qualification.get("source_packet"), "Issue输入"),
            link(qualification.get("source_archive"), "来源归档"),
            link(qualification.get("classification"), "模型运行"),
        ))
        fields = (
            item["case_id"], "、".join(
                CATEGORY_LABELS.get(value, value)
                for value in item["capability_categories"]) or "—",
            "V3迁移" if item["migrated_from_v3"] else "V4原生",
            "待多模态必要性与防泄漏审核", result,
            "；".join(capability["task_relation"]
                      for capability in item["visual_capabilities"]) or "—",
            "; ".join(item["exclusion_reasons"]) or "—",
        )
        cells = "".join(f"<td>{html.escape(str(value))}</td>" for value in fields)
        rendered_rows.append(
            f'<tr data-result="{result}" data-categories="{html.escape(" ".join(item["capability_categories"]), quote=True)}">'
            + cells + f"<td>{evidence}</td></tr>")
    rows = "".join(rendered_rows)
    document = f'''<!doctype html><meta charset="utf-8"><title>视觉能力分布审计</title>
<style>body{{font:13px system-ui;margin:16px;color:#1d1d1f}}.grid{{display:grid;grid-template-columns:repeat(3,minmax(150px,1fr));gap:6px}}.card{{border:1px solid #ddd;border-radius:7px;padding:8px;display:flex;flex-direction:column;gap:3px}}.card span{{font-size:22px}}.tools{{display:flex;gap:8px;margin:10px 0;position:sticky;top:0;background:#fff;padding:6px 0}}select{{padding:5px}}table{{width:100%;border-collapse:collapse;table-layout:fixed}}th,td{{padding:5px;border-bottom:1px solid #ddd;text-align:left;vertical-align:top;overflow-wrap:anywhere}}th{{position:sticky;top:44px;background:#fff}}th:nth-child(1){{width:12%}}th:nth-child(2){{width:11%}}th:nth-child(7){{width:25%}}th:nth-child(8){{width:17%}}small{{color:#666}}a{{white-space:nowrap}}</style>
<h1>四类视觉能力候选分布</h1><p>Gate: <b>{'PASS' if record['gate_passed'] else 'NOT MET'}</b> · unique PRs={record['qualified_count']} · multi-label={record['multi_label_count']}</p>
<div class="grid">{cards}</div><div class="tools"><select id="result"><option value="">全部结果</option><option>保留</option><option>筛除</option></select><select id="category"><option value="">全部能力</option>{''.join(f'<option value="{html.escape(category)}">{html.escape(CATEGORY_LABELS[category])}</option>' for category in COUNTED_CATEGORIES)}</select><span>每个能力池按唯一 PR 计数；多标签 PR 可分别进入多个池。</span></div><table><thead><tr><th>PR</th><th>视觉能力</th><th>来源</th><th>人工审核</th><th>结果</th><th>任务关系</th><th>筛除理由</th><th>证据</th></tr></thead><tbody>{rows}</tbody></table><script>const r=document.querySelector('#result'),c=document.querySelector('#category');function f(){{for(const x of document.querySelectorAll('tbody tr'))x.hidden=(r.value&&x.dataset.result!==r.value)||(c.value&&!x.dataset.categories.split(' ').includes(c.value))}}r.onchange=c.onchange=f;</script>'''
    (output / "16_03_09_03_category_distribution.html").write_text(document + "\n")
    return record
