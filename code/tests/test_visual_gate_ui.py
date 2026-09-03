import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from report_pipeline.paths import TMP_ROOT
from report_pipeline import visual_gate_ui as subject
from report_pipeline import visual_review_server as live_subject


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class VisualGateUiTests(unittest.TestCase):
    def test_v3_roles_seed_conservative_review_roles(self):
        self.assertEqual("before_only", subject._seed_role("实际状态"))
        self.assertEqual("expected_design", subject._seed_role("期望目标"))
        self.assertEqual("temporal_sequence", subject._seed_role("多状态过程"))
        self.assertEqual("after_only", subject._seed_role("可能泄漏的修复后结果"))
        self.assertEqual("unclear", subject._seed_role("当前输入不足，无法判断"))

    def fixture(self, root):
        case_id = "owner__repo-7"
        archive_root = root / "source"
        asset_path = archive_root / "11_http_archive/assets/image"
        asset_path.parent.mkdir(parents=True)
        Image.new("RGB", (20, 10), "red").save(asset_path, format="PNG")
        asset_id = sha(asset_path)
        archive = archive_root / "11_record.json"
        archive.write_text(json.dumps({
            "instance_id": case_id,
            "sections": {
                "pull_request": {"data": {
                    "html_url": "https://github.com/owner/repo/pull/7",
                    "title": "Fix the visual state", "body": "curator-only solution prose",
                }},
                "assets": {"items": [{
                    "status": "complete", "sha256": asset_id,
                    "media_type": "image/png", "local_path": "assets/image",
                }]},
            },
        }))
        source_packet = root / "source_packet.json"
        source_packet.write_text(json.dumps({
            "case_id": case_id, "repository": "owner/repo", "pr_number": 7,
            "problem_sources": [{"source_id": "owner/repo#6:body", "text": "Broken visual state"}],
        }))
        source_result = root / "source_result.json"
        source_result.write_text(json.dumps({
            "case_id": case_id,
            "text_decision": {"bucket": "visual_candidate"},
            "reconciliation": {"queue": "human_review"},
        }))
        packet = root / "classification_packet.json"
        packet.write_text(json.dumps({
            "task_id": case_id, "problem_statement": "Broken visual state",
            "assets": [{"asset_id": asset_id, "attachment_index": 1,
                        "source_ids": ["owner/repo#6:body"]}],
        }))
        source_run_manifest = root / "16_03_run_manifest.json"
        source_run_manifest.write_text(json.dumps({"schema_version": "fixture"}))
        source_text = case_id + "\0Fix the visual state\0Broken visual state"
        (root / "16_04_04_translations_zh.json").write_text(json.dumps({
            "schema_version": "human-review-zh-translations-v1",
            "source_run_manifest_sha256": sha(source_run_manifest),
            "items": [{
                "case_id": case_id,
                "pr_title_zh": "修复视觉状态",
                "problem_statement_zh": "视觉状态损坏",
                "source_text_sha256": hashlib.sha256(source_text.encode()).hexdigest(),
            }],
        }))
        classification = root / "classification.json"
        classification.write_text("{}")
        record = {
            "case_id": case_id,
            "change_scale": {
                "schema_version": "reference-change-scale-v1",
                "label": "中规模修改",
                "reason": "2–4 个生产源代码文件，清洗后增删行数不超过 100",
                "cleaned_source_file_count": 2,
                "cleaned_changed_lines": 42,
                "raw_changed_file_count": 3,
                "raw_changed_lines": 52,
                "production_files": [
                    {"filename": "src/a.ts", "status": "modified",
                     "additions": 20, "deletions": 10, "changed_lines": 30},
                    {"filename": "src/b.ts", "status": "modified",
                     "additions": 10, "deletions": 2, "changed_lines": 12},
                ],
                "excluded_files": [
                    {"filename": "src/a.test.ts", "status": "modified",
                     "additions": 5, "deletions": 5, "changed_lines": 10,
                     "exclusion_reason": "test_code"},
                ],
                "human_review_required": False,
                "limitations": ["Fixture limitation"],
            },
            "visual_capability": {"status": "complete", "annotation": {
                "assets": [{
                    "asset_id": asset_id, "solver_visible_role": "实际状态",
                    "ocr_transcription_sufficient": "否", "task_relevance": "相关",
                    "observation": "A non-text rendering defect",
                }],
                "atomic_visual_constraints": [{"constraint_id": "c1"}],
            }},
        }
        qualification = {
            "classification_packet": str(packet),
            "classification_packet_sha256": sha(packet),
            "source_result": str(source_result), "source_result_sha256": sha(source_result),
            "source_packet": str(source_packet), "source_packet_sha256": sha(source_packet),
            "source_archive": str(archive), "source_archive_sha256": sha(archive),
            "classification": str(classification), "classification_sha256": sha(classification),
        }
        row = {
            "case_id": case_id, "counted": True,
            "primary_visual_category": "外观与渲染属性理解",
            "category_purity": "单一能力题", "evidence_mode": "实际错误图",
            "strict_multimodal_admission": "非文字视觉信息候选不可替代",
            "admission_reason": "Pixels define the defect.",
            "classification_reason": "Appearance is primary.",
            "contributing_visual_categories": ["外观与渲染属性理解"],
            "source_qualification": qualification,
        }
        distribution = root / "distribution.json"
        distribution.write_text(json.dumps({
            "schema_version": "visual-category-distribution-v3",
            "classifications": [{"path": str(classification), "sha256": sha(classification)}],
            "rows": [row],
        }))
        return distribution, classification, record, case_id, asset_id

    def test_render_audit_and_bound_human_export(self):
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            distribution, classification, record, case_id, asset_id = self.fixture(root)
            output = root / "review"
            with patch.object(subject, "_classification_runs", return_value=[
                    (classification, root / "source_run", {"records": [record]})]):
                result = subject.render(distribution, output)
            self.assertEqual("passed", result["audit"]["status"])
            self.assertEqual(1, result["candidate_count"])
            self.assertTrue((output / "16_04_03_visual_gate_review.html").is_file())
            payload = json.loads((output / "16_04_01_review_payload.json").read_text())
            case = payload["cases"][0]
            self.assertEqual(
                "https://github.com/owner/repo/issues/6",
                case["problem_sources"][0]["issue_url"],
            )
            self.assertEqual("视觉状态损坏", case["problem_statement_zh"])
            self.assertEqual("available", case["translation"]["status"])
            self.assertEqual("中规模修改", case["change_scale"]["label"])
            self.assertEqual(42, case["change_scale"]["cleaned_changed_lines"])
            page = (output / "16_04_03_visual_gate_review.html").read_text()
            self.assertIn('class="issue-link"', page)
            self.assertIn('target="_blank"', page)
            self.assertIn("中文翻译 · 仅供审核对照", page)
            self.assertIn('id="review-basis"', page)
            self.assertNotIn('id="non-text"', page)
            self.assertNotIn('id="reason"', page)
            self.assertIn("function reviewBasis", page)
            self.assertIn("参考代码修改量", page)
            self.assertIn("小：1 个生产文件且 ≤100 行", page)
            self.assertIn("V4 多标签能力建议", page)
            self.assertIn('value="__multi__"', page)
            self.assertIn("data-preview-frame", page)
            self.assertIn("function readSaved()", page)
            self.assertIn("本页内暂存；请导出 JSON", page)
            self.assertNotIn("saved=JSON.parse(localStorage.getItem", page)
            self.assertIn("(payloadNode.content||payloadNode).textContent.trim()", page)
            self.assertIn("new TextDecoder('utf-8',{fatal:true})", page)
            decisions = root / "decisions.json"
            decisions.write_text(json.dumps({
                "schema_version": "visual-gate-human-export-v1",
                "source_manifest_sha256": payload["source_manifest_sha256"],
                "exported_at": "2026-09-02T00:00:00Z",
                "rows": [{
                    "case_id": case_id,
                    "candidate_binding_sha256": case["candidate_binding_sha256"],
                    "source_route": "issue_derived",
                    "problem_statement": "Broken visual state",
                    "problem_statement_leak_free": True,
                    "text_only_sufficient": "no", "ocr_replaceable": "no",
                    "non_text_visual_fact": "The pixels define the wrong color.",
                    "images": [{
                        "asset_id": asset_id, "role": "before_only",
                        "solver_visible": True, "contains_fixed_after": False,
                        "contains_solution_evidence": False, "crop_required": False,
                        "reason": "Shows the bug",
                    }],
                    "decision": "keep", "reason": "Image is necessary and safe.",
                    "reviewed_at": "2026-09-02T00:00:00Z",
                }],
            }))
            audited = subject.audit(output, decisions)
            self.assertEqual({"keep": 1, "exclude": 0, "needs_review": 0},
                             audited["human_export"]["counts"])

    def test_keep_rejects_after_only_solver_visible_image(self):
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            distribution, classification, record, case_id, asset_id = self.fixture(root)
            output = root / "review"
            with patch.object(subject, "_classification_runs", return_value=[
                    (classification, root / "source_run", {"records": [record]})]):
                subject.render(distribution, output)
            payload = json.loads((output / "16_04_01_review_payload.json").read_text())
            decisions = root / "unsafe.json"
            decisions.write_text(json.dumps({
                "schema_version": "visual-gate-human-export-v1",
                "source_manifest_sha256": payload["source_manifest_sha256"],
                "exported_at": "now",
                "rows": [{
                    "case_id": case_id,
                    "candidate_binding_sha256": payload["cases"][0]["candidate_binding_sha256"],
                    "source_route": "issue_derived", "problem_statement": "problem",
                    "problem_statement_leak_free": True, "text_only_sufficient": "no",
                    "ocr_replaceable": "no", "non_text_visual_fact": "geometry",
                    "images": [{"asset_id": asset_id, "role": "after_only",
                                "solver_visible": True, "contains_fixed_after": False,
                                "contains_solution_evidence": False,
                                "crop_required": False, "reason": "unsafe"}],
                    "decision": "keep", "reason": "incorrect", "reviewed_at": "now",
                }],
            }))
            with self.assertRaisesRegex(ValueError, "unsafe or unresolved"):
                subject.validate_human_export(output, decisions)

    def test_live_server_externalizes_payload_and_atomically_saves_review(self):
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            distribution, classification, record, case_id, asset_id = self.fixture(root)
            output = root / "review"
            with patch.object(subject, "_classification_runs", return_value=[
                    (classification, root / "source_run", {"records": [record]})]):
                subject.render(distribution, output)
            config = root / "config.json"
            config.write_text(json.dumps({
                "schema_version": "visual-review-live-config-v1",
                "distribution": str(distribution), "prebuilt_bundle": str(output),
            }))
            state = live_subject.ReviewState(config, root / "live")
            bundle, manifest, payload, _ = state.bundle()
            page = live_subject._dynamic_page(
                (bundle / "16_04_03_visual_gate_review.html").read_text())
            self.assertIn("fetch('/api/data'", page)
            self.assertIn("测试覆盖 Verifier", page)
            self.assertNotIn("const DATA=JSON.parse(atob", page)
            case = payload["cases"][0]
            result = state.save({
                "schema_version": "visual-gate-human-export-v1",
                "source_manifest_sha256": manifest["source_manifest_sha256"],
                "exported_at": "2026-09-02T00:00:00Z",
                "rows": [{
                    "case_id": case_id,
                    "candidate_binding_sha256": case["candidate_binding_sha256"],
                    "source_route": "issue_derived",
                    "problem_statement": "Broken visual state",
                    "problem_statement_leak_free": False,
                    "text_only_sufficient": "unclear", "ocr_replaceable": "unclear",
                    "non_text_visual_fact": "",
                    "images": [{"asset_id": asset_id, "role": "before_only",
                                "solver_visible": True, "contains_fixed_after": False,
                                "contains_solution_evidence": False,
                                "crop_required": False, "reason": "pending"}],
                    "decision": "needs_review", "reason": "Needs human review.",
                    "reviewed_at": "2026-09-02T00:00:00Z",
                }],
            })
            self.assertEqual("saved", result["status"])
            self.assertTrue(Path(result["path"]).is_file())
            self.assertEqual(1, result["audit"]["reviewed_count"])

    def test_live_server_accepts_self_contained_review_and_injects_test_controls(self):
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            distribution, classification, record, case_id, _ = self.fixture(root)
            legacy = root / "legacy"
            with patch.object(subject, "_classification_runs", return_value=[
                    (classification, root / "source_run", {"records": [record]})]):
                subject.render(distribution, legacy)
            bundle = root / "portable"
            shutil.copytree(legacy, bundle)
            shutil.copy2(bundle / "16_04_03_visual_gate_review.html", bundle / "index.html")
            shutil.copy2(bundle / "16_04_01_review_payload.json", bundle / "metadata.json")
            payload = json.loads((bundle / "metadata.json").read_text())
            asset_count = sum(len(case["assets"]) for case in payload["cases"])
            (bundle / "manifest.json").write_text(json.dumps({
                "schema_version": "self-contained-visual-review-v1",
                "entrypoint": "index.html", "entrypoint_sha256": sha(bundle / "index.html"),
                "metadata": "metadata.json", "metadata_sha256": sha(bundle / "metadata.json"),
                "candidate_count": len(payload["cases"]),
                "asset_file_count": asset_count,
                "source_manifest_sha256": payload["source_manifest_sha256"],
            }))
            config = root / "config.json"
            config.write_text(json.dumps({
                "schema_version": "visual-review-live-config-v1",
                "standalone_bundle": str(bundle),
            }))
            state = live_subject.ReviewState(config, root / "state")
            resolved, manifest, live_payload, distribution_path = state.bundle()
            self.assertEqual(bundle.resolve(), resolved)
            self.assertEqual(case_id, live_payload["cases"][0]["case_id"])
            self.assertEqual(bundle / "metadata.json", distribution_path)
            self.assertEqual(1, manifest["candidate_count"])
            page = live_subject._dynamic_page(
                live_subject._bundle_page(resolved).read_text())
            self.assertIn("生成 / 重新整理测试脚本", page)
            self.assertIn("PR 作者随本 PR 提交/修改的测试", page)

    def test_live_server_translates_one_bound_case_and_reuses_cache(self):
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            distribution, classification, record, case_id, _ = self.fixture(root)
            (root / "16_04_04_translations_zh.json").unlink()
            output = root / "review"
            with patch.object(subject, "_classification_runs", return_value=[
                    (classification, root / "source_run", {"records": [record]})]):
                subject.render(distribution, output)
            key_file = root / "key.env"
            key_file.write_text("AIDP_API_KEY=unused-test-key\n")
            config = root / "config.json"
            config.write_text(json.dumps({
                "schema_version": "visual-review-live-config-v1",
                "distribution": str(distribution), "prebuilt_bundle": str(output),
                "translation": {
                    "backend": "gemini", "key_file": str(key_file),
                    "attempts": 2, "timeout": 30,
                },
            }))
            state = live_subject.ReviewState(config, root / "live")
            _, manifest, payload, _ = state.bundle()
            self.assertEqual("", payload["cases"][0]["problem_statement_zh"])

            class FakeEvaluator:
                backend = "gemini"
                profile = {"model": "fake-translation-model"}
                attempts = 1

                def __call__(self, **kwargs):
                    self.packet = kwargs["packet"]
                    return ({"translations": [{
                        "case_id": case_id,
                        "pr_title_zh": "修复视觉状态",
                        "problem_statement_zh": "视觉状态损坏",
                    }]}, {"backend": "gemini", "requested_model": "fake"})

            evaluator = FakeEvaluator()
            result = state.translate({
                "source_manifest_sha256": manifest["source_manifest_sha256"],
                "case_id": case_id,
            }, evaluator=evaluator)
            self.assertEqual("translated", result["status"])
            self.assertEqual("视觉状态损坏", result["problem_statement_zh"])
            self.assertEqual(case_id, evaluator.packet["items"][0]["case_id"])
            _, _, refreshed, _ = state.bundle()
            self.assertEqual("视觉状态损坏",
                             refreshed["cases"][0]["problem_statement_zh"])
            self.assertEqual(1, refreshed["live_meta"]["on_demand_translation_count"])
            cached = state.translate({
                "source_manifest_sha256": manifest["source_manifest_sha256"],
                "case_id": case_id,
            })
            self.assertEqual("cached", cached["status"])
            page = live_subject._dynamic_page(
                (output / "16_04_03_visual_gate_review.html").read_text())
            self.assertIn('id="translate-case"', page)
            self.assertIn("fetch('/api/translate'", page)

    def test_test_coverage_runs_provisionally_before_keep_and_records_gate_state(self):
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            distribution, classification, record, case_id, asset_id = self.fixture(root)
            output = root / "review"
            with patch.object(subject, "_classification_runs", return_value=[
                    (classification, root / "source_run", {"records": [record]})]):
                subject.render(distribution, output)
            key_file = root / "key.env"
            key_file.write_text("AIDP_API_KEY=server-only-test-key\n")
            task = root / "task"
            task.mkdir()
            source_measurement = root / "source-measurement.json"
            browser_measurement = root / "browser-measurement.json"
            source_measurement.write_text("{}")
            browser_measurement.write_text("{}")
            config = root / "config.json"
            config.write_text(json.dumps({
                "schema_version": "visual-review-live-config-v1",
                "distribution": str(distribution), "prebuilt_bundle": str(output),
                "test_coverage": {
                    "backend": "gemini", "key_file": str(key_file),
                    "max_concurrency": 2,
                    "cases": {case_id: {
                        "mode": "harbor", "task": str(task),
                        "source_measurement": str(source_measurement),
                        "browser_measurement": str(browser_measurement),
                    }},
                },
            }))
            state = live_subject.ReviewState(config, root / "live")
            bundle, manifest, payload, _ = state.bundle()
            self.assertEqual("ready_provisional",
                             state.test_coverage_status(case_id)["status"])
            calls = {}

            def fake_runner(**kwargs):
                calls.update(kwargs)
                kwargs["output"].mkdir(parents=True)
                audit_path = kwargs["output"] / "20_11_08_audit.html"
                audit_path.write_text("<p>functional coverage only</p>")
                return {"status": "complete", "audit": {"path": str(audit_path)}}

            class FakeEvaluator:
                pass

            provisional = state.trigger_test_coverage({
                "source_manifest_sha256": manifest["source_manifest_sha256"],
                "case_id": case_id,
            }, launch=False, evaluator_factory=lambda _: FakeEvaluator(),
               verifier_runner=fake_runner)
            self.assertEqual("complete", provisional["status"])
            self.assertTrue(provisional["provisional"])
            self.assertEqual("pending", provisional["visual_gate_status"])
            self.assertTrue(calls["provisional"])
            provisional_input = json.loads(Path(provisional["decision_path"]).read_text())
            self.assertEqual("visual-gate-provisional-input-v1",
                             provisional_input["schema_version"])
            self.assertEqual("needs_review",
                             provisional_input["rows"][0]["decision"])
            case = payload["cases"][0]
            state.save({
                "schema_version": "visual-gate-human-export-v1",
                "source_manifest_sha256": manifest["source_manifest_sha256"],
                "exported_at": "2026-09-02T00:00:00Z",
                "rows": [{
                    "case_id": case_id,
                    "candidate_binding_sha256": case["candidate_binding_sha256"],
                    "source_route": "issue_derived",
                    "problem_statement": "Broken visual state",
                    "problem_statement_leak_free": True,
                    "text_only_sufficient": "no", "ocr_replaceable": "no",
                    "non_text_visual_fact": "The pixels define the wrong color.",
                    "images": [{"asset_id": asset_id, "role": "before_only",
                                "solver_visible": True, "contains_fixed_after": False,
                                "contains_solution_evidence": False,
                                "crop_required": False, "reason": "Shows the bug"}],
                    "decision": "keep", "reason": "Image is necessary and safe.",
                    "reviewed_at": "2026-09-02T00:00:00Z",
                }],
            })
            ready = state.test_coverage_status(case_id)
            self.assertTrue(ready["eligible"])
            self.assertTrue(ready["visual_gate_approved"])
            refreshed_payload = state.bundle()[2]
            self.assertEqual("keep", refreshed_payload["cases"][0][
                "server_decision"]["decision"])
            self.assertEqual(1, refreshed_payload["live_meta"][
                "server_decision_count"])

            result = state.trigger_test_coverage({
                "source_manifest_sha256": manifest["source_manifest_sha256"],
                "case_id": case_id,
            }, launch=False, evaluator_factory=lambda _: FakeEvaluator(),
               verifier_runner=fake_runner)
            self.assertEqual("complete", result["status"])
            self.assertFalse(result["provisional"])
            self.assertEqual("approved", result["visual_gate_status"])
            self.assertFalse(calls["provisional"])
            self.assertEqual(task.resolve(), calls["task"])
            self.assertEqual(classification.resolve(), calls["classification"])
            self.assertNotIn("server-only-test-key", json.dumps(result))
            status = state.test_coverage_status(case_id)
            self.assertTrue(status["job"]["audit_url"].endswith("20_11_08_audit.html"))
            page = live_subject._dynamic_page(
                (bundle / "16_04_03_visual_gate_review.html").read_text())
            self.assertIn("运行测试覆盖 Verifier", page)
            self.assertIn("test_inputs_missing", page)
            self.assertIn("ready_provisional", page)
            self.assertNotIn("button.disabled=!out.eligible", page)
            self.assertIn("预测绝不是最终 F2P/P2P", page)
            self.assertIn("if(c.server_decision)saved[c.case_id]=c.server_decision", page)
            self.assertNotIn("server-only-test-key", page)

    def test_unmapped_case_reports_test_inputs_missing(self):
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            distribution, classification, record, case_id, _ = self.fixture(root)
            output = root / "review"
            with patch.object(subject, "_classification_runs", return_value=[
                    (classification, root / "source_run", {"records": [record]})]):
                subject.render(distribution, output)
            config = root / "config.json"
            config.write_text(json.dumps({
                "schema_version": "visual-review-live-config-v1",
                "distribution": str(distribution), "prebuilt_bundle": str(output),
            }))
            state = live_subject.ReviewState(config, root / "live")
            self.assertEqual("test_inputs_missing",
                             state.test_coverage_status(case_id)["status"])
            with self.assertRaisesRegex(ValueError, "test_inputs_missing"):
                state.trigger_test_coverage({
                    "source_manifest_sha256": state.bundle()[1]["source_manifest_sha256"],
                    "case_id": case_id,
                }, launch=False)

    def test_unified_flow_loads_archived_native_generated_and_measurement_evidence(self):
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            distribution, classification, record, case_id, _ = self.fixture(root)
            output = root / "review"
            with patch.object(subject, "_classification_runs", return_value=[
                    (classification, root / "source_run", {"records": [record]})]):
                subject.render(distribution, output)
            evidence_root = root / "cases"
            run = evidence_root / case_id / "03_test_construction/20_11_verifier_run_01"
            run.mkdir(parents=True)
            (run / "20_11_09_manifest.json").write_text("{}")
            measurement_file = evidence_root / case_id / "04_measurements/result.json"
            measurement_file.parent.mkdir()
            measurement_file.write_text('{"reward": 1}')
            case_manifest = evidence_root / case_id / "00_case_manifest.json"
            case_manifest.write_text(json.dumps({
                "state": "candidate",
                "sections": {
                    "test_construction": [{
                        "path": "03_test_construction/20_11_verifier_run_01/20_11_09_manifest.json",
                        "sha256": sha(run / "20_11_09_manifest.json"),
                        "size_bytes": (run / "20_11_09_manifest.json").stat().st_size,
                        "storage": "generated",
                    }],
                    "measurements": [{
                        "path": "04_measurements/result.json",
                        "sha256": sha(measurement_file),
                        "size_bytes": measurement_file.stat().st_size,
                        "storage": "copied",
                    }],
                },
                "pipeline_status": {
                    "harbor_empty": "complete", "harbor_oracle": "complete",
                },
                "blockers": [],
            }))
            config = root / "config.json"
            config.write_text(json.dumps({
                "schema_version": "visual-review-live-config-v1",
                "distribution": str(distribution), "prebuilt_bundle": str(output),
                "test_evidence_root": str(evidence_root),
            }))
            state = live_subject.ReviewState(config, root / "live")
            archived = {
                "verifier_status": "additional_tests_proposed",
                "verifier_summary": "One visual constraint needs a browser assertion.",
                "constraints": [{"constraint_id": "c1", "description": "layout",
                                 "coverage": {"assertion_summary": "measure x positions",
                                              "reason": "functional geometry"}}],
                "existing_test_files": [{"path": "test/native.js", "sha256": "a" * 64,
                                         "content": "it('native', () => {})"}],
                "bundles": [{"bundle_id": "bundle_01", "predicted_transition": "F2P",
                             "why_assertions_measure_requirements": "checks geometry",
                             "unified_test_patch": "+it('generated', () => {})", "files": []}],
                "measurement": {
                    "transitions": [{"test_id": "native", "class": "P2P",
                                     "base_status": "pass", "gold_status": "pass",
                                     "matches": True}],
                    "approval_eligible": True, "approval_blockers": [],
                    "f2p": 1, "p2p": 1, "repeats_per_state": 3,
                },
            }
            with patch("report_pipeline.test_review_ui._load_case", return_value=archived):
                flow = state.test_flow_evidence(case_id)
            self.assertTrue(flow["evidence_available"])
            self.assertEqual("it('native', () => {})",
                             flow["verifier"]["repository_context_files"][0]["content"])
            self.assertEqual("complete", flow["pipeline"]["tests_measured"])
            self.assertEqual("complete", flow["pipeline"]["tests_approved"])
            self.assertEqual("complete", flow["pipeline"]["harbor_controls_passed"])
            self.assertIn("test_construction", flow["archive_artifacts"])
            self.assertEqual(measurement_file.resolve(),
                             state.case_artifact(case_id, "04_measurements/result.json"))
            with self.assertRaises(FileNotFoundError):
                state.case_artifact(case_id, "00_case_manifest.json")
            live_output = root / "live/20_11_test_coverage_jobs/job_live/output"
            live_output.mkdir(parents=True)
            (live_output / "20_11_09_manifest.json").write_text("{}")
            state._write_coverage_job({
                "job_id": "job_live", "case_id": case_id, "status": "complete",
                "output": str(live_output),
            })
            with patch("report_pipeline.test_review_ui._load_case", return_value=archived):
                live_flow = state.test_flow_evidence(case_id)
            self.assertEqual("live_job", live_flow["evidence_source"])
            self.assertEqual(str(live_output.resolve()), live_flow["evidence_directory"])
            page = live_subject._dynamic_page(
                (output / "16_04_03_visual_gate_review.html").read_text())
            self.assertIn("统一流程与测试证据", page)
            self.assertIn("PR 作者随本 PR 提交/修改的测试", page)
            self.assertIn("Verifier 生成的候选测试", page)
            self.assertNotIn("本轮 Verifier 前由造题流程生成的测试", page)
            self.assertNotIn("本轮 Verifier 新生成测试", page)
            self.assertIn(
                "c.case_id!==current().case_id||out.case_id!==c.case_id", page)
            self.assertIn("已加载归档测试证据", page)
            self.assertIn("/api/test-flow?case_id=", page)
            self.assertIn("!out.evidence_available?5000:null", page)
            self.assertIn("c.case_id===current().case_id", page)


if __name__ == "__main__":
    unittest.main()
