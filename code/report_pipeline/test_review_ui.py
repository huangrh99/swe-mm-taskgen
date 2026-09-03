"""Build a static, hash-bound human review page for F2P/P2P test evidence."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import re

from report_pipeline.atomic import write_json


RUNNER_VERSION = "test-review-ui-v1"
PACKET_NAME = "20_11_01_packet.json"
RESULT_NAME = "20_11_06_result.json"
MANIFEST_NAME = "20_11_09_manifest.json"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json_hash(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _display_test_id(test_id: object) -> str:
    if not isinstance(test_id, str):
        return str(test_id or "")
    try:
        parsed = json.loads(test_id)
        if (isinstance(parsed, list) and len(parsed) == 2
                and isinstance(parsed[0], list) and isinstance(parsed[1], str)):
            parts = [str(item) for item in parsed[0] if item]
            return " / ".join([*parts, parsed[1]])
    except json.JSONDecodeError:
        pass
    return test_id


def _observed_transition(item: dict) -> tuple[str | None, str | None, bool]:
    klass = item.get("class") or item.get("observed_type")
    actual = item.get("actual")
    matches = item.get("matches")
    if actual is None:
        baseline = item.get("baseline") or []
        reference = item.get("reference") or []
        if baseline and reference and set(baseline) == {"fail"} and set(reference) == {"pass"}:
            actual, matches = "fail->pass", True
        elif baseline and reference and set(baseline) == {"pass"} and set(reference) == {"pass"}:
            actual, matches = "pass->pass", True
    return klass, actual, matches is True


def _load(path: Path) -> dict:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _bound_file(directory: Path, name: str, binding: dict | None = None) -> tuple[Path, dict]:
    path = (directory / name).resolve(strict=True)
    if not path.is_file() or path.is_symlink() or not path.is_relative_to(directory):
        raise ValueError(f"unsafe verifier artifact: {name}")
    digest = _sha(path)
    if binding and binding.get("sha256") != digest:
        raise ValueError(f"verifier artifact hash changed: {name}")
    return path, _load(path)


def _transition_status(packet: dict, bundles: list[dict]) -> dict:
    existing = packet.get("existing_tests") or {}
    transitions = existing.get("measured_transitions") or []
    repeats = existing.get("repeats_per_state")
    counts = existing.get("measured_counts") or {}
    f2p = counts.get("F2P", 0)
    p2p = counts.get("P2P", 0)
    anomalies = []
    measured_ids = set()
    if not isinstance(repeats, int) or repeats < 3:
        anomalies.append(f"repeats gate 未通过：当前 {repeats!r}，要求至少 3")
    if not isinstance(f2p, int) or f2p < 1:
        anomalies.append("缺少至少一个实测 F2P")
    if not isinstance(p2p, int) or p2p < 1:
        anomalies.append("缺少至少一个实测 P2P")
    rendered_transitions = []
    for item in transitions:
        test_id = item.get("test_id")
        klass, actual, matches = _observed_transition(item)
        expected = item.get("expected") or {
            "F2P": "fail->pass", "P2P": "pass->pass",
        }.get(klass)
        if (not test_id or klass not in {"F2P", "P2P"}
                or actual not in {"fail->pass", "pass->pass"}
                or expected != actual or not matches):
            anomalies.append(f"异常或未分类测试结果：{test_id or '<missing id>'}")
        else:
            measured_ids.add(test_id)
        states = str(actual or "").split("->", 1)
        rendered_transitions.append({
            **item,
            "class": klass,
            "expected": expected,
            "actual": actual,
            "matches": matches,
            "display_name": _display_test_id(test_id),
            "base_status": states[0] if len(states) == 2 else None,
            "gold_status": states[1] if len(states) == 2 else None,
        })
    unmeasured = []
    for bundle in bundles:
        missing = sorted(set(bundle.get("stable_test_ids") or []) - measured_ids)
        if missing:
            unmeasured.append({"bundle_id": bundle.get("bundle_id"), "test_ids": missing})
    if unmeasured:
        anomalies.append("存在尚未完成 base/gold 实测的生成测试 bundle")
    return {
        "f2p": f2p,
        "p2p": p2p,
        "repeats_per_state": repeats,
        "transitions": rendered_transitions,
        "unmeasured_bundles": unmeasured,
        "approval_blockers": anomalies,
        "approval_eligible": not anomalies,
    }


def _test_semantics(packet: dict, coverage: dict, bundles: list[dict]) -> list[dict]:
    existing = packet.get("existing_tests") or {}
    author_added_names = {
        match.group(2) for match in re.finditer(
            r"^\+\s*(?:it|test)\(\s*(['\"])(.*?)\1",
            existing.get("author_test_patch") or "",
            flags=re.MULTILINE,
        )
    }
    coverage_by_test = {}
    for requirement_id, item in coverage.items():
        for test_id in item.get("existing_test_ids") or []:
            coverage_by_test.setdefault(test_id, []).append((requirement_id, item))
    rows = []
    for item in existing.get("measured_transitions") or []:
        test_id = item.get("test_id")
        klass, actual, matches = _observed_transition(item)
        mapped = coverage_by_test.get(test_id, [])
        name = _display_test_id(test_id)
        if mapped:
            purpose = "；".join(dict.fromkeys(
                entry.get("assertion_summary", "") for _, entry in mapped
                if entry.get("assertion_summary")))
        elif klass == "F2P":
            purpose = f"验证修复是否使“{name}”从失败转为通过。"
        elif klass == "P2P":
            purpose = f"回归保护：验证“{name}”在修复前后保持通过。"
        else:
            purpose = f"验证“{name}”对应的可观察行为。"
        source = item.get("source") or "unclassified_existing_test"
        leaf_name = name.rsplit(" / ", 1)[-1]
        if source == "author_or_existing_component_test":
            origin = ("pr_author_test" if leaf_name in author_added_names
                      else "repository_existing_regression_test")
        else:
            origin = "verifier_generated" if source == "vlm_generated_test" else source
        rows.append({
            "test_id": test_id, "display_name": name, "origin": origin,
            "raw_source": source,
            "origin_label": {
                "verifier_generated": "Verifier 生成",
                "pr_author_test": "PR 作者新增测试",
                "repository_existing_regression_test": "仓库既有回归测试",
                "unclassified_existing_test": "来源尚未细分的已有测试",
            }.get(origin, origin),
            "classification": klass or "unclassified",
            "classification_basis": "base_gold_measured" if actual else "unmeasured",
            "actual_transition": actual, "matches": matches,
            "purpose": purpose,
            "requirement_ids": [requirement_id for requirement_id, _ in mapped],
        })
    for bundle in bundles:
        for test_id in bundle.get("stable_test_ids") or []:
            rows.append({
                "test_id": test_id, "display_name": _display_test_id(test_id),
                "origin": "verifier_generated",
                "origin_label": "Verifier 生成",
                "generation_scope": "current_run",
                "classification": bundle.get("predicted_transition") or "unclassified",
                "classification_basis": "verifier_prediction_not_measured",
                "actual_transition": None, "matches": False,
                "purpose": bundle.get("why_assertions_measure_requirements") or (
                    f"验证“{_display_test_id(test_id)}”对应的功能约束。"),
                "requirement_ids": bundle.get("requirement_ids") or [],
                "bundle_id": bundle.get("bundle_id"),
            })
    return rows


def _test_input_groups(packet: dict) -> dict:
    existing = packet.get("existing_tests") or {}
    files = existing.get("files") or []
    generated = existing.get("current_generated_test") or {}
    generated_path = generated.get("path")
    author_paths = {
        match.group(2) for match in re.finditer(
            r"^diff --git a/(.+?) b/(.+?)$",
            existing.get("author_test_patch") or "",
            flags=re.MULTILINE,
        )
    }
    groups = {"repository_context": [], "pr_author_tests": [],
              "verifier_generated_tests": []}
    for item in files:
        path = item.get("path")
        if path == generated_path:
            groups["verifier_generated_tests"].append({
                **item, "generation_scope": "prior_run",
            })
        elif path in author_paths:
            groups["pr_author_tests"].append(item)
        else:
            groups["repository_context"].append(item)
    return groups


def _verifier_generated_files(input_groups: dict, bundles: list[dict]) -> list[dict]:
    files = list(input_groups["verifier_generated_tests"])
    for bundle in bundles:
        for item in bundle.get("files") or []:
            content = item.get("content") or ""
            files.append({
                **item,
                "sha256": item.get("sha256") or hashlib.sha256(content.encode()).hexdigest(),
                "generation_scope": "current_run",
                "bundle_id": bundle.get("bundle_id"),
            })
    return files


def _load_case(directory: Path) -> dict:
    directory = directory.resolve(strict=True)
    manifest_path, manifest = _bound_file(directory, MANIFEST_NAME)
    packet_path, packet = _bound_file(directory, PACKET_NAME, manifest.get("packet"))
    result_path, result = _bound_file(directory, RESULT_NAME, manifest.get("result"))
    if result.get("task_id") != packet.get("task_id"):
        raise ValueError("packet/result task identity changed")
    annotation = result.get("annotation") or {}
    if result.get("status") != "complete" or annotation.get("task_id") != packet.get("task_id"):
        raise ValueError("test-extension verifier result is not complete and bound")
    constraints = (packet.get("frozen_visual_classification") or {}).get(
        "atomic_visual_constraints") or []
    critical = [item for item in constraints if item.get("decision_critical") == "是"]
    coverage = {item.get("requirement_id"): item
                for item in annotation.get("coverage") or []}
    if not critical or any(item.get("constraint_id") not in coverage for item in critical):
        raise ValueError("decision-critical constraints lack verifier coverage")
    bundles = annotation.get("test_bundles") or []
    measurement = _transition_status(packet, bundles)
    input_groups = _test_input_groups(packet)
    test_semantics = _test_semantics(packet, coverage, bundles)
    source = {
        "directory": str(directory),
        "packet": {"path": str(packet_path), "sha256": _sha(packet_path)},
        "result": {"path": str(result_path), "sha256": _sha(result_path)},
        "manifest": {"path": str(manifest_path), "sha256": _sha(manifest_path)},
    }
    case = {
        "task_id": packet["task_id"],
        "verifier_status": annotation.get("status"),
        "verifier_summary": annotation.get("summary"),
        "correctness_target": (packet.get("measurement_boundary") or {}).get(
            "correctness_target", "observable functional equivalence, not source-code equality"),
        "constraints": [{**constraint, "coverage": coverage[constraint["constraint_id"]]}
                        for constraint in critical],
        "bundles": bundles,
        "existing_test_files": (packet.get("existing_tests") or {}).get("files") or [],
        "repository_context_files": input_groups["repository_context"],
        "pr_author_test_files": input_groups["pr_author_tests"],
        "verifier_generated_test_files": _verifier_generated_files(input_groups, bundles),
        "test_semantics": test_semantics,
        "measurement": measurement,
        "source": source,
    }
    case["candidate_binding_sha256"] = _json_hash(case)
    return case


def _page(payload: dict) -> str:
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>F2P/P2P 测试人工审计</title><style>
*{{box-sizing:border-box}}body{{margin:0;font:13px/1.45 system-ui;color:#202124;background:#f4f6f8}}
header{{position:sticky;top:0;z-index:3;background:#fff;border-bottom:1px solid #d9dde3;padding:9px 14px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}}
main{{max-width:1500px;margin:auto;padding:10px}}button,select,textarea{{font:inherit}}button{{padding:6px 11px;border:1px solid #c8ced8;border-radius:6px;background:#fff}}button.primary{{background:#265bd7;color:#fff}}button:disabled{{opacity:.42}}
.grid{{display:grid;grid-template-columns:minmax(0,1.45fr) minmax(330px,.55fr);gap:10px}}.card{{background:#fff;border:1px solid #d9dde3;border-radius:8px;padding:10px;margin-bottom:9px}}h1{{font-size:18px;margin:0 10px 0 0}}h2{{font-size:15px;margin:0 0 7px}}h3{{font-size:13px;margin:8px 0 4px}}.pill{{padding:2px 7px;border-radius:12px;background:#e8eefc}}.ok{{color:#137333}}.bad{{color:#b3261e}}.muted{{color:#687386}}pre{{margin:5px 0;white-space:pre-wrap;max-height:360px;overflow:auto;background:#f7f8fa;border:1px solid #e2e5e9;padding:8px}}details{{margin:5px 0}}table{{width:100%;border-collapse:collapse}}th,td{{text-align:left;vertical-align:top;border-bottom:1px solid #e6e8eb;padding:5px}}textarea{{width:100%;min-height:70px;padding:7px}}.decision{{display:grid;grid-template-columns:1fr;gap:7px}}.constraint{{border-left:3px solid #4f6fdd;padding-left:9px;margin:8px 0}}.blockers{{background:#fff2f0}}@media(max-width:900px){{.grid{{grid-template-columns:1fr}}}}
</style><header><button id="prev">←</button><button id="next">→</button><h1 id="title"></h1><span id="eligibility" class="pill"></span><button id="export" class="primary">导出审计 JSON</button></header>
<main><div class="grid"><div id="evidence"></div><aside id="form"></aside></div><pre id="errors" class="bad"></pre></main>
<script>const DATA={data};let i=0;const $=s=>document.querySelector(s), saved=JSON.parse(localStorage.getItem('test-review-ui-v1:'+DATA.source_manifest_sha256)||'{{}}');
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
function render(){{const c=DATA.cases[i],m=c.measurement;$('#title').textContent=`测试人工审计 · ${{i+1}}/${{DATA.cases.length}} · ${{c.task_id}}`;$('#eligibility').textContent=m.approval_eligible?'可批准':'禁止批准';$('#eligibility').className='pill '+(m.approval_eligible?'ok':'bad');
let constraints=c.constraints.map(x=>`<div class="constraint"><b>${{esc(x.constraint_id)}} · ${{esc(x.description)}}</b><div>视觉类别：${{esc(x.visual_category)}} · 覆盖：${{esc(x.coverage.coverage)}}</div><div>${{esc(x.coverage.assertion_summary)}}</div><div class="muted">${{esc(x.coverage.reason)}}</div><div>映射现有 test：${{(x.coverage.existing_test_ids||[]).map(esc).join('、')||'无'}}</div></div>`).join('');
let bundles=c.bundles.map(b=>`<div class=card><h3>生成批次 bundle · ${{esc(b.bundle_id)}}</h3><div>预测：${{esc(b.predicted_transition)}}；base：${{esc(b.predicted_base_behavior)}}；gold：${{esc(b.predicted_reference_behavior)}}</div><div>${{esc(b.why_assertions_measure_requirements)}}</div><details open><summary>完整 unified diff</summary><pre>${{esc(b.unified_test_patch)}}</pre></details></div>`).join('')||'<div class="muted">当前记录中没有新增测试 bundle。</div>';
let transitions=m.transitions.map(t=>`<tr><td>${{esc(t.test_id)}}</td><td>${{esc(t.class)}}</td><td>${{esc(t.expected)}}</td><td>${{esc(t.base_status)}}</td><td>${{esc(t.gold_status)}}</td><td>${{t.matches===true?'✓':'✗'}}</td></tr>`).join('');
const files=(xs,empty)=>xs.length?xs.map(f=>`<details><summary>${{esc(f.path)}} · ${{esc(f.sha256)}}</summary><pre>${{esc(f.content||'（packet 未内嵌文本）')}}</pre></details>`).join('):`<p class=muted>${{esc(empty)}}</p>`;
const semantics=(origin,empty)=>{{const rows=c.test_semantics.filter(x=>origin(x));return rows.length?`<table><tr><th>测试名称</th><th>测试目的</th><th>实测分类</th><th>证据</th></tr>${{rows.map(x=>`<tr><td>${{esc(x.display_name)}}</td><td>${{esc(x.purpose)}}</td><td>${{esc(x.classification)}}</td><td>${{esc(x.classification_basis==='base_gold_measured'?'Base/Gold 实测':'尚未实测/预测')}}</td></tr>`).join('')}}</table>`:`<p class=muted>${{esc(empty)}}</p>`}};
$('#evidence').innerHTML=`<div class=card><h2>功能等价判定边界</h2><b>${{esc(c.correctness_target)}}</b><p>只审查可观察功能是否一致；不要求实现代码、文件结构或控制流与 gold patch 相同。</p></div><div class=card><h2>Verifier 结论</h2><b>${{esc(c.verifier_status)}}</b><p>${{esc(c.verifier_summary)}}</p></div><div class=card><h2>决策关键视觉约束 → 测试</h2>${{constraints}}</div><div class=card><h2>实测 F2P/P2P</h2><div>${{m.f2p}} F2P · ${{m.p2p}} P2P · base/gold 各 ${{esc(m.repeats_per_state)}} 次</div><table><tr><th>test id</th><th>分类</th><th>预测/预期</th><th>base 实测</th><th>gold 实测</th><th>一致</th></tr>${{transitions}}</table></div><div class=card><h2>测试输入来源与目的</h2><details><summary><b>仓库与测试运行上下文（${{c.repository_context_files.length}}）</b></summary><p class=muted>只解释测试框架与运行环境，不自动算作 PR 测试。</p>${{files(c.repository_context_files,'无')}}</details><details open><summary><b>PR 作者提交/修改测试文件（${{c.pr_author_test_files.length}} 个）与实际新增测试</b></summary>${{semantics(x=>x.origin==='pr_author_test','没有从作者 patch 解析到新增测试名称。')}}${{files(c.pr_author_test_files,'PR 未提交或修改测试')}}</details><details><summary><b>仓库既有回归测试</b></summary>${{semantics(x=>x.origin==='repository_existing_regression_test','没有既有回归测试。')}}</details><details open><summary><b>Verifier 生成的候选测试（${{c.verifier_generated_test_files.length}} 个文件/版本）</b></summary><p class=muted>统一展示历史轮次与当前轮次；底层仍保留生成批次和哈希。</p>${{semantics(x=>x.origin==='verifier_generated','没有可绑定的 Verifier 生成测试目的。')}}${{files(c.verifier_generated_test_files,'尚无 Verifier 生成测试文件。')}}${{bundles}}</details></div>`;
const s=saved[c.task_id]||{{decision:'revision_requested',reason:'',false_positive_risks:'',false_negative_risks:''}}, blockers=m.approval_blockers.map(esc).join('<br>');$('#form').innerHTML=`<div class="card ${{m.approval_eligible?'':'blockers'}}"><h2>批准硬门</h2>${{blockers||'<span class=ok>repeats、F2P/P2P、异常结果和生成 bundle 实测均通过。</span>'}}</div><div class="card decision"><h2>人工结论</h2><select id=decision><option value=approved ${{s.decision==='approved'?'selected':''}} ${{m.approval_eligible?'':'disabled'}}>approved</option><option value=rejected ${{s.decision==='rejected'?'selected':''}}>rejected</option><option value=revision_requested ${{s.decision==='revision_requested'?'selected':''}}>revision_requested</option></select><label>理由<textarea id=reason>${{esc(s.reason)}}</textarea></label><label>已知假阳性风险<textarea id=fp>${{esc(s.false_positive_risks)}}</textarea></label><label>已知假阴性风险<textarea id=fn>${{esc(s.false_negative_risks)}}</textarea></label><button id=save>保存本题</button></div>`;$('#save').onclick=save}}
function save(){{const c=DATA.cases[i],decision=$('#decision').value;if(decision==='approved'&&!c.measurement.approval_eligible){{$('#errors').textContent='当前证据不满足批准硬门';return}}saved[c.task_id]={{task_id:c.task_id,candidate_binding_sha256:c.candidate_binding_sha256,decision,reason:$('#reason').value,false_positive_risks:$('#fp').value,false_negative_risks:$('#fn').value,reviewed_at:new Date().toISOString()}};localStorage.setItem('test-review-ui-v1:'+DATA.source_manifest_sha256,JSON.stringify(saved));$('#errors').textContent='已保存';}}
$('#prev').onclick=()=>{{i=(i+DATA.cases.length-1)%DATA.cases.length;render()}};$('#next').onclick=()=>{{i=(i+1)%DATA.cases.length;render()}};$('#export').onclick=()=>{{const rows=DATA.cases.map(c=>saved[c.task_id]).filter(Boolean);for(const row of rows){{const c=DATA.cases.find(x=>x.task_id===row.task_id);if(row.decision==='approved'&&!c.measurement.approval_eligible){{$('#errors').textContent=row.task_id+' 不满足批准硬门';return}}}}const out={{schema_version:'test-review-human-export-v1',source_manifest_sha256:DATA.source_manifest_sha256,exported_at:new Date().toISOString(),rows}};const a=document.createElement('a'),blob=new Blob([JSON.stringify(out,null,2)],{{type:'application/json'}});a.href=URL.createObjectURL(blob);a.download='20_12_test_review_decisions.json';a.click();URL.revokeObjectURL(a.href)}};render();</script></html>'''


def render(verifier_directories: list[Path] | Path, output: Path) -> dict:
    """Render one static page for one or more completed verifier output directories."""
    if isinstance(verifier_directories, Path):
        verifier_directories = [verifier_directories]
    cases = [_load_case(Path(path)) for path in verifier_directories]
    if not cases:
        raise ValueError("at least one verifier directory is required")
    ids = [case["task_id"] for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate task id in test review page")
    output = output.resolve()
    if output.exists():
        raise ValueError(f"output already exists: {output}")
    output.mkdir(parents=True)
    payload = {"schema_version": RUNNER_VERSION, "created_at": datetime.now(timezone.utc).isoformat(),
               "cases": cases}
    payload["source_manifest_sha256"] = _json_hash(payload)
    payload_path = output / "20_12_01_review_payload.json"
    write_json(payload_path, payload)
    page_path = output / "20_12_02_test_review.html"
    page_path.write_text(_page(payload), encoding="utf-8")
    manifest = {
        "schema_version": RUNNER_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "candidate_count": len(cases),
        "approval_eligible_count": sum(case["measurement"]["approval_eligible"] for case in cases),
        "payload": {"path": str(payload_path), "sha256": _sha(payload_path)},
        "page": {"path": str(page_path), "sha256": _sha(page_path)},
    }
    write_json(output / "20_12_03_manifest.json", manifest)
    return manifest


def audit(output: Path, decisions: Path) -> dict:
    """Validate a browser export against the exact rendered evidence and hard gates."""
    output = output.resolve(strict=True)
    payload_path = output / "20_12_01_review_payload.json"
    payload = _load(payload_path)
    exported = _load(decisions.resolve(strict=True))
    if (exported.get("schema_version") != "test-review-human-export-v1"
            or exported.get("source_manifest_sha256") != payload.get("source_manifest_sha256")):
        raise ValueError("human export is not bound to this review payload")
    cases = {case["task_id"]: case for case in payload["cases"]}
    seen = set()
    counts = {"approved": 0, "rejected": 0, "revision_requested": 0}
    for row in exported.get("rows") or []:
        task_id = row.get("task_id")
        if task_id in seen or task_id not in cases:
            raise ValueError("duplicate or unknown task in human export")
        seen.add(task_id)
        case = cases[task_id]
        if row.get("candidate_binding_sha256") != case["candidate_binding_sha256"]:
            raise ValueError(f"candidate binding changed: {task_id}")
        decision = row.get("decision")
        if decision not in counts:
            raise ValueError(f"unsupported decision: {decision}")
        if decision == "approved" and not case["measurement"]["approval_eligible"]:
            raise ValueError(f"approval hard gate failed: {task_id}")
        for field in ("reason", "false_positive_risks", "false_negative_risks"):
            if not isinstance(row.get(field), str):
                raise ValueError(f"missing review field {field}: {task_id}")
        counts[decision] += 1
    result = {
        "schema_version": "test-review-audit-v1",
        "source_payload": {"path": str(payload_path), "sha256": _sha(payload_path)},
        "human_export": {"path": str(decisions.resolve()), "sha256": _sha(decisions),
                         "counts": counts, "reviewed_count": len(seen)},
        "status": "passed",
    }
    write_json(output / "20_12_04_human_audit.json", result)
    return result
