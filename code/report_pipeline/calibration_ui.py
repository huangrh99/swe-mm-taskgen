"""Build and audit a local two-gate human-calibration interface."""

from __future__ import annotations

import base64
import hashlib
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
import subprocess

from report_pipeline.calibration import task_directory_checksum


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _AuditParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.images: list[str] = []
        self.event_attributes = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        self.event_attributes += sum(name.lower().startswith("on") for name, _ in attrs)
        if tag.lower() == "img" and values.get("src"):
            self.images.append(values["src"] or "")


def _load_inputs(
    dossier_path: Path,
    manifest_path: Path,
    measurement_path: Path,
    task_path: Path,
    test_context_path: Path,
) -> tuple[dict, dict, dict, dict, dict]:
    dossier = json.loads(dossier_path.read_text())
    manifest = json.loads(manifest_path.read_text())
    measured = json.loads(measurement_path.read_text())
    measurement = measured.get("measurement", measured)
    test_context = json.loads(test_context_path.read_text())
    if manifest.get("candidate_id") != dossier.get("candidate_id"):
        raise ValueError("dossier and test manifest candidates differ")
    expected = [(item["test_id"], item["class"]) for item in manifest["tests"]]
    observed = [(item["test_id"], item["class"]) for item in measurement["transitions"]]
    if expected != observed or not measurement.get("all_transitions_match"):
        raise ValueError("measurement does not match the frozen test inventory")
    context_inventory = [
        (item.get("test_id"), item.get("class")) for item in test_context.get("tests", [])
    ]
    if (test_context.get("candidate_id") != dossier.get("candidate_id")
            or test_context.get("source_test_manifest_sha256") != _sha(manifest_path)
            or context_inventory != expected):
        raise ValueError("human test context does not match the frozen test inventory")
    packet_path = Path(dossier["source_bindings"]["packet_path"])
    if _sha(packet_path) != dossier["source_bindings"]["packet_sha256"]:
        raise ValueError("text-only packet binding changed")
    packet = json.loads(packet_path.read_text())
    if packet.get("case_id") != dossier.get("candidate_id"):
        raise ValueError("text-only packet candidate differs")
    if task_path.name != dossier.get("candidate_id"):
        raise ValueError("formal task directory candidate differs")
    return dossier, manifest, measurement, packet, test_context


def _seed(
    dossier_path: Path,
    manifest_path: Path,
    measurement_path: Path,
    task_path: Path,
    test_context_path: Path,
    dossier: dict,
) -> dict:
    return {
        "schema_version": "dual-human-calibration-v2",
        "candidate_id": dossier["candidate_id"],
        "dossier_sha256": _sha(dossier_path),
        "test_manifest_sha256": _sha(manifest_path),
        "test_review_context_sha256": _sha(test_context_path),
        "measurement_sha256": _sha(measurement_path),
        "task_directory_checksum": task_directory_checksum(task_path),
        "multimodal_necessity": {
            "state": "pending",
            "reviewer": None,
            "reason": None,
            "reviewed_at": None,
            "text_only_sufficiency": None,
            "ocr_replaceable": None,
            "non_text_visual_fact": None,
            "evidence_asset_ids": [],
            "text_only_notes": None,
            "text_first_recorded_at": None,
            "images_revealed_at": None,
        },
        "f2p_p2p_semantic_validity": {
            "state": "pending",
            "reviewer": None,
            "reason": None,
            "reviewed_at": None,
            "coverage": None,
            "missing_behaviors": None,
            "test_reviews": [],
        },
    }


