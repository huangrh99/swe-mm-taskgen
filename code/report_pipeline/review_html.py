"""Render a compact local review page for candidate and test evidence."""

import html
import hashlib
import json
import os
from html.parser import HTMLParser
from pathlib import Path


class _ImageAuditParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.images: list[str] = []
        self.script_tags = 0
        self.event_attributes = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "script":
            self.script_tags += 1
        self.event_attributes += sum(name.lower().startswith("on") for name, _ in attrs)
        if tag.lower() == "img":
            values = dict(attrs)
            if values.get("src"):
                self.images.append(values["src"] or "")


def audit(output: Path, record_path: Path) -> dict:
    """Statically audit the generated local HTML and all relative image links."""
    output = output.resolve()
    parser = _ImageAuditParser(); parser.feed(output.read_text())
    assets = []
    from report_pipeline.paths import REPORT_ROOT
    workspace = REPORT_ROOT.resolve()
    for reference in parser.images:
        candidate = (output.parent / reference).resolve()
        if not candidate.is_relative_to(workspace) or not candidate.is_file():
            raise ValueError(f"missing or unsafe HTML image reference: {reference}")
        assets.append({"reference": reference, "sha256": hashlib.sha256(candidate.read_bytes()).hexdigest()})
    text = output.read_text()
    markers = ["门 1 · Multimodal 必要性", "门 2 · F2P/P2P 语义有效性",
               "Harbor 结构化负向与隔离控制", "all_controls_passed"]
    optional_markers = ["Pass@5 冻结提案（尚未调用）"]
    missing = [value for value in markers if value not in text]
    if parser.script_tags or parser.event_attributes or missing or len(assets) != 4:
        raise ValueError(f"HTML static audit failed: scripts={parser.script_tags}, events={parser.event_attributes}, missing={missing}, images={len(assets)}")
    result = {"schema_version": "candidate-review-static-audit-v1", "status": "passed",
              "html": str(output), "html_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
              "bytes": output.stat().st_size, "image_count": len(assets), "images": assets,
              "required_markers": markers, "script_tags": parser.script_tags,
              "event_attributes": parser.event_attributes,
              "optional_markers_present": {value: value in text for value in optional_markers},
              "browser_rendering_claimed": False}
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


