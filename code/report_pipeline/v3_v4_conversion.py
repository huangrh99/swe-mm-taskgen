"""Deterministically convert frozen V3 capability evidence to V4 labels."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path

from report_pipeline.atomic import write_json
from report_pipeline.category_audit import (
    CATEGORIES, CATEGORY_LABELS, LEGACY_CATEGORY_MAP, _qualification_from_source,
)
from report_pipeline.pre_review_classification import validate_classification_run


DOMAIN_CATEGORY = "图形符号与领域语义理解"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _convert_annotation(annotation: dict) -> tuple[str, list[dict], list[str]]:
    if annotation.get("schema_version") != "visual-capability-classifier-v3":
        return "excluded_not_v3", [], ["annotation is not V3"]
    if annotation.get("strict_multimodal_admission") != "非文字视觉信息候选不可替代":
        return "excluded_not_strict_visual", [], [
            f'admission={annotation.get("strict_multimodal_admission")}']
    if annotation.get("human_review_required") is not False:
        return "needs_review", [], ["V3 annotation already requires human review"]

    grouped: dict[str, list[dict]] = {}
    unresolved_domain = []
    for constraint in annotation.get("atomic_visual_constraints") or []:
        old_category = constraint.get("visual_category")
        if old_category == DOMAIN_CATEGORY:
            unresolved_domain.append(constraint.get("constraint_id"))
            continue
        category = LEGACY_CATEGORY_MAP.get(old_category)
        if category is None:
            return "needs_review", [], [f"unmapped V3 category: {old_category}"]
        grouped.setdefault(category, []).append(constraint)

    if unresolved_domain:
        return "needs_review_unmapped_domain", [], [
            "V3 domain-semantic evidence cannot be mapped to a V4 capability "
            "without interpreting the pixels again",
            "unmapped_constraints=" + ",".join(str(value) for value in unresolved_domain),
        ]
    capabilities = []
    for category in CATEGORIES:
        constraints = grouped.get(category) or []
        if not constraints:
            continue
        importance = ("core" if any(item.get("decision_critical") == "是"
                                    for item in constraints) else "supporting")
        evidence = "；".join(dict.fromkeys(
            str(item.get("direct_visual_evidence") or "").strip()
            for item in constraints if str(item.get("direct_visual_evidence") or "").strip()))
        relation = "；".join(dict.fromkeys(
            str(item.get("description") or "").strip()
            for item in constraints if str(item.get("description") or "").strip()))
        if not evidence or not relation:
            return "needs_review", [], [f"{category} lacks direct evidence or task relation"]
        capabilities.append({
            "category": category,
            "importance": importance,
            "visual_evidence": evidence,
            "task_relation": relation,
            "source_constraint_ids": [item.get("constraint_id") for item in constraints],
        })
    if not capabilities or not any(item["importance"] == "core" for item in capabilities):
        return "needs_review", [], ["no directly mappable core V4 capability"]
    return "converted", capabilities, []


def run(config_path: Path, output: Path) -> dict:
    config_path, output = config_path.resolve(strict=True), output.resolve()
    if output.exists():
        raise ValueError(f"V3-to-V4 output already exists: {output}")
    config = json.loads(config_path.read_text())
    sources = [item for item in config.get("records") or []
               if item.get("evidence_type") == "strict_v3"]
    if not sources:
        raise ValueError("conversion config has no frozen strict_v3 records")
    case_ids = [item.get("case_id") for item in sources]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("conversion config contains duplicate V3 cases")

    cache, rows = {}, []
    for source in sources:
        case_id = source["case_id"]
        manifest_path = Path(source["evidence_file"]).resolve(strict=True)
        try:
            if manifest_path not in cache:
                manifest_value = json.loads(manifest_path.read_text())
                source_run = Path(manifest_value["source_run"]).resolve(strict=True)
                cache[manifest_path] = (
                    validate_classification_run(source_run, manifest_path), source_run)
        except Exception as exc:
            rows.append({
                "case_id": case_id,
                "conversion_status": "excluded_invalid_v3_run",
                "v3_primary_visual_category": source.get("category"),
                "v3_category_purity": None,
                "v3_visual_necessity_reason": None,
                "visual_capabilities": [],
                "conversion_reasons": [f"{type(exc).__name__}: {str(exc)[:1000]}"],
                "problem_statement": "",
                "source_qualification": None,
                "v3_classification": str(manifest_path),
                "v3_classification_sha256": _sha(manifest_path),
                "packet": None,
                "packet_sha256": None,
            })
            continue
        manifest, source_run = cache[manifest_path]
        matches = [(index, record) for index, record in enumerate(manifest["records"], 1)
                   if record.get("case_id") == case_id]
        if len(matches) != 1:
            raise ValueError(f"{case_id}: frozen V3 record count is not one")
        index, record = matches[0]
        qualification = _qualification_from_source(
            record, source_run / f"16_03_result_{index:04d}.json")
        capability = record.get("visual_capability") or {}
        annotation = capability.get("annotation") or {}
        status, converted, reasons = _convert_annotation(annotation)
        if qualification.get("qualified") is not True:
            status = "excluded_source_or_asset_binding"
            reasons = qualification.get("reasons") or ["source qualification failed"]
            converted = []
        if annotation.get("primary_visual_category") != source.get("category"):
            status = "excluded_config_category_mismatch"
            reasons = ["config category differs from frozen V3 primary_visual_category"]
            converted = []
        packet_path = Path(record.get("packet", ""))
        packet = json.loads(packet_path.read_text()) if packet_path.is_file() else {}
        rows.append({
            "case_id": case_id,
            "conversion_status": status,
            "v3_primary_visual_category": annotation.get("primary_visual_category"),
            "v3_category_purity": annotation.get("category_purity"),
            "v3_visual_necessity_reason": annotation.get("admission_reason"),
            "visual_capabilities": converted,
            "conversion_reasons": reasons,
            "problem_statement": packet.get("problem_statement", ""),
            "source_qualification": qualification,
            "v3_classification": str(manifest_path),
            "v3_classification_sha256": _sha(manifest_path),
            "packet": str(packet_path) if packet_path.is_file() else None,
            "packet_sha256": _sha(packet_path) if packet_path.is_file() else None,
        })

    converted_rows = [row for row in rows if row["conversion_status"] == "converted"]
    counts = Counter(item["category"] for row in converted_rows
                     for item in row["visual_capabilities"])
    distribution = [{
        "category": category,
        "label": CATEGORY_LABELS[category],
        "count": counts[category],
    } for category in CATEGORIES]
    result = {
        "schema_version": "v3-to-v4-capability-conversion-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "method": "deterministic_no_model_call",
        "model_invoked": False,
        "source_config": str(config_path),
        "source_config_sha256": _sha(config_path),
        "converter": str(Path(__file__).resolve()),
        "converter_sha256": _sha(Path(__file__).resolve()),
        "input_v3_count": len(rows),
        "converted_count": len(converted_rows),
        "needs_review_count": sum(row["conversion_status"].startswith("needs_review")
                                  for row in rows),
        "excluded_count": sum(row["conversion_status"].startswith("excluded")
                              for row in rows),
        "multi_label_count": sum(len(row["visual_capabilities"]) > 1
                                 for row in converted_rows),
        "distribution": distribution,
        "records": sorted(rows, key=lambda item: item["case_id"]),
    }
    output.mkdir(parents=True)
    data_path = output / "16_13_01_v3_to_v4_classifications.json"
    write_json(data_path, result)
    pills = "".join(
        f'<span class="pill">{html.escape(item["label"])} {item["count"]}</span>'
        for item in distribution)
    cards = []
    for row in result["records"]:
        labels = "、".join(CATEGORY_LABELS[item["category"]]
                           for item in row["visual_capabilities"]) or "—"
        cards.append(f'''<article class="{html.escape(row["conversion_status"])}"><header><code>{html.escape(row["case_id"])}</code><b>{html.escape(row["conversion_status"])}</b></header><p>V3: {html.escape(str(row["v3_primary_visual_category"]))} / {html.escape(str(row["v3_category_purity"]))}</p><p>V4: {html.escape(labels)}</p><p>{html.escape("；".join(row["conversion_reasons"]) or "直接映射成功")}</p><details><summary>完整题面</summary><pre>{html.escape(row["problem_statement"])}</pre></details><details><summary>V4 转换证据</summary><pre>{html.escape(json.dumps(row["visual_capabilities"], ensure_ascii=False, indent=2))}</pre></details><details><summary>来源绑定</summary><pre>{html.escape(json.dumps({key: row[key] for key in ("v3_classification", "v3_classification_sha256", "packet", "packet_sha256", "source_qualification")}, ensure_ascii=False, indent=2))}</pre></details></article>''')
    page = f'''<!doctype html><meta charset="utf-8"><title>V3 → V4 能力转换</title><style>body{{font:13px system-ui;margin:16px;color:#182034}}.pills{{display:flex;gap:6px;flex-wrap:wrap;position:sticky;top:0;background:white;padding:8px 0}}.pill{{background:#eef2ff;padding:4px 8px;border-radius:7px}}main{{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:8px}}article{{border:1px solid #d7ddea;border-radius:8px;padding:9px}}article:not(.converted){{background:#fff7ed}}header{{display:flex;justify-content:space-between}}pre{{white-space:pre-wrap;background:#f7f8fb;padding:7px;max-height:260px;overflow:auto}}</style><h1>V3 → V4 确定性能力转换</h1><p>零模型调用；旧领域语义约束不猜测，统一进入待复核。混合题按原子约束拆为多个 V4 标签。</p><div class="pills">{pills}</div><main>{''.join(cards)}</main>'''
    html_path = output / "16_13_02_v3_to_v4_audit.html"
    html_path.write_text(page + "\n")
    manifest = {
        "schema_version": "v3-to-v4-capability-conversion-manifest-v1",
        "data": data_path.name,
        "data_sha256": _sha(data_path),
        "html": html_path.name,
        "html_sha256": _sha(html_path),
    }
    write_json(output / "16_13_03_manifest.json", manifest)
    return result
