import json
import tempfile
import unittest
from pathlib import Path

from report_pipeline.candidate import build
from report_pipeline import pre_review_classification as classification_subject
from tests import test_pre_review_classification as fixture_module


class CandidateDossierTest(unittest.TestCase):
    STRICT_VISUAL_LABEL = "非文字视觉信息候选不可替代"

    @staticmethod
    def _fixture(root: Path):
        source_run, classification = (
            fixture_module.PromptContractTests._copy_frozen_classification(root))
        verifier = source_run / "16_03_result_0001.json"
        result = json.loads(verifier.read_text())
        packet_path = Path(result["packet"])
        packet = json.loads(packet_path.read_text())
        archive_path = Path(packet["provenance"]["source_archive"])
        archive = json.loads(archive_path.read_text())
        archive.update({"repo": "owner/repo", "number": 1,
                        "instance_id": "owner__repo-1", "status": "complete"})
        archive["sections"].update({
            "pull_request": {"data": {"html_url": "https://example.test/pull/1",
                "title": "Fix visual defect", "merged_at": "2026-01-01T00:00:00Z",
                "base": {"ref": "main", "sha": "a" * 40,
                         "repo": {"default_branch": "main"}},
                "head": {"sha": "c" * 40}}},
            "merge_commit": {"data": {"sha": "d" * 40,
                                         "parents": [{"sha": "b" * 40}]}},
            "merge_anchor_evidence": {"resolved_sha": "d" * 40},
        })
        archive_path.write_text(json.dumps(archive))
        packet["provenance"]["source_archive_sha256"] = classification_subject._sha(archive_path)
        packet_path.write_text(json.dumps(packet))
        result.update({
            "repository": "owner/repo", "pr_number": 1, "status": "complete",
            "packet_sha256": classification_subject._sha(packet_path),
            "visual_verifier": {"decision": {"bucket": "visual_necessary"},
                "annotation": {"quality": {"leakage_risk": "low"}},
                "result_path": "fixture-visual-result.json"},
            "annotation": {"confidence": "high"},
            "text_decision": {"bucket": "visual_candidate"},
            "reconciliation": {"reason_code": "legacy_visual_signal"},
        })
        verifier.write_text(json.dumps(result))
        manifest = json.loads(classification.read_text())
        record = manifest["records"][0]
        record["source_result_sha256"] = classification_subject._sha(verifier)
        record["source_packet_sha256"] = classification_subject._sha(packet_path)
        record["source_archive_sha256"] = classification_subject._sha(archive_path)
        record["change_scale"] = classification_subject.classify_change_scale(
            archive["sections"]["files"]["items"])
        classification.write_text(json.dumps(manifest))
        CandidateDossierTest._set_capability(
            classification, status="complete",
            label=CandidateDossierTest.STRICT_VISUAL_LABEL)
        return verifier, archive_path, classification

    @staticmethod
    def _set_capability(classification: Path, *, status: str,
                        label: str | None = None) -> None:
        value = json.loads(classification.read_text())
        capability = value["records"][0]["visual_capability"]
        if status != "complete":
            capability.update({"status": status, "annotation": None,
                               "invocation": None, "reason": "fixture unresolved"})
            value["human_review_ready"] = status == "ineligible"
            value["model_contracts"] = []
        else:
            if label is None:
                raise ValueError("complete V3 fixture requires an admission label")
            code_root = Path(classification_subject.__file__).resolve().parents[1]
            prompt = classification.parent / "16_03_05_visual_capability.system.md"
            schema = classification.parent / "16_03_06_visual_capability.schema.json"
            prompt.write_bytes((code_root / (
                "analysis/prompts/20_07_visual_capability_classifier_v3.system.md"
            )).read_bytes())
            schema.write_bytes((code_root / (
                "analysis/prompts/20_08_visual_capability_classifier_v3.schema.json"
            )).read_bytes())
            value["prompt_sha256"] = classification_subject._sha(prompt)
            value["schema_sha256"] = classification_subject._sha(schema)

            packet = json.loads(Path(value["records"][0]["packet"]).read_text())
            strict = label == CandidateDossierTest.STRICT_VISUAL_LABEL
            asset_ids = [item["asset_id"] for item in packet["assets"]]
            constraints = ([{
                "constraint_id": "constraint_001",
                "description": "边框间距必须与图中可见几何关系一致",
                "visual_category": "空间布局与几何理解",
                "evidence_asset_ids": asset_ids,
                "direct_visual_evidence": "图中直接显示边框与相邻元素的间距",
                "prose_already_complete": "否",
                "decision_critical": "是",
                "counterfactual_ambiguity": "移除图像后无法唯一确定目标间距",
            }] if strict else [])
            annotation = {
                "schema_version": "visual-capability-classifier-v3",
                "task_id": packet["task_id"],
                "strict_multimodal_admission": label,
                "admission_reason": (
                    "像素提供文字未完整描述的几何约束" if strict
                    else "题面文字已经足以确定修复要求"),
                "assets": [{
                    "asset_id": asset_id,
                    "observed": True,
                    "solver_visible_role": "期望目标",
                    "task_relevance": "相关",
                    "ocr_transcription_sufficient": "否" if strict else "是",
                    "observation": "图像展示了题目所指的边框视觉状态",
                } for asset_id in asset_ids],
                "atomic_visual_constraints": constraints,
                "primary_visual_category": "空间布局与几何理解" if strict else None,
                "category_purity": "单一能力题" if strict else None,
                "contributing_visual_categories": (
                    ["空间布局与几何理解"] if strict else []),
                "evidence_mode": "单张期望目标图",
                "classification_reason": (
                    "目标间距需要空间布局理解" if strict
                    else "图像不提供不可替代的非文字约束"),
                "human_review_required": False,
                "human_review_reasons": [],
            }
            capability.update({"status": "complete", "annotation": annotation})
            capability.pop("reason", None)
            invocation = capability["invocation"]
            invocation["prompt_sha256"] = value["prompt_sha256"]
            invocation["schema_sha256"] = value["schema_sha256"]
            request = Path(invocation["request"])
            request_value = json.loads(request.read_text())
            request_value["messages"][0]["content"] = (
                prompt.read_text() + "\nOutput JSON matching this schema:\n"
                + schema.read_text())
            request.write_text(json.dumps(request_value))
            invocation["request_sha256"] = classification_subject._sha(request)
            trace = Path(invocation["trace"])
            trace_value = json.loads(trace.read_text())
            trace_value.update({
                "prompt_sha256": value["prompt_sha256"],
                "schema_sha256": value["schema_sha256"],
                "request_sha256": invocation["request_sha256"],
            })
            trace.write_text(json.dumps(trace_value))
            invocation["trace_sha256"] = classification_subject._sha(trace)
            raw = Path(invocation["raw_response"])
            raw.write_text(json.dumps(annotation))
            invocation["raw_response_sha256"] = classification_subject._sha(raw)
            provider = Path(invocation["provider_response"])
            provider_value = json.loads(provider.read_text())
            provider_value["choices"][0]["message"]["content"] = json.dumps(annotation)
            provider.write_text(json.dumps(provider_value))
            invocation["provider_response_sha256"] = classification_subject._sha(provider)
            attempt = Path(invocation["attempt_receipt"])
            attempt_value = json.loads(attempt.read_text())
            attempt_value["response_sha256"] = invocation["provider_response_sha256"]
            attempt.write_text(json.dumps(attempt_value))
            invocation["attempt_receipt_sha256"] = classification_subject._sha(attempt)
        classification.write_text(json.dumps(value))

    def test_v3_strict_candidate_can_enter_test_construction(self):
        with tempfile.TemporaryDirectory() as directory:
            verifier, archive, classification = self._fixture(Path(directory))
            result = build(verifier, archive, classification)
        self.assertEqual("admitted_to_test_construction", result["status"])
        self.assertEqual("v3_strict_nontext_visual",
                         result["visual_admission"]["admission_route"])
        self.assertEqual("visual-v3-strict-nontext-v1",
                         result["visual_admission"]["selection_policy"]["policy_id"])
        self.assertIsNone(result["visual_admission"]["confidence"])
        self.assertEqual("v3_classifier_has_no_confidence_field",
                         result["visual_admission"]["confidence_semantics"])
        self.assertTrue(result["benchmark_eligibility"]["may_construct_and_measure_tests"])
        self.assertFalse(result["benchmark_eligibility"]["may_enter_final_taskset"])

    def test_v3_strict_candidate_survives_legacy_text_verifier_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            verifier, archive, classification = self._fixture(Path(directory))
            value = json.loads(verifier.read_text())
            value.update({"status": "failed", "error": "fixture schema failure"})
            value.pop("annotation")
            value.pop("text_decision")
            value.pop("reconciliation")
            verifier.write_text(json.dumps(value))
            manifest = json.loads(classification.read_text())
            manifest["records"][0]["source_result_sha256"] = (
                classification_subject._sha(verifier))
            classification.write_text(json.dumps(manifest))
            result = build(verifier, archive, classification)
        self.assertEqual("admitted_to_test_construction", result["status"])
        self.assertTrue(result["visual_admission"]["upstream_text_verifier"]
                        ["technical_failure"])
        self.assertEqual("unavailable_due_upstream_technical_failure",
                         result["visual_admission"]["text_only_bucket"])

    def test_v3_negative_overrides_legacy_auto_admission(self):
        with tempfile.TemporaryDirectory() as directory:
            verifier, archive, classification = self._fixture(Path(directory))
            self._set_capability(
                classification, status="complete", label="图片有帮助但文字已足够")
            result = build(verifier, archive, classification)
        self.assertEqual("review_or_exclude", result["status"])
        self.assertEqual("v3_review_or_exclude",
                         result["visual_admission"]["admission_route"])
        self.assertFalse(result["benchmark_eligibility"]["may_construct_and_measure_tests"])

    def test_noncomplete_v3_record_returns_review_instead_of_crashing(self):
        with tempfile.TemporaryDirectory() as directory:
            verifier, archive, classification = self._fixture(Path(directory))
            self._set_capability(classification, status="requires_video_review")
            result = build(verifier, archive, classification)
        self.assertEqual("review_or_exclude", result["status"])
        self.assertEqual("requires_video_review",
                         result["visual_admission"]["v3_classification"]["status"])

    def test_formal_candidate_requires_v3_and_legacy_is_review_only(self):
        with tempfile.TemporaryDirectory() as directory:
            verifier, archive, _ = self._fixture(Path(directory))
            with self.assertRaisesRegex(ValueError, "require.*V3"):
                build(verifier, archive)
            result = build(verifier, archive, allow_legacy_migration=True)
        self.assertEqual("review_or_exclude", result["status"])
        self.assertEqual("legacy_migration_review_only",
                         result["visual_admission"]["admission_route"])
        self.assertTrue(result["visual_admission"]["selection_policy"]
                        ["formal_admission_prohibited"])
        self.assertFalse(result["benchmark_eligibility"]["may_construct_and_measure_tests"])

    def test_mismatched_verifier_and_archive_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            verifier, archive, _ = self._fixture(root)
            value = json.loads(verifier.read_text())
            value["pr_number"] = 2
            tampered = root / "tampered-verifier.json"
            tampered.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "identities differ"):
                build(tampered, archive, allow_legacy_migration=True)

    def test_curator_cannot_promote_pr_asset_to_agent_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            verifier, archive, _ = self._fixture(root)
            value = json.loads(verifier.read_text())
            curator = json.loads(Path(value["curator_assets"]).read_text())
            curator["assets"][0]["source_ids"] = ["pr:body"]
            curator_path = root / "tampered-curator.json"
            curator_path.write_text(json.dumps(curator))
            value["curator_assets"] = str(curator_path)
            value["curator_assets_sha256"] = classification_subject._sha(curator_path)
            tampered = root / "tampered-verifier.json"
            tampered.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "unsafe or unbound"):
                build(tampered, archive, allow_legacy_migration=True)

    def test_archive_root_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            verifier, archive, _ = self._fixture(root)
            real_root = archive.parent / "real-assets"
            (archive.parent / "11_http_archive").rename(real_root)
            (archive.parent / "11_http_archive").symlink_to(real_root, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "root must not be a symlink"):
                build(verifier, archive, allow_legacy_migration=True)


if __name__ == "__main__":
    unittest.main()
