"""Bind the provisional Carbon test/control/K3 smoke evidence without promoting it."""

from __future__ import annotations

import hashlib
import html
import json
import os
from pathlib import Path

from report_pipeline.paths import WORKSPACE_ROOT


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _portable(path: Path) -> str:
    resolved = path.resolve()
    if not resolved.is_file() or not resolved.is_relative_to(WORKSPACE_ROOT.resolve()):
        raise ValueError(f"provisional evidence must be a workspace file: {path}")
    return resolved.relative_to(WORKSPACE_ROOT.resolve()).as_posix()


def _binding(path: Path) -> dict:
    return {"path": _portable(path), "sha256": _sha(path.resolve())}


def _load(path: Path) -> dict:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"provisional evidence is not an object: {path}")
    return value


def _test_contract(source: dict, browser: dict) -> dict:
    baseline = source.get("baseline") or {}
    reference = source.get("reference") or {}
    measurement = source.get("measurement") or {}
    baseline_results = baseline.get("results") or []
    reference_results = reference.get("results") or []
    ids = [item.get("test_id") for item in baseline_results]
    classes = {item.get("test_id"): item.get("class") for item in baseline_results}
    expected_baseline = {"F2P": "fail", "P2P": "pass"}
    source_valid = (
        ids and len(ids) == len(set(ids))
        and ids == [item.get("test_id") for item in reference_results]
        and baseline.get("test_manifest_sha256") == reference.get("test_manifest_sha256")
        and all(item.get("status") == expected_baseline.get(item.get("class"))
                for item in baseline_results)
        and all(item.get("status") == "pass" for item in reference_results)
        and measurement.get("all_transitions_match") is True
        and [item.get("test_id") for item in measurement.get("transitions", [])] == ids
    )
    browser_transitions = browser.get("transitions") or []
    browser_valid = (
        browser.get("all_transitions_match") is True
        and [item.get("test_id") for item in browser_transitions] == ids
        and all(item.get("matches") is True for item in browser_transitions)
    )
    return {
        "status": "measured_pending_human_semantic_gate" if source_valid and browser_valid else "invalid",
        "source_valid": source_valid,
        "browser_valid": browser_valid,
        "test_ids": ids,
        "f2p_count": sum(value == "F2P" for value in classes.values()),
        "p2p_count": sum(value == "P2P" for value in classes.values()),
        "source_scope": (baseline.get("scope")),
        "browser_oracle_kind": browser.get("oracle_kind"),
    }


def _control_contract(controls: dict, tests: dict, instance_id: str) -> dict:
    values = controls.get("controls") or {}
    nop = values.get("baseline_nop") or {}
    oracle = values.get("oracle") or {}
    ids = tests["test_ids"]
    nop_results = nop.get("results") or []
    oracle_results = oracle.get("results") or []
    valid = (
        controls.get("candidate_id") == instance_id
        and controls.get("status") == "baseline_and_oracle_controls_passed"
        and nop.get("reward") == 0 and oracle.get("reward") == 1
        and [item.get("test_id") for item in nop_results] == ids
        and [item.get("test_id") for item in oracle_results] == ids
        and all(item.get("status") == "pass" for item in oracle_results)
    )
    return {
        "status": "passed_provisional_only" if valid else "invalid",
        "valid": valid,
        "nop_reward": nop.get("reward"),
        "oracle_reward": oracle.get("reward"),
        "task_material_sha256": controls.get("task_material_sha256"),
        "scope": controls.get("scope"),
    }


def _smoke_contract(smokes: list[tuple[Path, dict]]) -> dict:
    attempts = []
    valid_trial_count = 0
    for path, value in smokes:
        formal = value.get("formal_pass5")
        classification = value.get("classification") or value.get("status")
        count = value.get("valid_behavioral_trial_count", 0)
        if not isinstance(count, int) or count < 0:
            raise ValueError("invalid provisional smoke valid-trial count")
        valid_trial_count += count
        attempts.append({
            "evidence": _binding(path),
            "classification": classification,
            "formal_pass5": formal,
            "valid_behavioral_trial_count": count,
            "counts_as_model_failure": (value.get("outcome") or {}).get(
                "counts_as_model_failure", False),
            "infrastructure_or_runtime_reason": (
                (value.get("outcome") or {}).get("failure_class")
                or (value.get("terminal_evidence") or {}).get("trial_exception")
            ),
            "trajectory_present": (value.get("terminal_evidence") or {}).get(
                "trajectory_present", False),
        })
    return {
        "status": "five_valid_trials_complete" if valid_trial_count == 5 else "incomplete",
        "valid_trial_count": valid_trial_count,
        "remaining_valid_trials": max(0, 5 - valid_trial_count),
        "attempts": attempts,
    }


def run(instance_id: str, category_audit_path: Path, source_path: Path,
        browser_path: Path, controls_path: Path, smoke_paths: list[Path],
        output: Path) -> dict:
    if not smoke_paths:
        raise ValueError("at least one K3 smoke record is required")
    category = _load(category_audit_path)
    category_row = next((item for item in category.get("rows", [])
                         if item.get("case_id") == instance_id), None)
    if category_row is None:
        raise ValueError("provisional instance is absent from category audit")
    source = _load(source_path)
    browser = _load(browser_path)
    controls = _load(controls_path)
    tests = _test_contract(source, browser)
    control = _control_contract(controls, tests, instance_id)
    smokes = _smoke_contract([(path, _load(path)) for path in smoke_paths])
    gates = {
        "visual_verifier": "provisional_auto_candidate" if category_row.get("counted") else "not_qualified",
        "visual_human_gate": "pending",
        "tests_measurement": tests["status"],
        "f2p_p2p_human_gate": "pending",
        "harbor_controls": control["status"],
        "formal_freeze": "not_executed",
        "k3_pass5": smokes["status"],
    }
    value = {
        "schema_version": "provisional-technical-chain-audit-v1",
        "status": "provisional_chain_partially_exercised",
        "formal_benchmark_admission": False,
        "instance_id": instance_id,
        "gates": gates,
        "category": {
            "audit": _binding(category_audit_path),
            "counted": category_row.get("counted"),
            "primary_visual_category": category_row.get("primary_visual_category"),
            "strict_multimodal_admission": category_row.get("strict_multimodal_admission"),
        },
        "tests": {"evidence": [_binding(source_path), _binding(browser_path)], **tests},
        "harbor_controls": {"evidence": _binding(controls_path), **control},
        "k3": smokes,
        "blocking_human_work": ["multimodal_necessity", "f2p_p2p_semantic_validity"],
        "blocking_technical_work": ["formal_freeze_after_human_gates",
                                     "five_valid_independent_k3_trials"],
    }
    if output.exists():
        raise ValueError(f"provisional audit output exists: {output}")
    output.mkdir(parents=True)
    json_path = output / "19_41_01_provisional_technical_chain.json"
    json_path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    def link(binding: dict, label: str) -> str:
        href = os.path.relpath(WORKSPACE_ROOT / binding["path"], output)
        return f'<a href="{html.escape(href, quote=True)}">{html.escape(label)}</a>'

    gate_rows = "".join(
        f"<tr><td>{html.escape(name)}</td><td>{html.escape(status)}</td></tr>"
        for name, status in gates.items())
    smoke_rows = "".join(
        "<tr><td>" + link(item["evidence"], f"attempt {index}") + "</td><td>"
        + html.escape(str(item["classification"])) + "</td><td>"
        + html.escape(str(item["infrastructure_or_runtime_reason"] or "—")) + "</td><td>"
        + str(item["valid_behavioral_trial_count"]) + "</td></tr>"
        for index, item in enumerate(smokes["attempts"], 1)
    )
    document = f'''<!doctype html><meta charset="utf-8"><title>Provisional technical chain</title>
<style>body{{font:13px system-ui;margin:18px;color:#202124;max-width:1200px}}.warn{{background:#fff4ce;border:1px solid #e5ba4e;padding:9px;border-radius:7px}}.grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:7px}}.card{{border:1px solid #ddd;padding:9px;border-radius:7px}}table{{width:100%;border-collapse:collapse;margin:10px 0}}th,td{{text-align:left;padding:6px;border-bottom:1px solid #ddd}}</style>
<h1>{html.escape(instance_id)} · provisional 技术链</h1><p class="warn"><b>不是正式题。</b> 两道人工审核、正式冻结和五次有效 K3 trial 尚未完成；API/基础设施无效尝试不算模型失败。</p>
<div class="grid"><div class="card"><b>V3 类别</b><p>{html.escape(str(category_row.get('primary_visual_category')))}</p>{link(value['category']['audit'], '完整分类审计')}</div><div class="card"><b>F2P / P2P</b><p>{tests['f2p_count']} F2P · {tests['p2p_count']} P2P</p>{' · '.join(link(item, label) for item, label in zip(value['tests']['evidence'], ('source', 'browser')))}</div><div class="card"><b>Harbor controls</b><p>nop={control['nop_reward']} · oracle={control['oracle_reward']}</p>{link(value['harbor_controls']['evidence'], 'control audit')}</div></div>
<h2>状态门</h2><table><thead><tr><th>门</th><th>状态</th></tr></thead><tbody>{gate_rows}</tbody></table>
<h2>K3 尝试</h2><p>有效 {smokes['valid_trial_count']}/5，仍缺 {smokes['remaining_valid_trials']}。</p><table><thead><tr><th>证据</th><th>分类</th><th>原因</th><th>有效 trial</th></tr></thead><tbody>{smoke_rows}</tbody></table>'''
    (output / "19_41_02_provisional_technical_chain.html").write_text(document + "\n")
    return value
