"""Render and audit the dedicated visual-necessity/leakage human gate.

This stage deliberately excludes F2P/P2P review.  V3 outputs are curator
suggestions only; the browser export cannot promote a task by itself.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from report_pipeline.atomic import write_json
from report_pipeline.category_audit import (
    CATEGORIES, CATEGORY_LABELS, COUNTED_CATEGORIES, _classification_runs,
)
from report_pipeline.paths import CODE_ROOT, WORKSPACE_ROOT


SCHEMA = CODE_ROOT.parent / "schemas/visual_gate_review_v1.schema.json"
RUNNER_VERSION = "visual-gate-ui-v1"
ROLE_VALUES = (
    "before_only", "after_only", "before_after_composite",
    "expected_design", "temporal_sequence", "unclear",
)
CHANGE_SCALE_LABELS = ("小规模修改", "中规模修改", "大规模修改", "无法分类")
ISSUE_SOURCE_ID = re.compile(
    r"^(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(?P<number>[1-9][0-9]*)(?::(?:title|body))?$"
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json_hash(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _workspace_path(value: str) -> Path:
    path = Path(value)
    return (path if path.is_absolute() else WORKSPACE_ROOT / path).resolve(strict=True)


def _portable(path: Path, root: Path) -> str:
    return Path(os.path.relpath(path.resolve(), root.resolve())).as_posix()


def translation_bindings(distribution_path: Path) -> list[dict]:
    """Return available curator-only translations bound to counted rows."""
    distribution_path = distribution_path.resolve(strict=True)
    distribution = json.loads(distribution_path.read_text())
    if distribution.get("schema_version") == "visual-review-unified-index-v1":
        bindings = distribution.get("translations") or []
        for binding in bindings:
            path = _workspace_path(binding.get("path", ""))
            if _sha(path) != binding.get("sha256"):
                raise ValueError("unified curator translation binding changed")
        return bindings
    paths = set()
    for row in distribution.get("rows", []):
        if row.get("counted") is not True:
            continue
        source_result = _workspace_path(
            (row.get("source_qualification") or {}).get("source_result", ""))
        candidate = source_result.parent / "16_04_04_translations_zh.json"
        if candidate.is_file():
            paths.add(candidate.resolve(strict=True))
    return [{"path": _portable(path, WORKSPACE_ROOT), "sha256": _sha(path)}
            for path in sorted(paths)]


def _translation_index(bindings: list[dict]) -> dict[str, dict]:
    translations = {}
    for binding in bindings:
        path = _workspace_path(binding["path"])
        if _sha(path) != binding["sha256"]:
            raise ValueError("curator translation binding changed")
        value = json.loads(path.read_text())
        if value.get("schema_version") != "human-review-zh-translations-v1":
            raise ValueError("unsupported curator translation schema")
        source_manifest = path.parent / "16_03_run_manifest.json"
        if (not source_manifest.is_file()
                or _sha(source_manifest) != value.get("source_run_manifest_sha256")):
            raise ValueError("curator translation source run changed")
        for item in value.get("items", []):
            case_id = item.get("case_id")
            if case_id in translations:
                raise ValueError(f"duplicate curator translation: {case_id}")
            translations[case_id] = {**item, "translation_file": binding["path"],
                                     "translation_file_sha256": binding["sha256"]}
    return translations


def _source_route(packet: dict) -> str:
    source_ids = [str(source_id).lower()
                  for asset in packet.get("assets", [])
                  for source_id in asset.get("source_ids", [])]
    issue = any("#" in value and (value.endswith(":body") or value.endswith(":title"))
                and not value.startswith(("pr:", "pull:")) for value in source_ids)
    pr = any(value.startswith(("pr:", "pull:", "comments:", "review"))
             for value in source_ids)
    if issue and not pr:
        return "issue_derived"
    if pr and not issue:
        return "pr_derived"
    raise ValueError("solver-visible assets do not define one unambiguous source route")


def _seed_role(value: str) -> str:
    return {
        "实际状态": "before_only",
        "期望目标": "expected_design",
        "时序证据": "temporal_sequence",
        "多状态过程": "temporal_sequence",
        "可能泄漏的修复后结果": "after_only",
        "当前输入不足，无法判断": "unclear",
    }.get(value, "unclear")


def _problem_sources_with_links(problem_sources: list[dict]) -> list[dict]:
    """Attach a derived GitHub Issue URL without trusting arbitrary source text as a URL."""
    linked = []
    for source in problem_sources:
        item = dict(source)
        match = ISSUE_SOURCE_ID.fullmatch(str(item.get("source_id") or ""))
        item["issue_url"] = (
            f"https://github.com/{match.group('repo')}/issues/{match.group('number')}"
            if match else None
        )
        linked.append(item)
    return linked


def _change_scale(record: dict) -> dict:
    """Validate the deterministic reference-patch scale shown to reviewers."""
    value = record.get("change_scale")
    if not isinstance(value, dict) or value.get("schema_version") != "reference-change-scale-v1":
        raise ValueError("candidate lacks reference change-scale evidence")
    if value.get("label") not in CHANGE_SCALE_LABELS:
        raise ValueError("candidate has an unsupported reference change-scale label")
    for name in ("cleaned_source_file_count", "cleaned_changed_lines",
                 "raw_changed_file_count", "raw_changed_lines"):
        if not isinstance(value.get(name), int) or value[name] < 0:
            raise ValueError(f"candidate change-scale field is invalid: {name}")
    if not isinstance(value.get("production_files"), list) or not isinstance(
            value.get("excluded_files"), list):
        raise ValueError("candidate change-scale file inventory is invalid")
    return value


def _asset_file(archive_path: Path, archive: dict, asset_id: str) -> tuple[Path, dict]:
    matches = [item for item in archive.get("sections", {}).get("assets", {}).get("items", [])
               if item.get("sha256") == asset_id and item.get("status") == "complete"]
    if len(matches) != 1:
        raise ValueError(f"solver-visible asset is missing or duplicated: {asset_id}")
    item = matches[0]
    relative = Path(item.get("local_path", ""))
    root = archive_path.parent / "11_http_archive"
    if relative.is_absolute() or ".." in relative.parts or root.is_symlink():
        raise ValueError("unsafe solver-visible asset path")
    resolved_root = root.resolve(strict=True)
    path = (root / relative).resolve(strict=True)
    if (not path.is_file() or path.is_symlink()
            or not path.is_relative_to(resolved_root) or _sha(path) != asset_id):
        raise ValueError(f"solver-visible asset binding changed: {asset_id}")
    return path, item


def _extension(media_type: str | None) -> str:
    return {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
            "image/gif": ".gif", "video/mp4": ".mp4",
            "video/quicktime": ".mov"}.get(media_type or "", ".bin")


def _case_payload(row: dict, record: dict, staging: Path, index: int,
                  classification_path: Path, translations: dict[str, dict]) -> tuple[dict, list[dict]]:
    qualification = row["source_qualification"]
    packet_path = _workspace_path(qualification["classification_packet"])
    source_result_path = _workspace_path(qualification["source_result"])
    source_packet_path = _workspace_path(qualification["source_packet"])
    archive_path = _workspace_path(qualification["source_archive"])
    expected = {
        packet_path: qualification["classification_packet_sha256"],
        source_result_path: qualification["source_result_sha256"],
        source_packet_path: qualification["source_packet_sha256"],
        archive_path: qualification["source_archive_sha256"],
        classification_path: qualification["classification_sha256"],
    }
    for path, digest in expected.items():
        if _sha(path) != digest:
            raise ValueError(f"candidate source binding changed: {path}")
    packet = json.loads(packet_path.read_text())
    source_result = json.loads(source_result_path.read_text())
    source_packet = json.loads(source_packet_path.read_text())
    archive = json.loads(archive_path.read_text())
    if (packet.get("task_id") != row["case_id"]
            or source_result.get("case_id") != row["case_id"]
            or source_packet.get("case_id") != row["case_id"]
            or archive.get("instance_id") != row["case_id"]):
        raise ValueError("candidate source identity changed")
    annotation = (record.get("visual_capability") or {}).get("annotation") or {}
    annotated = {item["asset_id"]: item for item in annotation.get("assets", [])}
    if [item.get("asset_id") for item in packet.get("assets", [])] != list(annotated):
        raise ValueError("V3 packet and annotation asset order changed")

    copied, assets = [], []
    case_directory = staging / "16_04_02_assets" / f"case_{index:04d}"
    case_directory.mkdir(parents=True)
    for asset_index, packet_asset in enumerate(packet["assets"], 1):
        asset_id = packet_asset["asset_id"]
        source, archived = _asset_file(archive_path, archive, asset_id)
        destination = case_directory / (
            f"asset_{asset_index:02d}_{asset_id[:12]}" + _extension(archived.get("media_type")))
        shutil.copyfile(source, destination)
        if _sha(destination) != asset_id:
            raise ValueError("copied visual review asset changed")
        suggestion = annotated[asset_id]
        relative = destination.relative_to(staging).as_posix()
        gate_suggestion = {
            "solver_visible_role": suggestion.get("solver_visible_role"),
            "seed_temporal_role": _seed_role(suggestion.get("solver_visible_role", "")),
            "ocr_transcription_sufficient": suggestion.get("ocr_transcription_sufficient"),
            "task_relevance": suggestion.get("task_relevance"),
            "observation": suggestion.get("observation"),
        }
        assets.append({
            "asset_id": asset_id,
            "path": relative,
            "sha256": asset_id,
            "media_type": archived.get("media_type") or "application/octet-stream",
            "source_ids": packet_asset.get("source_ids") or [],
            "v3_suggestion": gate_suggestion,
            "gate_suggestion": gate_suggestion,
        })
        copied.append({"case_id": row["case_id"], "asset_id": asset_id,
                       "path": relative, "sha256": asset_id})

    pull_request = archive["sections"]["pull_request"]["data"]
    problem_sources = _problem_sources_with_links(source_packet.get("problem_sources") or [])
    problem_statement = packet.get("problem_statement")
    if not isinstance(problem_statement, str) or not problem_statement.strip():
        raise ValueError("candidate problem statement is empty")
    route = _source_route(packet)
    change_scale = _change_scale(record)
    translation = translations.get(row["case_id"])
    source_text_sha256 = hashlib.sha256(
        (row["case_id"] + "\0" + (pull_request.get("title") or "")
         + "\0" + problem_statement).encode()).hexdigest()
    if translation and translation.get("source_text_sha256") != source_text_sha256:
        raise ValueError("curator translation belongs to different source text")
    case = {
        "case_id": row["case_id"], "position": index,
        "repository": source_packet.get("repository"),
        "pr_number": source_packet.get("pr_number"),
        "pr_url": pull_request.get("html_url"), "pr_title": pull_request.get("title"),
        "pr_body_curator_only": pull_request.get("body") or "",
        "source_route": route, "problem_statement": problem_statement,
        "problem_statement_sha256": hashlib.sha256(problem_statement.encode()).hexdigest(),
        "pr_title_zh": (translation or {}).get("pr_title_zh") or "",
        "problem_statement_zh": (translation or {}).get("problem_statement_zh") or "",
        "translation": ({
            "status": "available", "curator_only": True,
            "source_text_sha256": source_text_sha256,
            "file": translation["translation_file"],
            "file_sha256": translation["translation_file_sha256"],
        } if translation else {"status": "missing", "curator_only": True,
                               "source_text_sha256": source_text_sha256}),
        "problem_sources": problem_sources,
        "change_scale": change_scale,
        "category": row["primary_visual_category"],
        "category_purity": row["category_purity"],
        "evidence_mode": row["evidence_mode"],
        "v3": {
            "strict_multimodal_admission": row["strict_multimodal_admission"],
            "admission_reason": row["admission_reason"],
            "classification_reason": row["classification_reason"],
            "contributing_visual_categories": row["contributing_visual_categories"],
            "atomic_visual_constraints": annotation.get("atomic_visual_constraints") or [],
            "legacy_text_decision": source_result.get("text_decision"),
            "legacy_reconciliation": source_result.get("reconciliation"),
        },
        "assets": assets,
        "source_bindings": {
            "classification": _portable(classification_path, WORKSPACE_ROOT),
            "classification_sha256": _sha(classification_path),
            "classification_packet": _portable(packet_path, WORKSPACE_ROOT),
            "classification_packet_sha256": _sha(packet_path),
            "source_result": _portable(source_result_path, WORKSPACE_ROOT),
            "source_result_sha256": _sha(source_result_path),
            "source_packet": _portable(source_packet_path, WORKSPACE_ROOT),
            "source_packet_sha256": _sha(source_packet_path),
            "source_archive": _portable(archive_path, WORKSPACE_ROOT),
            "source_archive_sha256": _sha(archive_path),
        },
    }
    case["candidate_binding_sha256"] = _json_hash({
        "case_id": case["case_id"], "source_route": route,
        "problem_statement_sha256": case["problem_statement_sha256"],
        "assets": [{"asset_id": item["asset_id"], "source_ids": item["source_ids"]}
                   for item in assets],
        "change_scale": change_scale,
        "source_bindings": case["source_bindings"],
    })
    return case, copied


def _options(values: tuple[str, ...]) -> str:
    return "".join(f'<option value="{html.escape(value)}">{html.escape(value)}</option>'
                   for value in values)


def _page(payload: dict, manifest_sha256: str) -> str:
    encoded = base64.b64encode(json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")).encode()).decode()
    roles = _options(ROLE_VALUES)
    categories = "".join(
        f'<option value="{html.escape(value)}">{html.escape(CATEGORY_LABELS[value])}</option>'
        for value in CATEGORIES)
    categories += '<option value="__multi__">多能力组合</option>'
    category_labels = json.dumps(CATEGORY_LABELS, ensure_ascii=False)
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>多模态必要性与防泄漏审核</title>
<style>
:root{{--bg:#f3f4f6;--card:#fff;--line:#d8dce3;--muted:#667085;--accent:#2357d9}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:#1f2937;font:13px/1.45 system-ui,sans-serif}}
header{{position:sticky;top:0;z-index:4;background:#fff;border-bottom:1px solid var(--line);padding:8px 14px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}}
button,select,input,textarea{{font:inherit}}button,.button{{border:1px solid var(--line);background:#fff;border-radius:6px;padding:6px 9px;cursor:pointer}}button.primary{{background:var(--accent);color:#fff;border-color:var(--accent)}}
#layout{{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:10px;padding:10px;max-width:1500px;margin:auto}}section,.panel{{background:#fff;border:1px solid var(--line);border-radius:8px;padding:10px;margin-bottom:8px}}h1{{font-size:16px;margin:0}}h2{{font-size:15px;margin:0 0 7px}}h3{{font-size:13px;margin:0 0 5px}}small,.muted{{color:var(--muted)}}textarea{{width:100%;border:1px solid var(--line);border-radius:6px;padding:7px;resize:vertical}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#f8fafc;padding:8px;border-radius:5px;max-height:340px;overflow:auto}}.statement-grid{{display:grid;grid-template-columns:1fr 1fr;gap:8px}}.statement-grid textarea,.statement-grid pre{{height:330px;max-height:330px;margin:0;overflow:auto}}.images{{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:8px}}figure{{margin:0;border:1px solid var(--line);border-radius:7px;overflow:hidden}}figure img,figure video{{display:block;width:100%;height:240px;object-fit:contain;background:#fafafa}}figcaption{{padding:7px}}label{{display:block;margin:4px 0}}.row{{display:flex;gap:6px;align-items:center;flex-wrap:wrap}}.row label{{display:inline-flex;gap:4px;align-items:center}}code{{font-size:11px;overflow-wrap:anywhere}}.badge{{display:inline-block;padding:2px 6px;border-radius:9px;background:#edf2ff;color:#2949a3}}.asset-open{{display:inline-block;margin-top:4px}}aside{{position:sticky;top:62px;align-self:start;max-height:calc(100vh - 72px);overflow:auto}}.decision-grid{{display:grid;grid-template-columns:1fr 1fr;gap:6px}}.decision-grid label{{margin:0}}.decision-grid select{{width:100%}}.warn{{color:#9a3412}}#errors{{color:#b42318;white-space:pre-wrap}}#case[hidden]{{display:none}}details{{margin-top:5px}}summary{{cursor:pointer;font-weight:600}}@media(max-width:900px){{#layout{{grid-template-columns:1fr}}aside{{position:static;max-height:none}}.statement-grid{{grid-template-columns:1fr}}}}
</style>
<header><button id="prev">←</button><button id="next">→</button><h1>多模态必要性与防泄漏审核 · <span id="pos"></span></h1><select id="category"><option value="">全部类别</option>{categories}</select><span id="saved" class="muted"></span><button id="import">载入 JSON</button><button id="export" class="primary">导出审核 JSON</button><input id="file" type="file" accept="application/json" hidden></header>
<main id="layout"><div id="case"></div><aside><section><h2>审核结论</h2><div class="decision-grid"><label>问题来源（冻结）<select id="source-route" disabled><option value="issue_derived">Issue-derived</option><option value="pr_derived">PR-derived</option></select></label><label>纯文字足够？<select id="text-sufficient"><option value="unclear">待判断</option><option value="no">否</option><option value="yes">是</option></select></label><label>OCR 可完全替代？<select id="ocr"><option value="unclear">待判断</option><option value="no">否</option><option value="yes">是</option></select></label><label>结论<select id="decision"><option value="needs_review">待复核</option><option value="keep">保留候选</option><option value="exclude">排除</option></select></label></div><label><input id="leak-free" type="checkbox">题面已经人工确认不泄漏修复方案</label><label>判断依据（说明图片提供的不可替代非文字事实，以及保留或排除理由）<textarea id="review-basis" rows="5"></textarea></label><button id="save">保存本题</button><p id="errors"></p></section><section><h2>边界</h2><p>本页只审核视觉必要性与防泄漏。V3/V4 都是模型或确定性映射建议，不是人工结论；保存或导出不会准入 benchmark。</p><p class="warn">F2P/P2P 测试由独立的测试审核流程完成。</p><code>manifest sha256: {manifest_sha256}</code></section></aside></main>
<template id="payload">{encoded}</template>
<script>
const payloadNode=document.querySelector('#payload');
const encodedPayload=(payloadNode.content||payloadNode).textContent.trim();
const payloadBytes=Uint8Array.from(atob(encodedPayload),character=>character.charCodeAt(0));
const DATA=JSON.parse(new TextDecoder('utf-8',{{fatal:true}}).decode(payloadBytes));
const CATEGORY_LABELS={category_labels};
const KEY='visual-gate:'+DATA.source_manifest_sha256;let storageAvailable=true;
function readSaved(){{try{{return JSON.parse(localStorage.getItem(KEY)||'{{}}')}}catch(_){{storageAvailable=false;return {{}}}}}}
let saved=readSaved(),filtered=[...DATA.cases],at=0;
const $=s=>document.querySelector(s), esc=s=>String(s??'').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
function current(){{return filtered[at]}}
function suggestion(a){{return a.gate_suggestion||a.v3_suggestion||{{}}}}
function imageState(a){{const g=suggestion(a);return {{asset_id:a.asset_id,role:g.seed_temporal_role||g.solver_visible_role||'unclear',solver_visible:true,contains_fixed_after:g.contains_fixed_after===true,contains_solution_evidence:g.contains_solution_evidence===true,crop_required:false,reason:''}}}}
function initial(c){{return {{case_id:c.case_id,candidate_binding_sha256:c.candidate_binding_sha256,source_route:c.source_route,problem_statement:c.problem_statement,problem_statement_leak_free:false,text_only_sufficient:'unclear',ocr_replaceable:'unclear',non_text_visual_fact:'',images:c.assets.map(imageState),decision:'needs_review',reason:'',reviewed_at:''}}}}
function state(c){{return saved[c.case_id]||initial(c)}}
function reviewBasis(s){{const values=[s.non_text_visual_fact,s.reason].map(x=>String(x||'').trim()).filter(Boolean);return [...new Set(values)].join('\\n\\n')}}
function persist(){{if(storageAvailable){{try{{localStorage.setItem(KEY,JSON.stringify(saved))}}catch(_){{storageAvailable=false}}}}$('#saved').textContent=Object.keys(saved).length+'/'+DATA.cases.length+(storageAvailable?' 已保存':' 本页内暂存；请导出 JSON')}}
function render(){{const c=current();if(!c){{$('#case').innerHTML='<section>该筛选下没有候选。</section>';$('#pos').textContent='0/0';return}}const s=state(c),by=Object.fromEntries(s.images.map(x=>[x.asset_id,x]));$('#pos').textContent=(at+1)+'/'+filtered.length+' · '+c.case_id;$('#source-route').value=s.source_route;$('#text-sufficient').value=s.text_only_sufficient;$('#ocr').value=s.ocr_replaceable;$('#decision').value=s.decision;$('#leak-free').checked=s.problem_statement_leak_free;$('#review-basis').value=reviewBasis(s);
const imgs=c.assets.map((a,i)=>{{const x=by[a.asset_id]||imageState(a),g=suggestion(a),video=String(a.media_type||'').startsWith('video/'),media=video?`<video controls preload="auto" playsinline data-preview-frame src="${{esc(a.path)}}"></video><a class="asset-open" href="${{esc(a.path)}}" target="_blank">打开原视频 ↗</a>`:`<img loading="lazy" src="${{esc(a.path)}}">`;return `<figure data-id="${{a.asset_id}}">${{media}}<figcaption><b>视觉材料 ${{i+1}}</b> <span class="badge">角色建议: ${{esc(g.solver_visible_role)}}</span><p>${{esc(g.observation)}}</p><code>${{a.asset_id}}</code><div class="row"><label>角色<select class="role">{roles}</select></label><label><input class="visible" type="checkbox">交给 agent</label></div><label><input class="fixed" type="checkbox">含修复后结果</label><label><input class="solution" type="checkbox">含解决方案证据</label><label><input class="crop" type="checkbox">必须裁剪后使用</label><textarea class="image-reason" rows="2" placeholder="图片判断理由">${{esc(x.reason)}}</textarea><details><summary>来源与角色建议</summary><pre>${{esc(JSON.stringify({{source_ids:a.source_ids,suggestion:g}},null,2))}}</pre></details></figcaption></figure>`}}).join('');
const sources=c.problem_sources.map(x=>{{const label=x.issue_url?`<a class="issue-link" href="${{esc(x.issue_url)}}" target="_blank" rel="noopener noreferrer" title="打开对应 GitHub Issue">${{esc(x.source_id)}} ↗</a>`:esc(x.source_id);return `<details><summary>${{label}}</summary><pre>${{esc(x.text)}}</pre></details>`}}).join('');
const scale=c.change_scale;
const caps=(c.v4?.visual_capabilities||[]).map(x=>`<span class="badge">${{esc(CATEGORY_LABELS[x.category]||x.category)}} · ${{esc(x.importance)}}</span>`).join('');
const legacy=(c.v3&&c.v3.status!=='not_available_for_native_v4')?`<section><h2>V3 模型建议（保留作校准证据）</h2><p><b>必要性：</b>${{esc(c.v3.admission_reason)}}</p><p><b>分类：</b>${{esc(c.v3.classification_reason)}}</p><details><summary>完整约束与旧 Verifier 分歧</summary><pre>${{esc(JSON.stringify(c.v3,null,2))}}</pre></details></section>`:'';
$('#case').innerHTML=`<section><div class="row"><a href="${{esc(c.pr_url)}}" target="_blank"><b>${{esc(c.repository)}} #${{c.pr_number}}</b></a>${{caps}}<span>${{esc(c.category_purity)}}</span><span class="badge">参考代码修改量：${{esc(scale.label)}}</span><span>${{scale.cleaned_source_file_count}} 个生产文件 · ${{scale.cleaned_changed_lines}} 行</span></div><h2>${{esc(c.pr_title)}}</h2><div id="pr-title-zh">${{c.pr_title_zh?`<p class="muted">${{esc(c.pr_title_zh)}}</p>`:''}}</div><small>${{esc(c.source_route)}} · ${{esc(c.evidence_mode)}}</small><details><summary>代码修改量分级口径与文件明细</summary><p>小：1 个生产文件且 ≤100 行；中：2–4 个生产文件且 ≤100 行；大：超过 100 行或至少 5 个生产文件。该指标是参考 patch 的工程规模，不等同于认知难度。</p><pre>${{esc(JSON.stringify(scale,null,2))}}</pre></details></section><section><div class="statement-grid"><div><h2>无泄漏题面草稿 · 原文</h2><textarea id="statement">${{esc(s.problem_statement)}}</textarea></div><div><div class="row"><h2>中文翻译 · 仅供审核对照</h2><span id="translation-action"></span></div><div id="translation-content">${{c.problem_statement_zh?`<pre>${{esc(c.problem_statement_zh)}}</pre>`:'<p class="warn">译文待生成；在线审核页可按需翻译。</p>'}}</div></div></div><details><summary>原始 Issue 来源</summary>${{sources}}</details></section><section><h2>Solver-visible 视觉材料逐项审核</h2><div class="images">${{imgs}}</div></section><section><h2>V4 多标签能力建议</h2><div class="row">${{caps}}</div><details><summary>完整 V4 判断与来源绑定</summary><pre>${{esc(JSON.stringify(c.v4,null,2))}}</pre></details></section>${{legacy}}<section><details><summary>Curator-only PR 原文（不得复制进题面）</summary><pre>${{esc(c.pr_body_curator_only)}}</pre></details><details><summary>翻译与哈希绑定</summary><pre>${{esc(JSON.stringify({{translation:c.translation,source_bindings:c.source_bindings}},null,2))}}</pre></details></section>`;
document.querySelectorAll('.issue-link').forEach(a=>a.addEventListener('click',event=>event.stopPropagation()));
for(const video of document.querySelectorAll('video[data-preview-frame]')){{const capture=()=>{{if(!Number.isFinite(video.duration)||video.duration<=0)return;video.currentTime=Math.min(1,Math.max(.05,video.duration*.08))}},poster=()=>{{try{{const canvas=document.createElement('canvas');canvas.width=video.videoWidth;canvas.height=video.videoHeight;canvas.getContext('2d').drawImage(video,0,0);video.poster=canvas.toDataURL('image/jpeg',.82)}}catch(_){{}}video.currentTime=0}};video.addEventListener('loadedmetadata',capture,{{once:true}});video.addEventListener('seeked',poster,{{once:true}})}}
for(const fig of document.querySelectorAll('figure')){{const x=by[fig.dataset.id]||imageState(c.assets.find(a=>a.asset_id===fig.dataset.id));fig.querySelector('.role').value=x.role;fig.querySelector('.visible').checked=x.solver_visible;fig.querySelector('.fixed').checked=x.contains_fixed_after;fig.querySelector('.solution').checked=x.contains_solution_evidence;fig.querySelector('.crop').checked=x.crop_required}}
}}
function collect(){{const c=current(),s=state(c),basis=$('#review-basis').value;s.source_route=$('#source-route').value;s.problem_statement=$('#statement').value;s.problem_statement_leak_free=$('#leak-free').checked;s.text_only_sufficient=$('#text-sufficient').value;s.ocr_replaceable=$('#ocr').value;s.non_text_visual_fact=basis;s.decision=$('#decision').value;s.reason=basis;s.images=[...document.querySelectorAll('figure')].map(f=>({{asset_id:f.dataset.id,role:f.querySelector('.role').value,solver_visible:f.querySelector('.visible').checked,contains_fixed_after:f.querySelector('.fixed').checked,contains_solution_evidence:f.querySelector('.solution').checked,crop_required:f.querySelector('.crop').checked,reason:f.querySelector('.image-reason').value}}));s.reviewed_at=new Date().toISOString();return s}}
function validate(s){{let e=[];if(!s.problem_statement.trim())e.push('题面不能为空');if(!s.reason.trim())e.push('结论理由不能为空');if(s.decision==='keep'){{if(!s.problem_statement_leak_free)e.push('保留前必须确认题面无泄漏');if(s.text_only_sufficient!=='no')e.push('保留题必须确认纯文字不足');if(s.ocr_replaceable!=='no')e.push('保留题必须确认 OCR 不可替代');if(!s.non_text_visual_fact.trim())e.push('保留题必须写明不可替代的非文字事实');const visible=s.images.filter(x=>x.solver_visible);if(!visible.length)e.push('至少保留一张 solver-visible 图片');for(const x of visible){{if(['after_only','before_after_composite','unclear'].includes(x.role))e.push(x.asset_id.slice(0,12)+' 的角色不能直接交给 agent');if(x.contains_fixed_after||x.contains_solution_evidence)e.push(x.asset_id.slice(0,12)+' 存在泄漏');if(x.crop_required)e.push(x.asset_id.slice(0,12)+' 尚需裁剪')}}}}return e}}
$('#save').onclick=()=>{{const s=collect(),e=validate(s);$('#errors').textContent=e.join('\\n');if(e.length)return;saved[s.case_id]=s;persist();render()}};$('#prev').onclick=()=>{{if(at){{at--;render()}}}};$('#next').onclick=()=>{{if(at+1<filtered.length){{at++;render()}}}};$('#category').onchange=e=>{{const value=e.target.value;filtered=DATA.cases.filter(c=>{{const caps=c.v4?.visual_capabilities||[];return !value||(value==='__multi__'?caps.length>1:caps.some(x=>x.category===value))}});at=0;render()}};
$('#export').onclick=()=>{{const rows=DATA.cases.map(c=>saved[c.case_id]).filter(Boolean),bad=rows.flatMap(s=>validate(s).map(e=>s.case_id+': '+e));if(bad.length){{$('#errors').textContent=bad.join('\\n');return}}const out={{schema_version:'visual-gate-human-export-v1',source_manifest_sha256:DATA.source_manifest_sha256,exported_at:new Date().toISOString(),rows}};const blob=new Blob([JSON.stringify(out,null,2)],{{type:'application/json'}}),a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='16_04_visual_gate_decisions.json';a.click();URL.revokeObjectURL(a.href)}};
$('#import').onclick=()=>$('#file').click();$('#file').onchange=async e=>{{const x=JSON.parse(await e.target.files[0].text());if(x.source_manifest_sha256!==DATA.source_manifest_sha256)throw Error('manifest hash mismatch');saved=Object.fromEntries(x.rows.map(r=>[r.case_id,r]));persist();render()}};persist();render();
</script></html>'''


class _Parser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.images: list[str] = []
        self.events: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "img" and values.get("src"):
            self.images.append(values["src"])
        self.events.extend(name for name, _ in attrs if name.lower().startswith("on"))


def _validate_manifest(run: Path) -> tuple[dict, dict]:
    manifest_path = run / "16_04_04_review_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("schema_version") != RUNNER_VERSION:
        raise ValueError("unsupported visual-gate UI manifest")
    bound = {
        "16_04_00_visual_gate_ui.py": manifest.get("runner_sha256"),
        "16_04_00_visual_gate_review.schema.json": manifest.get("schema_sha256"),
        "16_04_01_review_payload.json": manifest.get("payload_sha256"),
        "16_04_03_visual_gate_review.html": manifest.get("html_sha256"),
    }
    for name, digest in bound.items():
        path = run / name
        if not path.is_file() or _sha(path) != digest:
            raise ValueError(f"visual-gate UI binding changed: {name}")
    payload = json.loads((run / "16_04_01_review_payload.json").read_text())
    if (payload.get("source_manifest_sha256") != manifest.get("source_manifest_sha256")
            or len(payload.get("cases", [])) != manifest.get("candidate_count")):
        raise ValueError("visual-gate payload inventory changed")
    case_ids = [item.get("case_id") for item in payload["cases"]]
    if len(case_ids) != len(set(case_ids)) or case_ids != manifest.get("case_ids"):
        raise ValueError("visual-gate case inventory changed")
    distribution_path = _workspace_path(manifest.get("distribution", {}).get("path", ""))
    if _sha(distribution_path) != manifest.get("distribution", {}).get("sha256"):
        raise ValueError("visual-gate source distribution changed")
    for binding in manifest.get("translations", []):
        translation_path = _workspace_path(binding.get("path", ""))
        if _sha(translation_path) != binding.get("sha256"):
            raise ValueError("visual-gate curator translation changed")
    payload_assets = []
    for case in payload["cases"]:
        if hashlib.sha256(case.get("problem_statement", "").encode()).hexdigest() != case.get(
                "problem_statement_sha256"):
            raise ValueError("visual-gate problem statement binding changed")
        expected_candidate = _json_hash({
            "case_id": case["case_id"], "source_route": case["source_route"],
            "problem_statement_sha256": case["problem_statement_sha256"],
            "assets": [{"asset_id": item["asset_id"], "source_ids": item["source_ids"]}
                       for item in case["assets"]],
            "change_scale": case["change_scale"],
            "source_bindings": case["source_bindings"],
        })
        if expected_candidate != case.get("candidate_binding_sha256"):
            raise ValueError("visual-gate candidate binding changed")
        for name, value in case["source_bindings"].items():
            if name.endswith("_sha256"):
                continue
            digest = case["source_bindings"].get(name + "_sha256")
            path = _workspace_path(value)
            if not digest or _sha(path) != digest:
                raise ValueError(f"visual-gate upstream binding changed: {name}")
        payload_assets.extend({"case_id": case["case_id"],
                               "asset_id": item["asset_id"],
                               "path": item["path"], "sha256": item["sha256"]}
                              for item in case["assets"])
    if payload_assets != manifest.get("assets"):
        raise ValueError("visual-gate manifest asset inventory changed")
    expected_source_manifest = _json_hash({
        "distribution": manifest["distribution"],
        "translations": manifest.get("translations", []),
        "cases": [{"case_id": item["case_id"],
                   "candidate_binding_sha256": item["candidate_binding_sha256"]}
                  for item in payload["cases"]],
        "assets": manifest["assets"],
    })
    if expected_source_manifest != manifest.get("source_manifest_sha256"):
        raise ValueError("visual-gate source manifest identity changed")
    for item in manifest.get("assets", []):
        path = (run / item["path"]).resolve(strict=True)
        if (not path.is_relative_to(run) or not path.is_file()
                or _sha(path) != item["sha256"] or item["sha256"] != item["asset_id"]):
            raise ValueError("visual-gate asset binding changed")
    return manifest, payload


def render(distribution_path: Path, output: Path) -> dict:
    distribution_path = distribution_path.resolve(strict=True)
    output = output.resolve()
    if output.exists():
        raise ValueError(f"visual-gate output exists: {output}")
    distribution = json.loads(distribution_path.read_text())
    if distribution.get("schema_version") == "visual-review-unified-index-v1":
        from report_pipeline.unified_visual_review import render_index
        return render_index(distribution_path, output)
    if distribution.get("schema_version") != "visual-category-distribution-v3":
        raise ValueError("visual-gate input is not a V3 category distribution")
    rows = [item for item in distribution.get("rows", []) if item.get("counted") is True]
    if not rows or len({item.get("case_id") for item in rows}) != len(rows):
        raise ValueError("visual-gate counted candidate inventory is empty or duplicated")
    classification_paths = [_workspace_path(item["path"])
                            for item in distribution.get("classifications", [])]
    for item, path in zip(distribution.get("classifications", []), classification_paths):
        if _sha(path) != item.get("sha256"):
            raise ValueError("visual-gate classification binding changed")
    runs = _classification_runs(classification_paths)
    records: dict[str, tuple[dict, Path]] = {}
    for classification_path, _, manifest in runs:
        for record in manifest["records"]:
            records[record["case_id"]] = (record, classification_path)
    if any(row["case_id"] not in records for row in rows):
        raise ValueError("visual-gate counted candidate lacks a V3 record")
    translations_binding = translation_bindings(distribution_path)
    translations = _translation_index(translations_binding)

    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".16_04_visual_gate_", dir=output.parent))
    try:
        shutil.copyfile(Path(__file__), staging / "16_04_00_visual_gate_ui.py")
        shutil.copyfile(SCHEMA, staging / "16_04_00_visual_gate_review.schema.json")
        cases, assets = [], []
        for index, row in enumerate(rows, 1):
            record, classification_path = records[row["case_id"]]
            case, copied = _case_payload(
                row, record, staging, index, classification_path, translations)
            cases.append(case)
            assets.extend(copied)
        distribution_binding = {
            "path": _portable(distribution_path, WORKSPACE_ROOT),
            "sha256": _sha(distribution_path),
        }
        payload = {
            "schema_version": "visual-gate-review-payload-v1",
            "boundary": {
                "machine_candidates_are_not_human_approved": True,
                "review_scope": "visual_necessity_and_leakage_only",
                "f2p_p2p_gate_excluded": True,
                "browser_export_cannot_promote_task": True,
            },
            "distribution": distribution_binding,
            "translations": translations_binding,
            "source_manifest_sha256": "PENDING",
            "cases": cases,
        }
        # The manifest identity excludes its own hash and binds the immutable
        # source inventory used by browser exports.
        source_manifest_sha256 = _json_hash({
            "distribution": distribution_binding,
            "translations": translations_binding,
            "cases": [{"case_id": item["case_id"],
                       "candidate_binding_sha256": item["candidate_binding_sha256"]}
                      for item in cases],
            "assets": assets,
        })
        payload["source_manifest_sha256"] = source_manifest_sha256
        write_json(staging / "16_04_01_review_payload.json", payload)
        page = _page(payload, source_manifest_sha256)
        (staging / "16_04_03_visual_gate_review.html").write_text(page, encoding="utf-8")
        manifest = {
            "schema_version": RUNNER_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "ready_for_visual_human_review",
            "source_manifest_sha256": source_manifest_sha256,
            "distribution": distribution_binding,
            "translations": translations_binding,
            "runner_sha256": _sha(staging / "16_04_00_visual_gate_ui.py"),
            "schema_sha256": _sha(staging / "16_04_00_visual_gate_review.schema.json"),
            "payload_sha256": _sha(staging / "16_04_01_review_payload.json"),
            "html_sha256": _sha(staging / "16_04_03_visual_gate_review.html"),
            "candidate_count": len(cases),
            "case_ids": [item["case_id"] for item in cases],
            "category_counts": {category: sum(item["category"] == category for item in cases)
                                for category in COUNTED_CATEGORIES},
            "assets": assets,
            "boundary": payload["boundary"],
        }
        write_json(staging / "16_04_04_review_manifest.json", manifest)
        # Static HTML/JS and all bindings must pass while publication is still
        # private.  Only then expose the stable output directory.
        audit(staging)
        os.replace(staging, output)
        audit_result = audit(output)
        return {**manifest, "output": str(output), "audit": audit_result}
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def validate_human_export(run: Path, decisions: Path) -> dict:
    import jsonschema
    manifest, payload = _validate_manifest(run.resolve(strict=True))
    value = json.loads(decisions.resolve(strict=True).read_text())
    jsonschema.validate(value, json.loads((run / "16_04_00_visual_gate_review.schema.json").read_text()))
    if value["source_manifest_sha256"] != manifest["source_manifest_sha256"]:
        raise ValueError("human export belongs to a different visual-gate manifest")
    by_case = {item["case_id"]: item for item in payload["cases"]}
    seen = set()
    counts = {"keep": 0, "exclude": 0, "needs_review": 0}
    for row in value["rows"]:
        case = by_case.get(row["case_id"])
        if case is None or row["case_id"] in seen:
            raise ValueError("human export contains unknown or duplicate case")
        seen.add(row["case_id"])
        if row["candidate_binding_sha256"] != case["candidate_binding_sha256"]:
            raise ValueError("human export candidate binding changed")
        if row["source_route"] != case["source_route"]:
            raise ValueError("human export source route changed")
        expected_assets = [item["asset_id"] for item in case["assets"]]
        if [item["asset_id"] for item in row["images"]] != expected_assets:
            raise ValueError("human export image inventory changed")
        if row["decision"] == "keep":
            visible = [item for item in row["images"] if item["solver_visible"]]
            if (not row["problem_statement_leak_free"]
                    or row["text_only_sufficient"] != "no"
                    or row["ocr_replaceable"] != "no"
                    or not row["non_text_visual_fact"].strip() or not visible):
                raise ValueError("kept visual candidate did not pass necessity/leakage fields")
            for item in visible:
                if (item["role"] in {"after_only", "before_after_composite", "unclear"}
                        or item["contains_fixed_after"]
                        or item["contains_solution_evidence"] or item["crop_required"]):
                    raise ValueError("kept solver-visible image is unsafe or unresolved")
        counts[row["decision"]] += 1
    return {"schema_version": "visual-gate-human-export-audit-v1", "status": "passed",
            "reviewed_count": len(seen), "unreviewed_count": len(by_case) - len(seen),
            "counts": counts, "decisions_sha256": _sha(decisions)}


def audit(run: Path, decisions: Path | None = None) -> dict:
    run = run.resolve(strict=True)
    manifest, payload = _validate_manifest(run)
    page_path = run / "16_04_03_visual_gate_review.html"
    text = page_path.read_text()
    parser = _Parser()
    parser.feed(text)
    if parser.events or any(marker in text for marker in ("fetch(", "XMLHttpRequest", "WebSocket")):
        raise ValueError("visual-gate review page is not offline-safe")
    # Images are created dynamically; their complete inventory is verified via
    # the payload and manifest rather than only static HTML tags.
    payload_assets = [item for case in payload["cases"] for item in case["assets"]]
    if len(payload_assets) != len(manifest["assets"]):
        raise ValueError("visual-gate payload asset count changed")
    forbidden = [marker for marker in ("第一项人工核验", "第二项人工核验") if marker in text]
    if forbidden:
        raise ValueError("legacy combined human gate leaked into visual-only UI")
    scripts = re.findall(r"<script>(.*?)</script>", text, re.DOTALL)
    node = shutil.which("node")
    if node:
        for script in scripts:
            subprocess.run([node, "--check"], input=script.encode(), capture_output=True,
                           check=True, timeout=10)
    result = {
        "schema_version": "visual-gate-ui-audit-v1", "status": "passed",
        "run": str(run), "manifest_sha256": _sha(run / "16_04_04_review_manifest.json"),
        "candidate_count": len(payload["cases"]), "asset_count": len(payload_assets),
        "offline": True, "inline_event_attribute_count": len(parser.events),
        "human_export": validate_human_export(run, decisions) if decisions else None,
    }
    write_json(run / "16_04_05_static_audit.json", result)
    return result