def render(
    dossier_path: Path,
    manifest_path: Path,
    measurement_path: Path,
    task_path: Path,
    test_context_path: Path,
    output: Path,
    source_scope_path: Path | None = None,
    queue_path: Path | None = None,
) -> dict:
    dossier_path = dossier_path.resolve()
    manifest_path = manifest_path.resolve()
    measurement_path = measurement_path.resolve()
    task_path = task_path.resolve()
    test_context_path = test_context_path.resolve()
    output = output.resolve()
    source_scope_path = source_scope_path.resolve() if source_scope_path else None
    queue_path = queue_path.resolve() if queue_path else None
    dossier, manifest, measurement, packet, test_context = _load_inputs(
        dossier_path, manifest_path, measurement_path, task_path, test_context_path
    )
    archive_path = Path(dossier["source_bindings"]["archive_path"]).resolve()
    if _sha(archive_path) != dossier["source_bindings"]["archive_sha256"]:
        raise ValueError("source archive binding changed")
    archive = json.loads(archive_path.read_text())
    pull_request = archive["sections"]["pull_request"]["data"]
    closing_issues = archive["sections"]["closing_issues"]["items"]
    if pull_request.get("number") != int(dossier["candidate_id"].rsplit("-", 1)[-1]):
        raise ValueError("source archive pull request differs")
    seed = _seed(dossier_path, manifest_path, measurement_path, task_path,
                 test_context_path, dossier)
    output.parent.mkdir(parents=True, exist_ok=True)

    verifier_path = Path(dossier["source_bindings"]["verifier_path"]).resolve()
    if _sha(verifier_path) != dossier["source_bindings"]["verifier_sha256"]:
        raise ValueError("text-only verifier binding changed")
    text_verifier = json.loads(verifier_path.read_text())
    visual_binding = text_verifier.get("visual_verifier", {})
    visual_verifier_path = Path(visual_binding["result_path"]).resolve()
    if (_sha(visual_verifier_path) != visual_binding.get("result_sha256")
            or visual_verifier_path != Path(
                dossier["visual_admission"]["raw_model_evidence"]
            ).resolve()):
        raise ValueError("visual verifier binding changed")
    visual_verifier = json.loads(visual_verifier_path.read_text())

    source_scope = None
    source_scope_manifest_path = None
    if source_scope_path:
        scope_manifests = list(source_scope_path.parent.glob("*_run_manifest.json"))
        if len(scope_manifests) != 1:
            raise ValueError("source-scope verifier run manifest is missing or ambiguous")
        source_scope_manifest_path = scope_manifests[0]
        source_scope_manifest = json.loads(source_scope_manifest_path.read_text())
        if (source_scope_manifest.get("candidate_id") != dossier["candidate_id"]
                or Path(source_scope_manifest.get("result", "")).resolve() != source_scope_path
                or source_scope_manifest.get("result_sha256") != _sha(source_scope_path)):
            raise ValueError("source-scope verifier binding changed")
        source_scope = json.loads(source_scope_path.read_text())

    queue = None
    queue_index = 0
    queue_navigation = ""
    if queue_path:
        queue = json.loads(queue_path.read_text())
        cases = queue.get("cases", [])
        ids = [item.get("candidate_id") for item in cases]
        if len(ids) != len(set(ids)) or dossier["candidate_id"] not in ids:
            raise ValueError("invalid human-calibration queue")
        queue_index = ids.index(dossier["candidate_id"])
        previous = cases[queue_index - 1] if queue_index else None
        following = cases[queue_index + 1] if queue_index + 1 < len(cases) else None

        def navigation_control(label: str, item: dict | None, disabled_reason: str) -> str:
            if item:
                target = (queue_path.parent / item["review_html"]).resolve()
                relative = target.relative_to(output.parent, walk_up=True).as_posix()
                return (f'<a class="button" href="{html.escape(relative)}" '
                        f'title="{html.escape(item["candidate_id"])}">{label}</a>')
            return f'<button disabled title="{html.escape(disabled_reason)}">{label}</button>'

        pool_count = queue.get("candidate_pool_count", len(cases))
        pool_link = queue.get("candidate_pool_html")
        if pool_link:
            pool_target = (queue_path.parent / pool_link).resolve()
            if not pool_target.is_file():
                raise ValueError("human-calibration candidate-pool page is missing")
            pool_href = pool_target.relative_to(output.parent, walk_up=True).as_posix()
            pool_anchor = f'<a href="{html.escape(pool_href)}">查看全部 {pool_count} 个初筛候选</a>'
        else:
            pool_anchor = f'初筛候选共 {pool_count} 个'
        queue_navigation = (
            '<nav class="queue-nav" aria-label="题目切换">'
            f'{navigation_control("← 上一题", previous, "当前已经是第一题")} '
            f'<b>双重核验队列：第 {queue_index + 1}/{len(cases)} 题</b> '
            f'{navigation_control("下一题 →", following, "目前没有下一题完成 dossier、F2P/P2P 和前后测量")} '
            f'<span>{pool_anchor}；目前只有 {len(cases)} 题材料完整，可进入双重人工核验。</span>'
            '</nav>'
        )

    text_sources = "".join(
        f'<section class="source"><h3><a href="{html.escape(item["url"])}">'
        f'Issue #{html.escape(item["url"].rstrip("/").rsplit("/", 1)[-1])}</a> · '
        f'{"标题" if item["field"] == "title" else "正文"} '
        f'<small>（由 PR #{pull_request["number"]} 明确关闭）</small></h3>'
        f'<pre>{html.escape(item["text"])}</pre></section>'
        for item in packet["problem_sources"]
    )
    issue_links = "、".join(
        f'<a href="{html.escape(item["url"])}">Issue #{item["number"]}</a>'
        for item in closing_issues
    )
    relationship = (
        '<section class="relationship"><b>编号关系：</b>'
        f'<a href="{html.escape(pull_request["html_url"])}">PR #{pull_request["number"]}</a> '
        f'是本题采用的已合并修复 PR；它在正文中明确写了 Closes {issue_links}。'
        '下面两项是该 PR 要解决的 Issue（需求与图片来源），不是另外两个 PR。'
        '<br><small>Benchmark 使用 PR 合入前的代码作为待修复基线，并把关联 Issue 的文字和安全图片整理为 Agent 题面。</small>'
        '</section>'
    )
    image_rows = []
    for asset in dossier["leakage_policy"]["safe_agent_assets"]:
        source = Path(asset["local_path"]).resolve()
        if not source.is_file() or _sha(source) != asset["asset_id"]:
            raise ValueError("agent-safe image binding changed")
        relative = source.relative_to(output.parent, walk_up=True).as_posix()
        asset_id = html.escape(asset["asset_id"])
        image_rows.append(
            f'<figure><img loading="lazy" src="{html.escape(relative)}" '
            f'alt="Issue evidence {asset_id[:12]}"><figcaption>'
            f'<label><input type="checkbox" data-asset-id="{asset_id}"> '
            f'引用此图作为必要性证据</label><code>{asset_id}</code><br>'
            f'{html.escape(", ".join(asset["source_ids"]))}</figcaption></figure>'
        )
    transition_by_id = {item["test_id"]: item for item in measurement["transitions"]}
    context_by_id = {item["test_id"]: item for item in test_context["tests"]}
    test_rows = []
    for item in manifest["tests"]:
        transition = transition_by_id[item["test_id"]]
        context = context_by_id[item["test_id"]]
        assertions = "<br>".join(html.escape(value) for value in item["contains_all"])
        test_id = html.escape(item["test_id"])
        test_rows.append(
            f'<tr data-test-id="{test_id}" data-test-class="{html.escape(item["class"])}">'
            f'<td class="test-summary"><b>{html.escape(context["title"])}</b><br>'
            f'{html.escape(context["purpose"])}<br><small>{html.escape(context["requirement_source"])}</small><br>'
            f'<code>{test_id}</code></td>'
            f'<td><b>{html.escape(item["class"])}</b> · {html.escape(transition["actual"])} '
            f'· {"✓" if transition["matches"] else "✗"}<br>'
            f'<small>{html.escape(context["classification_reason"])}</small></td>'
            f'<td>{html.escape(context["observed_check"])}'
            f'<details><summary>查看技术断言</summary><div class="assertions">{assertions}</div></details></td><td>'
            '<select class="test-decision"><option value="unclear">待判断</option>'
            '<option value="valid">有效</option><option value="invalid">无效</option></select>'
            '<textarea class="test-reason" rows="2" placeholder="说明它验证的需求或回归"></textarea>'
            '</td></tr>'
        )
    payload = {
        "seed": seed,
        "test_inventory": [
            {"test_id": item["test_id"], "class": item["class"]}
            for item in manifest["tests"]
        ],
        "asset_ids": [
            item["asset_id"] for item in dossier["leakage_policy"]["safe_agent_assets"]
        ],
    }
    encoded = base64.b64encode(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    ).decode()
    instruction = (task_path / "instruction.md").read_text()

    verifier_cards = [
        ("图片语义 Verifier", visual_verifier.get("decision", {}).get("bucket", "unknown"),
         visual_verifier.get("decision", {}).get("reason_code", "")),
        ("无图可修复性 Verifier", text_verifier.get("text_decision", {}).get("bucket", "unknown"),
         text_verifier.get("text_decision", {}).get("reason_code", "")),
    ]
    if source_scope:
        verifier_cards.append(
            ("Parent Issue 范围 Verifier", source_scope.get("overall_decision", "unknown"),
             f'confidence={source_scope.get("confidence", "unknown")}')
        )
    verifier_summary = "".join(
        f'<article><b>{html.escape(label)}</b><strong>{html.escape(decision)}</strong>'
        f'<span>{html.escape(reason)}</span></article>'
        for label, decision, reason in verifier_cards
    )
    verifier_documents = [
        ("图片语义 Verifier · 完整结构化输出", visual_verifier_path, visual_verifier),
        ("无图可修复性 Verifier · 完整结构化输出", verifier_path, text_verifier),
    ]
    if source_scope and source_scope_path:
        verifier_documents.append(
            ("Parent Issue 范围 Verifier · 完整结构化输出", source_scope_path, source_scope)
        )
    verifier_output = "".join(
        '<details class="verifier-output">'
        f'<summary>{html.escape(label)}</summary>'
        f'<div class="evidence-link"><a href="{path.as_uri()}">打开原始 JSON</a> · '
        f'<code>sha256:{_sha(path)}</code></div>'
        f'<pre>{html.escape(json.dumps(document, ensure_ascii=False, indent=2))}</pre>'
        '</details>'
        for label, path, document in verifier_documents
    )
    source_links = [
        ("GitHub PR", dossier.get("url")),
        ("完整来源档案", dossier["source_bindings"].get("archive_path")),
        ("图片语义 Verifier JSON", str(visual_verifier_path)),
        ("无图可修复性 Verifier JSON", str(verifier_path)),
        ("Parent Issue 范围 Verifier JSON", str(source_scope_path) if source_scope_path else None),
    ]
    links = "".join(
        f'<a href="{html.escape(str(value if str(value).startswith("http") else Path(value).resolve().as_uri()))}">{html.escape(label)}</a>'
        for label, value in source_links
        if value
    )
    page = _TEMPLATE
    replacements = {
        "__TITLE__": html.escape(
            f'{pull_request["base"]["repo"]["full_name"]} · PR #{pull_request["number"]}'
        ),
        "__QUEUE_NAVIGATION__": queue_navigation,
        "__RELATIONSHIP__": relationship,
        "__PAYLOAD__": encoded,
        "__TEXT_SOURCES__": text_sources,
        "__IMAGES__": "".join(image_rows),
        "__TEST_ROWS__": "".join(test_rows),
        "__INSTRUCTION__": html.escape(instruction),
        "__SOURCE_LINKS__": links,
        "__VERIFIER_SUMMARY__": verifier_summary,
        "__VERIFIER_OUTPUT__": verifier_output,
        "__VISUAL_MODEL_DECISION__": html.escape(dossier["visual_admission"]["decision"]),
        "__VISUAL_MODEL_REASON__": html.escape(dossier["visual_admission"]["reason"]),
        "__ORACLE_KIND__": html.escape(measurement.get("oracle_kind", "unknown")),
    }
    for marker, value in replacements.items():
        page = page.replace(marker, value)
    output.write_text(page, encoding="utf-8")
    seed_path = output.with_name(output.stem + "_seed.json")
    seed_path.write_text(json.dumps(seed, ensure_ascii=False, indent=2) + "\n")
    result = {
        "schema_version": "dual-human-calibration-ui-manifest-v1",
        "status": "ready_for_two_independent_human_gates",
        "candidate_id": dossier["candidate_id"],
        "html": str(output),
        "html_sha256": _sha(output),
        "seed": str(seed_path),
        "seed_sha256": _sha(seed_path),
        "dossier_sha256": seed["dossier_sha256"],
        "test_manifest_sha256": seed["test_manifest_sha256"],
        "test_review_context_sha256": seed["test_review_context_sha256"],
        "measurement_sha256": seed["measurement_sha256"],
        "task_directory_checksum": seed["task_directory_checksum"],
        "asset_count": len(payload["asset_ids"]),
        "test_count": len(payload["test_inventory"]),
        "model_calls_added": 0,
        "visual_verifier_sha256": _sha(visual_verifier_path),
        "text_verifier_sha256": _sha(verifier_path),
        "source_scope_verifier_sha256": _sha(source_scope_path) if source_scope_path else None,
        "queue_sha256": _sha(queue_path) if queue_path else None,
        "queue_position": queue_index + 1 if queue else None,
        "queue_size": len(queue["cases"]) if queue else None,
    }
    manifest_output = output.with_name(output.stem + "_manifest.json")
    manifest_output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    result["manifest"] = str(manifest_output)
    return result


