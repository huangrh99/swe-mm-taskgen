"""Build the provisional four-capability, multi-label visual SWE pool."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import html
import json
import mimetypes
from pathlib import Path
import shutil

from report_pipeline.category_audit import (
    CATEGORIES, CATEGORY_LABELS, LEGACY_CATEGORY_MAP,
    _qualification_from_source,
)
from report_pipeline.pre_review_classification import validate_classification_run
from report_pipeline.capability_verifier import validate_run as validate_capability_run


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _translation_source_sha(row: dict) -> str:
    value = (row["case_id"] + "\0" + (row["archive"]["pr_title"] or "")
             + "\0" + row["problem_statement"])
    return hashlib.sha256(value.encode()).hexdigest()


def _load_translations(paths: list[Path], rows: list[dict]) -> tuple[dict, list[dict]]:
    expected = {row["case_id"]: _translation_source_sha(row) for row in rows}
    translations = {}
    sources = []
    for raw_path in paths:
        path = raw_path.resolve(strict=True)
        value = json.loads(path.read_text())
        if (value.get("schema_version") != "human-review-zh-translations-v1"
                or "curator display only" not in value.get("notice", "")):
            raise ValueError(f"translation artifact is not curator-only: {path}")
        sources.append({"path": str(path), "sha256": _sha(path)})
        for item in value.get("items") or []:
            case_id = item.get("case_id")
            if case_id not in expected or item.get("source_text_sha256") != expected[case_id]:
                continue
            if not item.get("problem_statement_zh", "").strip():
                continue
            selected = {
                "pr_title_zh": item.get("pr_title_zh", ""),
                "problem_statement_zh": item["problem_statement_zh"],
                "source_text_sha256": item["source_text_sha256"],
                "translation_artifact": str(path),
                "translation_artifact_sha256": _sha(path),
            }
            if case_id in translations and translations[case_id] != selected:
                raise ValueError(f"ambiguous bound translations for {case_id}")
            translations[case_id] = selected
    return translations, sources


def _capabilities(annotation: dict) -> tuple[list[dict], bool, list[str]]:
    version = annotation.get("schema_version")
    if version == "visual-capability-classifier-v4":
        return annotation.get("visual_capabilities") or [], False, []
    if version != "visual-capability-classifier-v3":
        raise ValueError("unsupported capability annotation version")
    if (annotation.get("strict_multimodal_admission")
            != "非文字视觉信息候选不可替代"
            or annotation.get("human_review_required") is not False):
        raise ValueError("legacy V3 record is not a strict provisional candidate")
    values, seen = [], set()
    for constraint in annotation.get("atomic_visual_constraints") or []:
        category = LEGACY_CATEGORY_MAP.get(constraint.get("visual_category"))
        if category and category not in seen:
            seen.add(category)
            values.append({
                "category": category,
                "importance": ("core" if constraint.get("decision_critical") == "是"
                               else "supporting"),
                "visual_evidence": constraint.get("direct_visual_evidence") or "legacy V3 evidence",
                "task_relation": constraint.get("description") or "legacy V3 constraint",
            })
    warnings = []
    if any(constraint.get("visual_category") == "图形符号与领域语义理解"
           for constraint in annotation.get("atomic_visual_constraints") or []):
        warnings.append("旧领域语义标签没有直接迁移；既有证据不能明确映射时进入复核，不猜测。")
    if not values:
        raise ValueError("legacy V3 record has no capability that can be safely migrated")
    return values, True, warnings


def _validate_capabilities(case_id: str, capabilities: list[dict]) -> list[str]:
    categories = [item.get("category") for item in capabilities]
    if (not categories or len(categories) != len(set(categories))
            or any(category not in CATEGORIES for category in categories)):
        raise ValueError(f"{case_id}: capabilities are missing, duplicated, or invalid")
    if not any(item.get("importance") == "core" for item in capabilities):
        raise ValueError(f"{case_id}: no core capability")
    return categories


def _archive_and_assets(qualification: dict, packet: dict) -> tuple[dict, list[dict]]:
    from report_pipeline.paths import WORKSPACE_ROOT
    archive_path = Path(qualification["source_archive"])
    if not archive_path.is_absolute():
        archive_path = WORKSPACE_ROOT / archive_path
    archive_path = archive_path.resolve(strict=True)
    archive = json.loads(archive_path.read_text())
    asset_rows = {item.get("sha256"): item
                  for item in (archive.get("sections", {}).get("assets", {}).get("items") or [])}
    assets = []
    for item in packet.get("assets") or []:
        asset_id = item["asset_id"]
        archived = asset_rows.get(asset_id)
        if not archived:
            raise ValueError(f"solver-visible asset absent from archive: {asset_id}")
        logical_root = archive_path.parent / "11_http_archive"
        asset_path = (logical_root / archived["local_path"]).resolve(strict=True)
        if _sha(asset_path) != asset_id:
            raise ValueError(f"solver-visible asset changed: {asset_id}")
        media_type = archived.get("media_type") or mimetypes.guess_type(
            archived.get("url") or "")[0] or "application/octet-stream"
        assets.append({
            "asset_id": asset_id,
            "path": str(asset_path),
            "sha256": asset_id,
            "media_type": media_type,
            "source_ids": item.get("source_ids") or [],
        })
    pull = archive["sections"]["pull_request"]["data"]
    merge_anchor = archive["sections"]["merge_anchor_evidence"]
    archive_summary = {
        "path": str(archive_path),
        "sha256": _sha(archive_path),
        "status": archive.get("status"),
        "pr_url": pull.get("html_url"),
        "pr_title": pull.get("title"),
        "created_at": pull.get("created_at"),
        "merged_at": pull.get("merged_at"),
        "base_ref": (pull.get("base") or {}).get("ref"),
        "merge_sha": merge_anchor.get("resolved_sha"),
    }
    if (not archive_summary["merged_at"] or merge_anchor.get("status") != "complete"
            or not archive_summary["merge_sha"]):
        raise ValueError("PR is not bound to a completed merge anchor")
    return archive_summary, assets


def _render_page(result: dict, output: Path) -> str:
    distribution = result["distribution"]

    def render_asset(asset: dict) -> str:
        uri = Path(asset["display_path"]).relative_to(output).as_posix()
        label = html.escape(asset["asset_id"][:12])
        if asset["media_type"].startswith("video/"):
            escaped_uri = html.escape(uri, quote=True)
            media = (f'<video controls preload="auto" playsinline '
                     f'src="{escaped_uri}" data-preview-frame></video>'
                     f'<a class="asset-open" href="{escaped_uri}" target="_blank">打开原视频</a>')
        else:
            media = f'<img loading="lazy" src="{html.escape(uri, quote=True)}" alt="{label}">'
        return f'<figure>{media}<figcaption>{label} · {html.escape(asset["media_type"])}</figcaption></figure>'

    cards = []
    for row in result["records"]:
        tags = "".join(f'<span class="tag">{html.escape(CATEGORY_LABELS[value])}</span>'
                       for value in row["capability_categories"])
        warnings = (f'<p class="warn">{html.escape("；".join(row["warnings"]))}</p>'
                    if row["warnings"] else "")
        capability_text = "".join(
            f'<li><b>{html.escape(CATEGORY_LABELS[item["category"]])} · {html.escape(item["importance"])}</b>'
            f'<br>{html.escape(item["visual_evidence"])}<br><small>{html.escape(item["task_relation"])}</small></li>'
            for item in row["visual_capabilities"])
        bindings = {key: row[key] for key in (
            "classification_version", "migrated_from_v3", "classification",
            "classification_sha256", "packet", "packet_sha256")}
        category_value = " ".join(row["capability_categories"])
        translation = row.get("translation")
        if translation:
            translated = (f'<details open><summary>中文题面（仅供人工审核）</summary>'
                          f'<h3>{html.escape(translation["pr_title_zh"])}</h3>'
                          f'<pre>{html.escape(translation["problem_statement_zh"])}</pre>'
                          f'<small>来源哈希：{html.escape(translation["source_text_sha256"])}</small></details>')
        else:
            translated = '<p class="translation-missing">暂无与当前完整题面哈希绑定的中文翻译。</p>'
        cards.append(f'''<article data-categories="{html.escape(category_value, quote=True)}"><header><code>{html.escape(row["case_id"])}</code>
<a href="{html.escape(row["archive"]["pr_url"], quote=True)}" target="_blank">GitHub PR</a></header>
<h2>{html.escape(row["archive"]["pr_title"] or "")}</h2><div>{tags}</div>
<p>{html.escape(row["rationale"])}</p>{warnings}<ul>{capability_text}</ul>
{translated}<details><summary>无泄漏题面原文</summary><pre>{html.escape(row["problem_statement"])}</pre></details>
<div class="assets">{''.join(render_asset(asset) for asset in row["assets"])}</div>
<details><summary>来源与 merge 绑定</summary><pre>{html.escape(json.dumps(row["archive"], ensure_ascii=False, indent=2))}</pre></details>
<details><summary>模型与哈希绑定</summary><pre>{html.escape(json.dumps(bindings, ensure_ascii=False, indent=2))}</pre></details>
</article>''')
    pills = "".join(
        f'<span class="pill {"ok" if item["deficit"] == 0 else "bad"}">{html.escape(item["label"])} {item["count"]}/{item["required"]}</span>'
        for item in distribution)
    total_rows = len(result["records"])
    filters = [f'<button class="filter active" type="button" data-category="all">全部 {total_rows}</button>']
    filters.extend(
        f'<button class="filter" type="button" data-category="{html.escape(item["category"], quote=True)}">'
        f'{html.escape(item["label"])} {item["count"]}</button>'
        for item in distribution)
    return f'''<!doctype html><meta charset="utf-8"><title>四类视觉能力候选池</title>
<style>body{{font:13px system-ui;margin:16px;color:#182034}}h1{{font-size:22px;margin:0}}.note{{color:#65708a}}.pills{{display:flex;gap:6px;flex-wrap:wrap;margin:10px 0}}.pill,.tag{{padding:3px 7px;border-radius:7px;background:#edf2ff}}.ok{{background:#dcfce7}}.bad{{background:#fee2e2}}.toolbar{{display:flex;align-items:center;gap:5px;flex-wrap:wrap;position:sticky;top:0;background:#fff;padding:7px 0;z-index:2;border-bottom:1px solid #e6e9f0}}.filter{{border:1px solid #cbd3e2;background:#fff;border-radius:7px;padding:5px 8px;color:#25324b;cursor:pointer}}.filter.active{{background:#275bd6;color:#fff;border-color:#275bd6}}#visible-count{{margin-left:auto;color:#65708a}}main{{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:9px;margin-top:9px}}article{{border:1px solid #d7ddea;border-radius:9px;padding:10px;min-width:0}}article[hidden]{{display:none}}header{{display:flex;justify-content:space-between}}h2{{font-size:16px;margin:7px 0}}h3{{font-size:14px;margin:7px}}.tag{{display:inline-block;margin:2px}}.warn{{color:#9a3412;background:#fff7ed;padding:6px;border-radius:6px}}.translation-missing{{color:#9a3412;background:#fff7ed;padding:6px;border-radius:6px}}pre{{white-space:pre-wrap;font-size:11px;background:#f7f8fb;padding:7px;border-radius:6px;max-height:260px;overflow:auto}}.assets{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:5px}}figure{{margin:0;border:1px solid #ddd;border-radius:6px;overflow:hidden}}img,video{{width:100%;height:220px;object-fit:contain;background:#f6f7f9}}.asset-open{{display:block;padding:4px 6px;border-top:1px solid #ddd;background:#fff}}figcaption{{padding:4px;font-size:10px;overflow-wrap:anywhere}}li{{margin:6px 0}}a{{color:#275bd6}}</style>
<h1>四类视觉能力候选池</h1><p class="note">多标签；每个能力池按唯一 PR 计数。V3 迁移记录仅供过渡，全部仍待多模态必要性与防泄漏审核。</p>
<div class="pills">{pills}</div><nav class="toolbar" aria-label="按视觉能力筛选">{''.join(filters)}<span id="visible-count">显示 {total_rows}/{total_rows}</span></nav>
<main>{''.join(cards)}</main>
<script>(()=>{{
const cards=[...document.querySelectorAll('article[data-categories]')];const count=document.querySelector('#visible-count');document.querySelectorAll('button.filter').forEach(button=>button.addEventListener('click',()=>{{const selected=button.dataset.category;document.querySelectorAll('button.filter').forEach(item=>item.classList.toggle('active',item===button));let visible=0;cards.forEach(card=>{{const show=selected==='all'||card.dataset.categories.split(' ').includes(selected);card.hidden=!show;if(show)visible+=1;}});count.textContent=`显示 ${{visible}}/${{cards.length}}`;}}));
const videos=[...document.querySelectorAll('video[data-preview-frame]')];
const prepare=(video)=>{{
  const seek=()=>{{if(!Number.isFinite(video.duration)||video.duration<=0)return;video.currentTime=Math.min(1,Math.max(.25,video.duration*.08));}};
  const capture=()=>{{
    const scale=Math.min(1,960/Math.max(video.videoWidth,video.videoHeight));
    const canvas=document.createElement('canvas');canvas.width=Math.max(1,Math.round(video.videoWidth*scale));canvas.height=Math.max(1,Math.round(video.videoHeight*scale));
    const context=canvas.getContext('2d');if(!context)return;context.drawImage(video,0,0,canvas.width,canvas.height);
    try{{video.poster=canvas.toDataURL('image/jpeg',.82);video.dataset.previewReady='true';video.currentTime=0;}}catch(error){{video.dataset.previewError=String(error);}}
  }};
  video.addEventListener('seeked',capture,{{once:true}});
  if(video.readyState>=1)seek();else video.addEventListener('loadedmetadata',seek,{{once:true}});
}};
videos.forEach(prepare);
}})();</script>'''


def build(config_path: Path, output: Path, *, required_per_category: int = 5) -> dict:
    config_path, output = config_path.resolve(), output.resolve()
    if output.exists():
        raise ValueError(f"output already exists: {output}")
    config = json.loads(config_path.read_text())
    if config.get("schema_version") != "capability-candidate-pool-config-v2":
        raise ValueError("unsupported candidate-pool config")
    sources = config.get("records")
    if not isinstance(sources, list) or not sources:
        raise ValueError("candidate-pool records are missing")
    case_ids = [item.get("case_id") for item in sources]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("a PR may appear only once; use multi-label capabilities")

    validated_runs: dict[Path, tuple[dict, Path]] = {}
    validated_capability_runs: dict[Path, dict] = {}
    rows = []
    for source in sources:
        case_id = source.get("case_id")
        capability_run_value = source.get("capability_run")
        if capability_run_value:
            capability_run = Path(capability_run_value).resolve(strict=True)
            if capability_run not in validated_capability_runs:
                validated_capability_runs[capability_run] = validate_capability_run(
                    capability_run)
            run = validated_capability_runs[capability_run]
            matches = [row for row in run["records"]
                       if row.get("case_id") == case_id]
            if len(matches) != 1 or matches[0].get("status") != "complete":
                raise ValueError(f"{case_id}: V4 capability record is missing or incomplete")
            record = matches[0]
            capabilities, migrated, migration_warnings = _capabilities(
                record.get("annotation") or {})
            categories = _validate_capabilities(case_id, capabilities)
            packet_path = Path(record["packet"]).resolve(strict=True)
            if _sha(packet_path) != record["packet_sha256"]:
                raise ValueError(f"{case_id}: capability packet changed")
            packet = json.loads(packet_path.read_text())
            archive, assets = _archive_and_assets(
                {"source_archive": record["source_archive"]}, packet)
            rows.append({
                "case_id": case_id,
                "candidate_status": "pending_human_visual_gate",
                "classification_version": record["annotation"]["schema_version"],
                "migrated_from_v3": migrated,
                "visual_capabilities": capabilities,
                "capability_categories": categories,
                "problem_statement": packet.get("problem_statement", ""),
                "assets": assets,
                "archive": archive,
                "classification": str(capability_run),
                "classification_sha256": _sha(
                    capability_run / "16_11_03_capability_results.json"),
                "packet": str(packet_path),
                "packet_sha256": _sha(packet_path),
                "rationale": source.get("rationale", ""),
                "warnings": [*migration_warnings, *(source.get("warnings") or [])],
            })
            continue

        classification_path = Path(source.get("classification", "")).resolve(strict=True)
        if classification_path not in validated_runs:
            manifest = json.loads(classification_path.read_text())
            source_run = Path(manifest["source_run"]).resolve(strict=True)
            validated_runs[classification_path] = (
                validate_classification_run(source_run, classification_path), source_run)
        manifest, source_run = validated_runs[classification_path]
        matches = [(index, row) for index, row in enumerate(manifest["records"], 1)
                   if row.get("case_id") == case_id]
        if len(matches) != 1:
            raise ValueError(f"{case_id}: classification record count is not one")
        index, record = matches[0]
        qualification = _qualification_from_source(
            record, source_run / f"16_03_result_{index:04d}.json")
        if qualification.get("qualified") is not True:
            raise ValueError(f"{case_id}: source qualification failed: "
                             + ",".join(qualification.get("reasons") or []))
        capability = record.get("visual_capability") or {}
        if capability.get("status") != "complete":
            raise ValueError(f"{case_id}: capability classification is incomplete")
        capabilities, migrated, migration_warnings = _capabilities(
            capability.get("annotation") or {})
        categories = _validate_capabilities(case_id, capabilities)
        packet_path = Path(record["packet"]).resolve(strict=True)
        if _sha(packet_path) != record["packet_sha256"]:
            raise ValueError(f"{case_id}: classification packet changed")
        packet = json.loads(packet_path.read_text())
        archive, assets = _archive_and_assets(qualification, packet)
        rows.append({
            "case_id": case_id,
            "candidate_status": "pending_human_visual_gate",
            "classification_version": capability["annotation"]["schema_version"],
            "migrated_from_v3": migrated,
            "visual_capabilities": capabilities,
            "capability_categories": categories,
            "problem_statement": packet.get("problem_statement", ""),
            "assets": assets,
            "archive": archive,
            "classification": str(classification_path),
            "classification_sha256": _sha(classification_path),
            "packet": str(packet_path),
            "packet_sha256": _sha(packet_path),
            "rationale": source.get("rationale", ""),
            "warnings": [*migration_warnings, *(source.get("warnings") or [])],
        })

    counts = Counter(category for row in rows for category in row["capability_categories"])
    distribution = [{
        "category": category,
        "label": CATEGORY_LABELS[category],
        "count": counts[category],
        "required": required_per_category,
        "deficit": max(0, required_per_category - counts[category]),
    } for category in CATEGORIES]
    quota_met = all(item["deficit"] == 0 for item in distribution)
    result = {
        "schema_version": "capability-candidate-pool-v2",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "scope": "provisional recall only; all candidates await the human visual gate",
        "config": str(config_path),
        "config_sha256": _sha(config_path),
        "required_per_category": required_per_category,
        "quota_met": quota_met,
        "unique_pr_count": len(rows),
        "multi_label_pr_count": sum(len(row["capability_categories"]) > 1 for row in rows),
        "distribution": distribution,
        "records": sorted(rows, key=lambda row: row["case_id"]),
    }
    output.mkdir(parents=True)
    asset_output = output / "16_11_05_assets"
    asset_output.mkdir()
    for row in result["records"]:
        for asset in row["assets"]:
            source_path = Path(asset["path"])
            suffix = source_path.suffix.lower() or mimetypes.guess_extension(
                asset["media_type"]) or ".bin"
            destination = asset_output / f'{asset["asset_id"]}{suffix}'
            if not destination.exists():
                shutil.copy2(source_path, destination)
            if _sha(destination) != asset["asset_id"]:
                raise ValueError(f'copied display asset changed: {asset["asset_id"]}')
            asset["display_path"] = str(destination)
    data_path = output / "16_11_05_candidate_pool.json"
    data_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")

    html_path = output / "16_11_06_candidate_pool.html"
    html_path.write_text(_render_page(result, output) + "\n")
    manifest = {
        "schema_version": "capability-candidate-pool-manifest-v2",
        "status": "complete" if quota_met else "deficit",
        "data": data_path.name, "data_sha256": _sha(data_path),
        "html": html_path.name, "html_sha256": _sha(html_path),
    }
    (output / "16_11_07_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return result


def render_snapshot(source_run: Path, output: Path,
                    translation_paths: list[Path] | None = None) -> dict:
    """Render a new portable view from a hash-bound frozen pool without model calls."""
    source_run, output = source_run.resolve(strict=True), output.resolve()
    if output.exists():
        raise ValueError(f"output already exists: {output}")
    source_manifest_path = source_run / "16_11_07_manifest.json"
    source_manifest = json.loads(source_manifest_path.read_text())
    if (source_manifest.get("schema_version") != "capability-candidate-pool-manifest-v2"
            or source_manifest.get("data") != "16_11_05_candidate_pool.json"
            or source_manifest.get("html") != "16_11_06_candidate_pool.html"):
        raise ValueError("source candidate-pool manifest is invalid")
    source_data = source_run / source_manifest["data"]
    source_html = source_run / source_manifest["html"]
    if (_sha(source_data) != source_manifest.get("data_sha256")
            or _sha(source_html) != source_manifest.get("html_sha256")):
        raise ValueError("source candidate-pool manifest binding changed")
    result = json.loads(source_data.read_text())
    if result.get("schema_version") != "capability-candidate-pool-v2":
        raise ValueError("source candidate-pool data is invalid")
    records = result.get("records") or []
    case_ids = [row.get("case_id") for row in records]
    counts = Counter(category for row in records
                     for category in row.get("capability_categories") or [])
    expected_counts = {item["category"]: item["count"]
                       for item in result.get("distribution") or []}
    if (not records or len(case_ids) != len(set(case_ids))
            or len(records) != result.get("unique_pr_count")
            or dict(counts) != expected_counts):
        raise ValueError("source candidate-pool counts are not reproducible")

    output.mkdir(parents=True)
    asset_output = output / "16_11_05_assets"
    asset_output.mkdir()
    render_result = json.loads(json.dumps(result))
    translations, translation_sources = _load_translations(
        translation_paths or [], render_result["records"])
    for row in render_result["records"]:
        if row["case_id"] in translations:
            row["translation"] = translations[row["case_id"]]
    asset_count = 0
    for row in render_result["records"]:
        for asset in row["assets"]:
            source_value = Path(asset["display_path"])
            source_path = source_value.resolve(strict=True)
            if (source_value.is_symlink() or not source_path.is_relative_to(source_run)
                    or _sha(source_path) != asset["sha256"]):
                raise ValueError(f'candidate display asset changed: {asset["asset_id"]}')
            destination = asset_output / source_path.name
            shutil.copy2(source_path, destination)
            if _sha(destination) != asset["sha256"]:
                raise ValueError(f'copied display asset changed: {asset["asset_id"]}')
            asset["display_path"] = str(destination)
            asset_count += 1
    data_path = output / source_data.name
    shutil.copy2(source_data, data_path)
    html_path = output / "16_11_06_candidate_pool.html"
    html_path.write_text(_render_page(render_result, output) + "\n")
    manifest = {
        "schema_version": "capability-candidate-pool-view-manifest-v1",
        "status": source_manifest["status"],
        "source_manifest": str(source_manifest_path),
        "source_manifest_sha256": _sha(source_manifest_path),
        "model_invoked": False,
        "asset_count": asset_count,
        "translation_count": len(translations),
        "translation_missing_case_ids": sorted(set(case_ids) - set(translations)),
        "translation_sources": translation_sources,
        "data": data_path.name,
        "data_sha256": _sha(data_path),
        "html": html_path.name,
        "html_sha256": _sha(html_path),
    }
    (output / "16_11_07_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest
