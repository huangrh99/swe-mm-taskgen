"""Audit measured test coverage and propose executable functional test gaps."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path, PurePosixPath
import shutil
import tarfile

from report_pipeline.atomic import write_json


CODE_ROOT = Path(__file__).resolve().parents[1]
PROMPT = CODE_ROOT / "analysis/prompts/20_09_existing_tests_extension.system.md"
SCHEMA = CODE_ROOT / "analysis/prompts/20_10_existing_tests_extension.schema.json"
RUNNER_VERSION = "test-extension-verifier-run-v2"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def _safe_test_path(value: str) -> bool:
    path = PurePosixPath(value)
    return (not path.is_absolute() and ".." not in path.parts
            and path.parts and path.parts[0] in {"test", "tests", "spec", "src"})


def _human_row(review: dict, case_id: str, *, require_approved: bool = True) -> dict:
    matches = [row for row in review.get("rows", []) if row.get("case_id") == case_id]
    if len(matches) != 1:
        raise ValueError("human review must contain the case exactly once")
    row = matches[0]
    visible = [asset for asset in row.get("images", []) if asset.get("solver_visible")]
    if require_approved and (row.get("decision") != "keep"
            or row.get("text_only_sufficient") != "no"
            or row.get("ocr_replaceable") != "no"
            or row.get("problem_statement_leak_free") is not True or not visible
            or any(asset.get("contains_fixed_after") or asset.get("contains_solution_evidence")
                   for asset in visible)):
        raise ValueError("visual input has not passed the human keep and leakage gate")
    if not require_approved and not row.get("problem_statement", "").strip():
        raise ValueError("provisional visual input lacks a problem statement")
    return row


def _classification(path: Path, case_id: str) -> dict:
    value = _load(path)
    records = value.get("records") or []
    matches = [record for record in records if record.get("case_id") == case_id]
    if len(matches) != 1:
        raise ValueError("classification must contain the case exactly once")
    capability = matches[0].get("visual_capability") or {}
    annotation = capability.get("annotation")
    if (capability.get("status") != "complete" or not isinstance(annotation, dict)
            or annotation.get("task_id") != case_id
            or annotation.get("strict_multimodal_admission")
            != "非文字视觉信息候选不可替代"):
        raise ValueError("case lacks a strict bound visual classification")
    return annotation


def _archive_identity(manifest: dict) -> tuple[str, dict]:
    archive_path = Path(manifest["source_archive"]).resolve(strict=True)
    if _sha(archive_path) != manifest.get("source_archive_sha256"):
        raise ValueError("case source archive changed")
    archive = _load(archive_path)
    return f'{archive["repo"].replace("/", "__")}-{archive["number"]}', archive


def _reference_files(case: Path, manifest: dict) -> list[dict]:
    wanted = set(manifest.get("selected_suites", []))
    wanted.update(manifest.get("author_test_paths", []))
    wanted.update(manifest.get("environment_inputs", {}))
    wanted.update({"package.json", "test/TestHelper.js", "test/helper/index.js"})
    archive = case / "14_reference_tree.tar"
    if _sha(archive) != manifest["artifacts"][archive.name]:
        raise ValueError("reference tree changed")
    result = []
    with tarfile.open(archive) as stream:
        members = {member.name.removeprefix("./"): member for member in stream.getmembers()}
        for name in sorted(wanted):
            member = members.get(name)
            if member is None or not member.isfile() or member.issym() or member.islnk():
                continue
            payload = stream.extractfile(member).read()
            try:
                content = payload.decode()
            except UnicodeDecodeError:
                continue
            result.append({"path": name, "sha256": hashlib.sha256(payload).hexdigest(),
                           "content": content})
    return result


def build_packet(human_review: Path, classification: Path, case: Path,
                 transition_audit: Path, *, provisional: bool = False) -> dict:
    case = case.resolve(strict=True)
    manifest_path = case / "14_case_manifest.json"
    manifest = _load(manifest_path)
    case_id, source_archive = _archive_identity(manifest)
    human = _human_row(_load(human_review), case_id, require_approved=not provisional)
    visual = _classification(classification, case_id)
    transitions = _load(transition_audit)
    if (transitions.get("status") != "measured"
            or transitions.get("measurement_eligible") is not True
            or Path(transitions.get("case", "")).resolve() != case):
        raise ValueError("transition audit is not a measured result for this case")
    production_patch = case / "14_production.patch"
    author_patch = case / "14_author_tests.patch"
    for path in (production_patch, author_patch):
        if _sha(path) != manifest["artifacts"][path.name]:
            raise ValueError(f"{path.name} changed")
    reference_files = _reference_files(case, manifest)
    file_hashes = {item["path"]: item["sha256"] for item in reference_files}
    generated = None
    candidates = sorted(Path(transitions["runs"]).glob(
        "reference_01/14_harness/14_generated_test.js"))
    if candidates:
        generated = {"path": "test/spec/14_generated_2396Spec.js",
                     "sha256": _sha(candidates[0]), "content": candidates[0].read_text()}
        file_hashes[generated["path"]] = generated["sha256"]
    return {
        "schema_version": "test-extension-verifier-packet-v1",
        "task_id": case_id,
        "admission_state": ("pre_human_visual_review" if provisional
                            else "provisional_visual_input_human_checked"),
        "solver_visible_problem_statement": human["problem_statement"],
        "human_visual_input_check": {
            "decision": human.get("decision"),
            "problem_statement_leak_free": human.get("problem_statement_leak_free"),
            "solver_visible_asset_ids": [asset["asset_id"] for asset in human["images"]
                                         if asset.get("solver_visible")],
            "reviewed_at": human.get("reviewed_at"),
            "boundary": ("input is an unapproved planning snapshot; no visual admission is implied"
                         if provisional else
                         "visual assets checked; final visual-necessity gate may remain pending"),
        },
        "frozen_visual_classification": visual,
        "production_change_summary": {
            "paths": manifest.get("production_paths", []),
            "patch": production_patch.read_text(),
            "instruction": "Use only to infer behavioral impact; never require gold code equality.",
        },
        "repository_test_context": {
            "framework": "Karma + Mocha + Chai in Chromium",
            "working_directory": ".",
            "target_command": "npm run test:unit -- --grep 'features/modeling - layout'",
            "selected_suites": manifest.get("selected_suites", []),
            "environment_inputs": manifest.get("environment_inputs", {}),
            "writable_test_roots": ["test/"],
        },
        "existing_tests": {
            "files": reference_files,
            "file_hashes": file_hashes,
            "current_generated_test": generated,
            "author_test_patch": author_patch.read_text(),
            "measured_transitions": transitions["test_transitions"],
            "measured_counts": transitions["counts"],
            "repeats_per_state": transitions["required_repeats_per_state"],
        },
        "measurement_boundary": {
            "existing_labels_are_observed": True,
            "new_bundle_labels_are_predictions_only": True,
            "final_rule": {
                "stable_fail_to_pass": "F2P",
                "stable_pass_to_pass": "P2P",
                "all_other_or_unstable_outcomes": "unclassified_or_blocked",
            },
            "correctness_target": "observable functional equivalence, not source-code equality",
        },
        "provenance": {
            "human_review": {"path": str(human_review.resolve()), "sha256": _sha(human_review)},
            "classification": {"path": str(classification.resolve()), "sha256": _sha(classification)},
            "case_manifest": {"path": str(manifest_path), "sha256": _sha(manifest_path)},
            "transition_audit": {"path": str(transition_audit.resolve()),
                                 "sha256": _sha(transition_audit)},
            "source_archive": {"path": str(Path(manifest["source_archive"]).resolve()),
                               "sha256": manifest["source_archive_sha256"]},
        },
    }


def build_harbor_packet(human_review: Path, classification: Path, task: Path,
                        source_measurement: Path, browser_measurement: Path, *,
                        provisional: bool = False) -> dict:
    task = task.resolve(strict=True)
    export_path = task / "export_manifest.json"
    export = _load(export_path)
    case_id = export["candidate_id"]
    human = _human_row(_load(human_review), case_id, require_approved=not provisional)
    visual = _classification(classification, case_id)
    for relative, expected in export["files"].items():
        path = task / relative
        if _sha(path) != expected:
            raise ValueError(f"Harbor task file changed: {relative}")
    source = _load(source_measurement)
    browser = _load(browser_measurement)
    source_transitions = source.get("measurement", {}).get("transitions", [])
    browser_transitions = browser.get("transitions", [])
    if (not source.get("measurement", {}).get("all_transitions_match")
            or not browser.get("all_transitions_match")
            or {item["test_id"] for item in source_transitions}
            != {item["test_id"] for item in browser_transitions}):
        raise ValueError("Harbor measurements are incomplete or disagree on test identity")
    test_files = []
    file_hashes = {}
    for path in sorted((task / "tests").iterdir()):
        if not path.is_file():
            continue
        try:
            content = path.read_text()
        except UnicodeDecodeError:
            continue
        relative = path.relative_to(task).as_posix()
        digest = _sha(path)
        test_files.append({"path": relative, "sha256": digest, "content": content})
        file_hashes[relative] = digest
    counts = {
        "F2P": sum(item.get("actual") == "fail->pass" for item in browser_transitions),
        "P2P": sum(item.get("actual") == "pass->pass" for item in browser_transitions),
    }
    reference_patch = task / "solution/gold.patch"
    return {
        "schema_version": "test-extension-verifier-packet-v1",
        "task_id": case_id,
        "admission_state": ("pre_human_visual_review_tests_measured_once" if provisional
                            else "human_visual_keep_tests_measured_once"),
        "solver_visible_problem_statement": human["problem_statement"],
        "human_visual_input_check": {
            "decision": human.get("decision"),
            "problem_statement_leak_free": human.get("problem_statement_leak_free"),
            "text_only_sufficient": human.get("text_only_sufficient"),
            "ocr_replaceable": human.get("ocr_replaceable"),
            "solver_visible_asset_ids": [asset["asset_id"] for asset in human["images"]
                                         if asset.get("solver_visible")],
            "reviewed_at": human.get("reviewed_at"),
            "boundary": ("input is an unapproved planning snapshot; no visual admission is implied"
                         if provisional else "human visual keep is recorded"),
        },
        "frozen_visual_classification": visual,
        "production_change_summary": {
            "patch": reference_patch.read_text(),
            "instruction": "Infer observable behavioral impact; never require gold code equality.",
        },
        "repository_test_context": {
            "framework": "Sass compilation plus headless Chromium computed-style assertions",
            "working_directory": "/app",
            "target_command": "bash /tests/test.sh",
            "writable_test_roots": ["tests/"],
        },
        "existing_tests": {
            "files": test_files,
            "file_hashes": file_hashes,
            "measured_transitions": browser_transitions,
            "measured_counts": counts,
            "repeats_per_state": 1,
            "measurement_sources": [
                {"kind": "source_semantics", "path": str(source_measurement.resolve()),
                 "sha256": _sha(source_measurement)},
                {"kind": "chromium_computed_style", "path": str(browser_measurement.resolve()),
                 "sha256": _sha(browser_measurement)},
            ],
        },
        "measurement_boundary": {
            "existing_labels_are_observed": True,
            "stable_three_run_repetition_complete": False,
            "new_bundle_labels_are_predictions_only": True,
            "final_rule": {
                "stable_fail_to_pass": "F2P",
                "stable_pass_to_pass": "P2P",
                "all_other_or_unstable_outcomes": "unclassified_or_blocked",
            },
            "correctness_target": "observable functional equivalence, not source-code equality",
        },
        "provenance": {
            "human_review": {"path": str(human_review.resolve()), "sha256": _sha(human_review)},
            "classification": {"path": str(classification.resolve()),
                               "sha256": _sha(classification)},
            "harbor_export": {"path": str(export_path), "sha256": _sha(export_path)},
            "reference_patch": {"path": str(reference_patch), "sha256": _sha(reference_patch)},
        },
    }


def validate_annotation(value: dict, packet: dict, schema: Path) -> None:
    import jsonschema
    jsonschema.validate(value, _load(schema))
    if value["task_id"] != packet["task_id"]:
        raise ValueError("response task identity changed")
    expected = {item["constraint_id"] for item in
                packet["frozen_visual_classification"]["atomic_visual_constraints"]
                if item.get("decision_critical") == "是"}
    observed = [item["requirement_id"] for item in value["coverage"]]
    if set(observed) != expected or len(observed) != len(set(observed)):
        raise ValueError("coverage must classify every decision-critical requirement exactly once")
    contract_ids = [item["requirement_id"] for item in
                    value["behavioral_contract"]["observable_requirements"]]
    if set(contract_ids) != expected or len(contract_ids) != len(set(contract_ids)):
        raise ValueError(
            "behavioral contract must cover every decision-critical requirement exactly once")
    bundles = value["test_bundles"]
    if (value["status"] == "additional_tests_proposed") != bool(bundles):
        raise ValueError("bundle presence does not match status")
    before = packet["existing_tests"]["file_hashes"]
    stable_ids = []
    for bundle in bundles:
        if not set(bundle["target_requirement_ids"]) <= expected:
            raise ValueError("bundle targets an unknown requirement")
        for item in bundle["files"]:
            path = item["path"]
            roots = packet.get("repository_test_context", {}).get(
                "writable_test_roots", ["test/"])
            in_root = any(path == root.rstrip("/") or path.startswith(root) for root in roots)
            if not _safe_test_path(path) or not in_root:
                raise ValueError("generated bundle writes outside the frozen test root")
            if item["operation"] == "add" and item["sha256_before"] is not None:
                raise ValueError("added test file unexpectedly has a prior hash")
            if item["operation"] == "modify" and before.get(path) != item["sha256_before"]:
                raise ValueError("modified test file hash does not match supplied context")
            if path not in bundle["unified_test_patch"]:
                raise ValueError("unified test patch omits a declared file")
        stable_ids.extend(bundle["stable_test_ids"])
    if len(stable_ids) != len(set(stable_ids)):
        raise ValueError("generated stable test IDs are duplicated")
    known_test_ids = set(stable_ids)
    for item in value["coverage"]:
        known_test_ids.update(item["existing_test_ids"])
    oracle_plan = value["oracle_quality_plan"]
    oracle_ids = set(oracle_plan["equivalent_positive_variant"]["expected_pass_test_ids"])
    for variant in oracle_plan["negative_variants"]:
        oracle_ids.update(variant["expected_failure_test_ids"])
    if not oracle_ids <= known_test_ids:
        raise ValueError("oracle-quality test identity is absent from coverage or bundles")


def _attempt_files(work: Path, index: int) -> dict:
    files = {}
    for path in sorted(work.iterdir()):
        if path.is_file():
            files[path.name] = {"path": str(path.resolve()), "sha256": _sha(path)}
    return {"semantic_attempt": index, "directory": str(work.resolve()), "files": files}


def _render(output: Path, packet: dict, result: dict) -> Path:
    annotation = result.get("annotation") or {}
    coverage = "".join(
        f"<tr><td>{html.escape(str(item.get('requirement_id')))}</td>"
        f"<td>{html.escape(str(item.get('coverage')))}</td>"
        f"<td>{html.escape(str(item.get('assertion_summary')))}</td>"
        f"<td>{html.escape(str(item.get('reason')))}</td></tr>"
        for item in annotation.get("coverage", []))
    bundles = "".join(
        f"<section><h3>{html.escape(str(item.get('bundle_id')))}</h3>"
        f"<p><b>预测：</b>{html.escape(str(item.get('predicted_transition')))} · "
        f"<b>oracle：</b>{html.escape(str(item.get('oracle_type')))}</p>"
        f"<p>{html.escape(str(item.get('why_assertions_measure_requirements')))}</p>"
        f"<details><summary>完整测试补丁</summary><pre>{html.escape(str(item.get('unified_test_patch')))}</pre></details></section>"
        for item in annotation.get("test_bundles", []))
    contract = annotation.get("behavioral_contract") or {}
    contract_rows = "".join(
        f"<tr><td>{html.escape(str(item.get('requirement_id')))}</td>"
        f"<td>{html.escape(str(item.get('contract')))}</td></tr>"
        for item in contract.get("observable_requirements", []))
    oracle_plan = annotation.get("oracle_quality_plan") or {}
    negative_rows = "".join(
        f"<tr><td>{html.escape(str(item.get('variant_id')))}</td>"
        f"<td>{html.escape(str(item.get('defect_preserved_or_introduced')))}</td>"
        f"<td>{html.escape(', '.join(item.get('expected_failure_test_ids', [])))}</td></tr>"
        for item in oracle_plan.get("negative_variants", []))
    positive = oracle_plan.get("equivalent_positive_variant") or {}
    page = f"""<!doctype html><meta charset=utf-8><title>20_11 test coverage verifier</title>
<style>body{{font:13px/1.45 system-ui;margin:18px;color:#202124;background:#f6f7f8}}main{{max-width:1280px;margin:auto}}section,.card{{background:#fff;border:1px solid #ddd;border-radius:8px;padding:12px;margin:10px 0}}table{{border-collapse:collapse;width:100%}}th,td{{border-bottom:1px solid #ddd;padding:6px;text-align:left;vertical-align:top}}pre{{white-space:pre-wrap;max-height:420px;overflow:auto}}.warn{{color:#9a6700}}</style>
<main><h1>{html.escape(packet['task_id'])} · F2P/P2P 覆盖补全 Verifier</h1>
<div class=card><b>结论：</b>{html.escape(str(annotation.get('status', result.get('status'))))}<br>
<b>已有实测：</b>{packet['existing_tests']['measured_counts']['F2P']} F2P · {packet['existing_tests']['measured_counts']['P2P']} P2P，前后各 {packet['existing_tests']['repeats_per_state']} 次<br>
<span class=warn>模型只提出覆盖与测试补全建议；新增测试的 F2P/P2P 必须重新实测。正确性按功能行为，不按 gold 代码文本。</span></div>
<section><h2>实现无关的行为契约</h2><table><tr><th>约束</th><th>可观察契约</th></tr>{contract_rows}</table>
<p><b>必须保持：</b>{html.escape('；'.join(contract.get('preserved_behaviors', [])))}</p>
<p><b>允许的实现差异：</b>{html.escape(str(contract.get('implementation_variation', '')))}</p></section>
<section><h2>逐约束覆盖</h2><table><tr><th>约束</th><th>覆盖</th><th>当前断言</th><th>理由</th></tr>{coverage}</table></section>
{bundles}<section><h2>Oracle-quality validation 计划</h2>
<p class=warn>{html.escape(str(oracle_plan.get('status', 'missing')))}；仅供构造与审计，不暴露给 solver，未执行不得视为通过。</p>
<table><tr><th>错误/不完整变体</th><th>应暴露的问题</th><th>应失败 test ID</th></tr>{negative_rows}</table>
<p><b>等价正确实现：</b>{html.escape(str(positive.get('description', '')))}<br>
<b>应通过：</b>{html.escape(', '.join(positive.get('expected_pass_test_ids', [])))}</p></section>
<details><summary>完整 Verifier JSON</summary><pre>{html.escape(json.dumps(annotation, ensure_ascii=False, indent=2))}</pre></details></main>"""
    target = output / "20_11_08_audit.html"
    target.write_text(page)
    return target


def _run_packet(packet: dict, output: Path, *, evaluator=None,
                timeout: int = 480) -> dict:
    output = output.resolve()
    if output.exists():
        raise ValueError(f"output already exists: {output}")
    output.mkdir(parents=True)
    packet_path = output / "20_11_01_packet.json"
    write_json(packet_path, packet)
    shutil.copyfile(PROMPT, output / "20_11_02_system.md")
    shutil.copyfile(SCHEMA, output / "20_11_03_schema.json")
    shutil.copyfile(Path(__file__), output / "20_11_04_runner.py")
    result = {"schema_version": RUNNER_VERSION, "task_id": packet["task_id"],
              "status": "prepared", "annotation": None, "invocation": None}
    attempts = []
    failures = []
    if evaluator is not None:
        for index in (1, 2, 3):
            work = output / f"20_11_05_calls/semantic_{index:02d}"
            work.mkdir(parents=True)
            attempt_packet = json.loads(json.dumps(packet))
            if failures:
                attempt_packet["previous_output_validation_error"] = failures[-1]
            try:
                annotation, invocation = evaluator(
                    packet=attempt_packet, image_paths=[],
                    system_prompt=output / "20_11_02_system.md",
                    schema=output / "20_11_03_schema.json", workdir=work,
                    timeout=timeout)
                attempts.append(_attempt_files(work, index))
                validate_annotation(annotation, packet, output / "20_11_03_schema.json")
                invocation.update(semantic_validation_attempts=index,
                                  prior_validation_failures=failures,
                                  semantic_attempt_records=attempts)
                result.update(status="complete", annotation=annotation,
                              invocation=invocation)
                break
            except Exception as exc:
                if not attempts or attempts[-1]["semantic_attempt"] != index:
                    attempts.append(_attempt_files(work, index))
                failures.append(f"{type(exc).__name__}: {str(exc)[:1200]}")
        if result["status"] != "complete":
            result.update(status="failed", failure_class="provider_or_semantic_validation",
                          semantic_attempt_records=attempts,
                          validation_failures=failures)
    result_path = output / "20_11_06_result.json"
    write_json(result_path, result)
    proposed = output / "20_11_07_proposed_test_files"
    if (result.get("annotation") or {}).get("test_bundles"):
        proposed.mkdir()
        for bundle_index, bundle in enumerate(result["annotation"]["test_bundles"], 1):
            for file_index, item in enumerate(bundle["files"], 1):
                suffix = Path(item["path"]).suffix or ".txt"
                (proposed / f"bundle_{bundle_index:02d}_file_{file_index:02d}{suffix}").write_text(
                    item["content"])
    audit = _render(output, packet, result)
    manifest = {
        "schema_version": RUNNER_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": result["status"],
        "model_invoked": evaluator is not None,
        "packet": {"path": str(packet_path), "sha256": _sha(packet_path)},
        "prompt_sha256": _sha(output / "20_11_02_system.md"),
        "schema_sha256": _sha(output / "20_11_03_schema.json"),
        "runner_sha256": _sha(output / "20_11_04_runner.py"),
        "result": {"path": str(result_path), "sha256": _sha(result_path)},
        "audit": {"path": str(audit), "sha256": _sha(audit)},
        "boundary": "predicted transitions are never final F2P/P2P labels",
    }
    write_json(output / "20_11_09_manifest.json", manifest)
    return manifest


def run(human_review: Path, classification: Path, case: Path,
        transition_audit: Path, output: Path, *, evaluator=None,
        timeout: int = 480, provisional: bool = False) -> dict:
    packet = build_packet(human_review, classification, case, transition_audit,
                          provisional=provisional)
    return _run_packet(packet, output, evaluator=evaluator, timeout=timeout)


def run_harbor(human_review: Path, classification: Path, task: Path,
               source_measurement: Path, browser_measurement: Path, output: Path,
               *, evaluator=None, timeout: int = 480,
               provisional: bool = False) -> dict:
    packet = build_harbor_packet(human_review, classification, task, source_measurement,
                                 browser_measurement, provisional=provisional)
    return _run_packet(packet, output, evaluator=evaluator, timeout=timeout)