def audit(output: Path, record_path: Path) -> dict:
    output = output.resolve()
    text = output.read_text()
    parser = _AuditParser()
    parser.feed(text)
    match = re.search(r'<template id="calibration-payload">([^<]+)</template>', text)
    if not match:
        raise ValueError("calibration payload missing")
    payload = json.loads(base64.b64decode(match.group(1)))
    from report_pipeline.paths import REPORT_ROOT
    workspace = REPORT_ROOT.resolve()
    assets = []
    for reference in parser.images:
        candidate = (output.parent / reference).resolve()
        if not candidate.is_relative_to(workspace) or not candidate.is_file():
            raise ValueError(f"missing or unsafe calibration image: {reference}")
        assets.append({"reference": reference, "sha256": _sha(candidate)})
    if parser.event_attributes:
        raise ValueError("inline event attributes are forbidden")
    if "fetch(" in text or "XMLHttpRequest" in text or "WebSocket" in text:
        raise ValueError("calibration page must be offline")
    required = [
        "第一项人工核验：视觉输入是否必要",
        "第二项人工核验：F2P/P2P 测试是否有效",
        "保存无图判断并揭示原图",
        "核验人",
        "导出人工核验 JSON",
        "载入已有标注",
    ]
    missing = [marker for marker in required if marker not in text]
    if missing or len(assets) != len(payload["asset_ids"]):
        raise ValueError(f"calibration UI audit failed: missing={missing}, images={len(assets)}")
    scripts = re.findall(r"<script>(.*?)</script>", text, re.DOTALL)
    node = shutil.which("node")
    if node:
        for script in scripts:
            subprocess.run(
                [node, "--check"], input=script.encode(), capture_output=True,
                check=True, timeout=10
            )
    result = {
        "schema_version": "dual-human-calibration-ui-audit-v1",
        "status": "passed",
        "html": str(output),
        "html_sha256": _sha(output),
        "candidate_id": payload["seed"]["candidate_id"],
        "asset_count": len(assets),
        "test_count": len(payload["test_inventory"]),
        "event_attributes": parser.event_attributes,
        "offline": True,
        "review_script_syntax_checked": bool(node),
        "browser_interaction_verified": False,
    }
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