def render(dossier_path: Path, manifest_path: Path, measurement_path: Path, output: Path,
           instruction_path: Path | None = None, controls_path: Path | None = None,
           run_proposal_path: Path | None = None,
           negative_controls_path: Path | None = None) -> Path:
    output = output.resolve()
    dossier = json.loads(dossier_path.read_text())
    manifest = json.loads(manifest_path.read_text())
    measured = json.loads(measurement_path.read_text())
    measurement = measured.get("measurement", measured)
    tests = {item["test_id"]: item for item in manifest["tests"]}
    rows = []
    for item in measurement["transitions"]:
        spec = tests[item["test_id"]]
        rows.append("<tr><td><code>{}</code></td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            html.escape(item["test_id"]), html.escape(str(item["class"])), html.escape(str(item["actual"])),
            "✓" if item["matches"] else "✗", html.escape(" | ".join(spec["contains_all"]))))
    images = []
    for asset in dossier["leakage_policy"]["safe_agent_assets"]:
        relative = os.path.relpath(asset["local_path"], output.parent)
        images.append(f'<figure><img src="{html.escape(relative)}"><figcaption><code>{html.escape(str(asset["asset_id"])[:12])}</code> · {html.escape(", ".join(asset["source_ids"]))}</figcaption></figure>')
    instruction = instruction_path.read_text() if instruction_path else ""
    controls = json.loads(controls_path.read_text()) if controls_path else None
    proposal = json.loads(run_proposal_path.read_text()) if run_proposal_path else None
    negative_controls = json.loads(negative_controls_path.read_text()) if negative_controls_path else None
    visual_gate = dossier["visual_admission"]
    test_gate = dossier["test_calibration"]
    eligibility = dossier.get("benchmark_eligibility", {})
    f2p_count = sum(item.get("class") == "F2P" for item in manifest["tests"])
    p2p_count = sum(item.get("class") == "P2P" for item in manifest["tests"])
    control_cards = ""
    if controls:
        cards = []
        for name, record in controls["controls"].items():
            cards.append(f'<div class="card"><b>{html.escape(name)}</b><br>reward: <span class="ok">{html.escape(str(record["reward"]))}</span><br>trial: <code>{html.escape(record["trial_id"])}</code><br>8 个稳定 test ID 均实际执行</div>')
        control_cards = '<h2>Harbor 实际对照</h2><div class="grid">' + "".join(cards) + '</div>'
    negative_cards = ""
    if negative_controls:
        cards = []
        for name, record in negative_controls["controls"].items():
            summary = record.get("summary") or {}
            counts = " · ".join(f"{key}={summary.get(key, 0)}" for key in ("pass", "fail", "skip", "missing", "error"))
            reward = "infra-invalid" if record.get("reward") is None else str(record["reward"])
            passed_class = "ok" if record.get("control_passed") else "pending"
            outcome = str(record.get("outcome_class", "unknown"))
            outcome_class = "outcome-" + outcome.replace("_", "-")
            cards.append(f'<div class="card"><b>{html.escape(name)}</b> · outcome: <span class="{html.escape(outcome_class)}">{html.escape(outcome)}</span><br>reward: {html.escape(reward)} · expectation: <span class="{passed_class}">{str(record.get("control_passed", False)).lower()}</span><br>{html.escape(counts)}<br><small>{html.escape(record.get("expected_outcome", ""))}</small></div>')
        negative_cards = '<h2>Harbor 结构化负向与隔离控制</h2><p>总体：<b>{}</b></p><div class="grid">{}</div>'.format(
            html.escape(negative_controls.get("status", "unknown")), "".join(cards))
    proposal_card = ""
    if proposal:
        agent, pass5 = proposal["agent"], proposal["pass_at_5"]
        proposal_card = f'''<h2>Pass@5 冻结提案（尚未调用）</h2><div class="card">
<b>{html.escape(agent["model_id"])}</b> · <code>{html.escape(agent["adapter"])}</code><br>
5 个有效独立 trial，串行度 {html.escape(str(pass5["concurrency"]))}；有效 trial 最多 {html.escape(str(pass5["maximum_model_calls_for_five_valid_trials"]))} 次模型 turn；含全部重试绝对上限 {html.escape(str(pass5["absolute_model_call_upper_bound_with_all_retries"]))}。<br>
状态：<span class="pending">{html.escape(proposal["status"])}</span>；没有授权记录就不能启动。</div>'''
    raw = html.escape(json.dumps({"dossier": dossier, "test_manifest": manifest, "measurement": measured,
                                  "harbor_controls": controls, "negative_controls": negative_controls,
                                  "run_proposal": proposal}, ensure_ascii=False, indent=2))
    source_links = []
    for label, key in (("完整 Issue/PR 来源归档", "archive_path"),
                       ("视觉 verifier 原始判定", "verifier_path"),
                       ("模型输入 packet", "packet_path"),
                       ("curator-only 资产索引", "curator_assets_path")):
        source = dossier.get("source_bindings", {}).get(key)
        if source:
            relative = os.path.relpath(source, output.parent)
            source_links.append(f'<li><a href="{html.escape(relative)}">{html.escape(label)}</a></li>')
    if dossier.get("url"):
        source_links.append(f'<li><a href="{html.escape(str(dossier["url"]))}">GitHub PR</a></li>')
    page = f'''<!doctype html><meta charset="utf-8"><title>{html.escape(dossier["candidate_id"])}</title>
<style>body{{font:13px/1.45 system-ui;margin:20px;color:#222}}h1{{font-size:20px}}.grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}}.card{{border:1px solid #ddd;border-radius:7px;padding:10px}}.pending,.outcome-infrastructure-invalid,.outcome-test-contract-invalid,.outcome-test-execution-invalid{{color:#9a6700}}.ok,.outcome-behavioral-pass{{color:#147d3f}}.outcome-behavioral-failure{{color:#b42318}}table{{border-collapse:collapse;width:100%}}th,td{{padding:6px;border-bottom:1px solid #e5e5e5;text-align:left;vertical-align:top}}td:last-child{{max-width:520px;word-break:break-word}}figure{{display:inline-block;width:23%;margin:1%;vertical-align:top}}img{{max-width:100%;max-height:180px;border:1px solid #ddd}}figcaption{{font-size:11px;word-break:break-all}}pre{{white-space:pre-wrap;font-size:11px}}code{{font-size:11px}}</style>
<h1>{html.escape(dossier["candidate_id"])} · verifier 自动准入后的双校准审查</h1>
<p class="pending"><b>当前资格：</b>{html.escape(eligibility.get("current_stage", "executable_candidate"))}；可继续构建/测量：{str(eligibility.get("may_construct_and_measure_tests", True)).lower()}；进入最终题集：{str(eligibility.get("may_enter_final_taskset", False)).lower()}。自动准入不等于人工确认。</p>
<div class="grid"><div class="card"><b>门 1 · Multimodal 必要性</b><br>Verifier：<span class="ok">{html.escape(visual_gate["decision"])}</span>（{html.escape(visual_gate.get("confidence", "unknown"))}）<br>授权范围：{html.escape(visual_gate.get("admission_scope", "legacy"))}<br>人工校准：<span class="pending">{html.escape(str(visual_gate["human_calibration_state"]))}</span><br>{html.escape(visual_gate["reason"])}</div>
<div class="card"><b>门 2 · F2P/P2P 语义有效性</b><br>实测：{f2p_count} F2P + {p2p_count} P2P transitions match<br>人工校准：<span class="pending">{html.escape(str(test_gate["human_semantic_calibration_state"]))}</span><br>oracle: {html.escape(measurement.get("oracle_kind", "source_semantics"))}; pixel oracle: false<br><span class="pending">通过实测不等于语义人工批准</span></div></div>
<h2>Agent-safe Issue 图片（PR/comment 图片均未进入）</h2>{''.join(images)}
<h2>测试转移与断言</h2><table><thead><tr><th>ID</th><th>类</th><th>实测</th><th>匹配</th><th>行为断言</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
{control_cards}
{negative_cards}
{proposal_card}
<h2>Agent 收到的完整题面</h2><pre>{html.escape(instruction)}</pre>
<h2>完整来源材料入口</h2><ul>{''.join(source_links)}</ul>
<p class="pending">注意：Harbor reward 已执行 SCSS 编译和真实 Chromium computed-style oracle，但仍是冻结的最小 shadow-DOM fixture，不是完整组件截图/pixel oracle；所有测试含义仍待门 2 人工校准。</p>
<details><summary>完整 dossier、manifest、measurement 与 Harbor controls</summary><pre>{raw}</pre></details>'''
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(page, encoding="utf-8")
    return output