_TEMPLATE = r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>__TITLE__ · 人工核验</title>
<style>
:root{--bg:#f5f6f8;--panel:#fff;--ink:#17191c;--muted:#626971;--line:#d8dce1;--accent:#14634f;--warn:#9a6700;--bad:#b42318}
*{box-sizing:border-box}body{margin:0;overflow-x:hidden;background:var(--bg);color:var(--ink);font:13px/1.45 system-ui,sans-serif}
header{position:sticky;top:0;z-index:5;background:#f5f6f8f2;border-bottom:1px solid var(--line);padding:10px 18px}h1{font-size:19px;margin:0 0 6px}
.toolbar,.status,.links,.queue-nav{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.status{margin-left:auto}.layout{max-width:1420px;margin:auto;padding:14px 18px}.gate{background:var(--panel);border:1px solid var(--line);border-radius:8px;margin-bottom:12px;padding:14px}.queue-nav{margin-top:8px;padding-top:8px;border-top:1px solid var(--line)}.queue-nav span{color:var(--muted)}
.gate-head{display:flex;gap:10px;align-items:start;justify-content:space-between}.gate h2{font-size:16px;margin:0 0 4px}.muted{color:var(--muted)}.warn{color:var(--warn)}.bad{color:var(--bad)}.ok{color:var(--accent)}
.cols{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(320px,.75fr);gap:14px}.review{border-left:1px solid var(--line);padding-left:14px}
label{display:block;font-weight:600;margin:8px 0 3px}textarea,input,select{width:100%;font:inherit;padding:7px;border:1px solid var(--line);border-radius:5px;background:var(--panel);color:var(--ink)}
textarea{resize:vertical}button,.button{font:inherit;padding:7px 10px;border:1px solid var(--line);border-radius:5px;background:var(--panel);color:var(--ink);cursor:pointer}button.primary{background:var(--accent);color:#fff;border-color:var(--accent)}button:disabled{opacity:.55;cursor:not-allowed}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--bg);padding:9px;border-radius:5px;margin:5px 0}.source h3{font-size:13px;margin:8px 0 2px}.hidden{display:none}
.assets{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:8px;margin-top:8px}figure{margin:0;border:1px solid var(--line);padding:7px;border-radius:5px}img{width:100%;height:210px;object-fit:contain;background:#fff}figcaption{overflow-wrap:anywhere}.assets input{width:auto}
.table-wrap{overflow:auto}table{border-collapse:collapse;width:100%;min-width:1080px}th,td{text-align:left;vertical-align:top;padding:8px;border-bottom:1px solid var(--line)}th{position:sticky;top:0;background:var(--panel)}td:nth-child(1){width:330px}td:nth-child(2){width:260px}td:nth-child(3){width:280px}td:nth-child(4){width:250px}.test-summary b{display:inline-block;margin-bottom:3px}.test-summary code{display:inline-block;margin-top:5px}.assertions{margin-top:5px;overflow-wrap:anywhere}td textarea,td select{margin-bottom:4px}small{color:var(--muted)}
.pill{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:2px 7px}.error{margin-top:8px;color:var(--bad);white-space:pre-wrap}.links a{margin-right:8px}.complete{color:var(--accent)}details{margin-top:8px}.final{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.relationship{margin:10px 0 12px;padding:9px 11px;border-left:4px solid var(--accent);background:#eef7f4;border-radius:4px}.relationship a,.source a{color:var(--accent)}
.verifier-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:8px;margin:10px 0}.verifier-grid article{display:flex;flex-direction:column;gap:3px;border:1px solid var(--line);border-radius:6px;padding:9px}.verifier-grid strong{color:var(--accent)}.verifier-grid span,.evidence-link{color:var(--muted);overflow-wrap:anywhere}.verifier-output{border-top:1px solid var(--line);padding-top:8px}.verifier-output summary{font-weight:700;cursor:pointer}.verifier-output pre{border:1px solid var(--line)}
@media(max-width:850px){.cols{grid-template-columns:1fr}.review{border-left:0;border-top:1px solid var(--line);padding:10px 0 0}.layout{padding:10px}.gate-head{display:block}}
</style></head><body>
<template id="calibration-payload">__PAYLOAD__</template>
<header><h1>__TITLE__ · 人工核验</h1><div class="toolbar"><label class="button">载入已有标注<input id="import-file" class="hidden" type="file" accept="application/json"></label><button id="export" class="primary">导出人工核验 JSON</button><span class="status"><span id="gate1-status" class="pill">视觉核验：待核验</span><span id="gate2-status" class="pill">测试核验：待核验</span><b id="eligibility">尚不可进入最终题集</b></span></div>__QUEUE_NAVIGATION__<div id="errors" class="error" role="alert"></div></header>
<main class="layout">
<section class="gate" id="gate1"><div class="gate-head"><div><h2>第一项人工核验：视觉输入是否必要</h2><div class="muted">先保存无图判断，再揭示原图。揭示后无图判断会锁定；若图片信息可以完整转写为文字，则不应认定为必须使用视觉输入。</div></div><div id="visual-model-summary" class="muted hidden">模型初筛：__VISUAL_MODEL_DECISION__ · __VISUAL_MODEL_REASON__</div></div>
__RELATIONSHIP__<div class="cols"><div><h3>问题文字材料（关联 Issue）</h3>__TEXT_SOURCES__<label for="text-only-notes">只看文字时，能否唯一确定需要修改的视觉行为？</label><textarea id="text-only-notes" rows="4"></textarea><button id="reveal-images" type="button">保存无图判断并揭示原图</button><div id="image-panel" class="hidden"><h3>Agent-safe Issue 原图</h3><div class="assets">__IMAGES__</div></div></div>
<div class="review"><label for="text-sufficiency">仅凭文字是否足够</label><select id="text-sufficiency"><option value="">待判断</option><option value="insufficient">不足</option><option value="sufficient">足够</option><option value="unclear">不确定</option></select><label for="ocr-replaceable">图片信息能否被 OCR/文字完整替代</label><select id="ocr-replaceable"><option value="">待判断</option><option value="no">不能</option><option value="yes">可以</option><option value="unclear">不确定</option></select><label for="visual-fact">图片独有、不可由文字恢复的事实</label><textarea id="visual-fact" rows="4"></textarea><label for="visual-reason">核验理由</label><textarea id="visual-reason" rows="4"></textarea><label for="visual-reviewer">核验人</label><input id="visual-reviewer" autocomplete="name"><label for="visual-state">核验结论</label><select id="visual-state"><option value="pending">待核验</option><option value="approved">通过</option><option value="rejected">不通过</option></select></div></div></section>
<div id="post-reveal" class="hidden"><section class="gate" id="verifier-evidence"><div class="gate-head"><div><h2>完整 Verifier 输出（未截断）</h2><div class="muted">这些是模型初筛的完整结构化结果，只作为人工判断的证据，不替代两项人工核验。</div></div></div><div class="verifier-grid">__VERIFIER_SUMMARY__</div>__VERIFIER_OUTPUT__</section>
<section class="gate" id="gate2"><div class="gate-head"><div><h2>第二项人工核验：F2P/P2P 测试是否有效</h2><div class="muted">执行结果符合预期，只能证明测试可以运行；这里还要逐项确认测试是否真正覆盖需求和相关回归。</div></div><div class="muted">执行判定：__ORACLE_KIND__ · pixel oracle=false</div></div>
<div class="table-wrap"><table><thead><tr><th>这项测试在验证什么</th><th>为何属于 F2P/P2P</th><th>实际如何检测</th><th>你的判断</th></tr></thead><tbody>__TEST_ROWS__</tbody></table></div>
<div class="cols"><div><details open><summary>Agent 收到的完整题面（未截断）</summary><pre>__INSTRUCTION__</pre></details><div class="links">__SOURCE_LINKS__</div></div><div class="review"><label for="coverage">整体需求与回归覆盖</label><select id="coverage"><option value="">待判断</option><option value="complete">完整</option><option value="incomplete">不完整</option><option value="unclear">不确定</option></select><label for="missing-behaviors">仍未覆盖的行为（完整时留空）</label><textarea id="missing-behaviors" rows="3"></textarea><label for="test-reason">整体核验理由</label><textarea id="test-reason" rows="4"></textarea><label for="test-reviewer">核验人</label><input id="test-reviewer" autocomplete="name"><label for="test-state">核验结论</label><select id="test-state"><option value="pending">待核验</option><option value="approved">通过</option><option value="rejected">不通过</option></select></div></div></section>
<section class="gate final"><b>准入规则：</b><span>只有两项人工核验都通过，任务才可进入最终题集。</span><span id="final-state" class="pill">暂不准入</span></section>
<details id="export-preview" class="gate hidden"><summary>导出 JSON 预览</summary><pre id="json-preview"></pre></details></div></main>
<script>
const payloadNode=document.getElementById('calibration-payload');
const payload=JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(payloadNode.content.textContent.trim()),c=>c.charCodeAt(0))));
const storageKey='dual-human-calibration:'+location.pathname+':'+payload.seed.dossier_sha256+':'+payload.seed.task_directory_checksum;
const ids=['text-only-notes','text-sufficiency','ocr-replaceable','visual-fact','visual-reason','visual-reviewer','visual-state','coverage','missing-behaviors','test-reason','test-reviewer','test-state'];
let phase={};
function saved(){try{return JSON.parse(localStorage.getItem(storageKey)||'{}')}catch{return {}}}
function persist(){const value={text_first_recorded_at:phase.text_first_recorded_at||null,images_revealed_at:phase.images_revealed_at||null};ids.forEach(id=>value[id]=document.getElementById(id).value);value.assets=[...document.querySelectorAll('[data-asset-id]:checked')].map(x=>x.dataset.assetId);value.tests=[...document.querySelectorAll('tr[data-test-id]')].map(row=>({test_id:row.dataset.testId,class:row.dataset.testClass,decision:row.querySelector('.test-decision').value,reason:row.querySelector('.test-reason').value}));phase=value;localStorage.setItem(storageKey,JSON.stringify(value));updateStatus()}
function applyPhase(){const revealed=!!phase.images_revealed_at;for(const id of ['image-panel','post-reveal','visual-model-summary'])document.getElementById(id).classList.toggle('hidden',!revealed);document.getElementById('reveal-images').disabled=revealed;document.getElementById('text-only-notes').disabled=revealed;document.getElementById('text-sufficiency').disabled=revealed}
function restore(value){phase=value||{};ids.forEach(id=>{if(value[id]!==undefined)document.getElementById(id).value=value[id]});const assets=new Set(value.assets||[]);document.querySelectorAll('[data-asset-id]').forEach(x=>x.checked=assets.has(x.dataset.assetId));const tests=new Map((value.tests||[]).map(x=>[x.test_id,x]));document.querySelectorAll('tr[data-test-id]').forEach(row=>{const item=tests.get(row.dataset.testId);if(item){row.querySelector('.test-decision').value=item.decision;row.querySelector('.test-reason').value=item.reason}});applyPhase();updateStatus()}
function build(){const now=new Date().toISOString(),visualState=document.getElementById('visual-state').value,testState=document.getElementById('test-state').value;return {...payload.seed,multimodal_necessity:{state:visualState,reviewer:document.getElementById('visual-reviewer').value.trim()||null,reason:document.getElementById('visual-reason').value.trim()||null,reviewed_at:visualState==='pending'?null:now,text_only_sufficiency:document.getElementById('text-sufficiency').value||null,ocr_replaceable:document.getElementById('ocr-replaceable').value||null,non_text_visual_fact:document.getElementById('visual-fact').value.trim()||null,evidence_asset_ids:[...document.querySelectorAll('[data-asset-id]:checked')].map(x=>x.dataset.assetId),text_only_notes:document.getElementById('text-only-notes').value.trim()||null,text_first_recorded_at:phase.text_first_recorded_at||null,images_revealed_at:phase.images_revealed_at||null},f2p_p2p_semantic_validity:{state:testState,reviewer:document.getElementById('test-reviewer').value.trim()||null,reason:document.getElementById('test-reason').value.trim()||null,reviewed_at:testState==='pending'?null:now,coverage:document.getElementById('coverage').value||null,missing_behaviors:document.getElementById('missing-behaviors').value.trim()||null,test_reviews:[...document.querySelectorAll('tr[data-test-id]')].map(row=>({test_id:row.dataset.testId,class:row.dataset.testClass,decision:row.querySelector('.test-decision').value,reason:row.querySelector('.test-reason').value.trim()}))}}}
function validate(value){const errors=[];for(const [name,gate] of [['视觉必要性核验',value.multimodal_necessity],['测试有效性核验',value.f2p_p2p_semantic_validity]])if(gate.state!=='pending'){if(!gate.reason)errors.push(name+'完成时必须填写整体理由');if(!gate.reviewer)errors.push(name+'完成时必须填写核验人')}const v=value.multimodal_necessity;if(v.state!=='pending'&&(!v.text_first_recorded_at||!v.images_revealed_at||!v.text_only_notes||!v.text_only_sufficiency))errors.push('视觉必要性核验前必须先保存无图判断并揭示原图');if(v.state==='approved'){if(v.text_only_sufficiency!=='insufficient')errors.push('视觉必要性核验通过时，必须确认仅凭文字不足');if(v.ocr_replaceable!=='no')errors.push('视觉必要性核验通过时，必须确认图片不能被 OCR/文字完整替代');if(!v.non_text_visual_fact||!v.evidence_asset_ids.length)errors.push('视觉必要性核验通过时，必须填写图片独有事实并引用至少一张图')}const t=value.f2p_p2p_semantic_validity;if(t.state==='approved'){if(t.coverage!=='complete'||t.missing_behaviors)errors.push('测试有效性核验通过时，必须确认覆盖完整且没有未覆盖行为');if(t.test_reviews.length!==payload.test_inventory.length||t.test_reviews.some(x=>x.decision!=='valid'||!x.reason))errors.push('测试有效性核验通过时，必须逐项确认全部冻结测试并填写理由')}return errors}
function stateText(value){return value==='approved'?'通过':value==='rejected'?'不通过':'待核验'}
function updateStatus(){const a=document.getElementById('visual-state').value,b=document.getElementById('test-state').value;document.getElementById('gate1-status').textContent='视觉核验：'+stateText(a);document.getElementById('gate2-status').textContent='测试核验：'+stateText(b);const ok=a==='approved'&&b==='approved';document.getElementById('eligibility').textContent=ok?'可进入最终题集':'尚不可进入最终题集';document.getElementById('eligibility').className=ok?'complete':'';document.getElementById('final-state').textContent=ok?'允许准入':'暂不准入'}
document.querySelectorAll('input,select,textarea').forEach(x=>x.addEventListener('input',persist));
document.getElementById('reveal-images').addEventListener('click',()=>{const notes=document.getElementById('text-only-notes').value.trim(),sufficiency=document.getElementById('text-sufficiency').value;if(!notes||!sufficiency){document.getElementById('errors').textContent='揭示原图前必须填写并保存无图判断';return}const now=new Date().toISOString();phase.text_first_recorded_at=now;persist();phase.images_revealed_at=new Date().toISOString();persist();applyPhase();document.getElementById('errors').textContent=''});
document.getElementById('export').addEventListener('click',()=>{persist();const value=build(),errors=validate(value),rendered=JSON.stringify(value,null,2)+'\n';document.getElementById('errors').textContent=errors.join('\n');if(errors.length)return;document.getElementById('json-preview').textContent=rendered;document.getElementById('export-preview').classList.remove('hidden');const blob=new Blob([rendered],{type:'application/json'}),a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='dual_human_calibration_'+payload.seed.candidate_id+'.json';a.click();URL.revokeObjectURL(a.href)});
document.getElementById('import-file').addEventListener('change',async event=>{const file=event.target.files[0];if(!file)return;try{const value=JSON.parse(await file.text());for(const key of ['schema_version','candidate_id','dossier_sha256','test_manifest_sha256','test_review_context_sha256','measurement_sha256','task_directory_checksum'])if(value[key]!==payload.seed[key])throw new Error('绑定字段不匹配: '+key);for(const gate of [value.multimodal_necessity,value.f2p_p2p_semantic_validity])if(gate.state==='approved'&&!String(gate.reviewer||'').trim())throw new Error('批准记录必须填写核验人');restore({'text-only-notes':value.multimodal_necessity.text_only_notes||'','text-sufficiency':value.multimodal_necessity.text_only_sufficiency||'','ocr-replaceable':value.multimodal_necessity.ocr_replaceable||'','visual-fact':value.multimodal_necessity.non_text_visual_fact||'','visual-reason':value.multimodal_necessity.reason||'','visual-reviewer':value.multimodal_necessity.reviewer||'','visual-state':value.multimodal_necessity.state,'coverage':value.f2p_p2p_semantic_validity.coverage||'','missing-behaviors':value.f2p_p2p_semantic_validity.missing_behaviors||'','test-reason':value.f2p_p2p_semantic_validity.reason||'','test-reviewer':value.f2p_p2p_semantic_validity.reviewer||'','test-state':value.f2p_p2p_semantic_validity.state,text_first_recorded_at:value.multimodal_necessity.text_first_recorded_at||null,images_revealed_at:value.multimodal_necessity.images_revealed_at||null,assets:value.multimodal_necessity.evidence_asset_ids||[],tests:value.f2p_p2p_semantic_validity.test_reviews||[]});document.getElementById('errors').textContent=''}catch(error){document.getElementById('errors').textContent='载入失败：'+error.message}});
restore(saved());
</script></body></html>'''
