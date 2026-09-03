import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from report_pipeline.capability_candidate_pool import (
    CATEGORIES, _capabilities, _render_page, _translation_source_sha, build,
    render_snapshot,
)


def annotation(case_id: str, categories: list[str]) -> dict:
    return {
        "schema_version": "visual-capability-classifier-v4",
        "task_id": case_id,
        "visual_capabilities": [{
            "category": category,
            "importance": "core" if index == 0 else "supporting",
            "visual_evidence": f"evidence {category}",
            "task_relation": f"relation {category}",
        } for index, category in enumerate(categories)],
    }


class CapabilityCandidatePoolTests(unittest.TestCase):
    def test_legacy_domain_constraint_is_review_only_without_requesting_reclassification(self):
        capabilities, migrated, warnings = _capabilities({
            "schema_version": "visual-capability-classifier-v3",
            "strict_multimodal_admission": "非文字视觉信息候选不可替代",
            "human_review_required": False,
            "atomic_visual_constraints": [
                {"visual_category": "外观与渲染属性理解",
                 "decision_critical": "是", "direct_visual_evidence": "color",
                 "description": "preserve color"},
                {"visual_category": "图形符号与领域语义理解",
                 "decision_critical": "否", "direct_visual_evidence": "symbol",
                 "description": "interpret symbol"},
            ],
        })
        self.assertTrue(migrated)
        self.assertEqual([item["category"] for item in capabilities],
                         ["rendering_appearance_understanding"])
        self.assertIn("进入复核", warnings[0])
        self.assertNotIn("重新分类", warnings[0])

    @patch("report_pipeline.capability_candidate_pool._archive_and_assets")
    @patch("report_pipeline.capability_candidate_pool.validate_capability_run")
    def test_builds_four_capability_multi_label_pool(self, validate, archive_assets):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "capability-run"
            run.mkdir()
            (run / "16_11_03_capability_results.json").write_text("{}\n")
            asset_path = root / "image.png"
            asset_path.write_bytes(b"visual")
            asset_id = hashlib.sha256(asset_path.read_bytes()).hexdigest()
            cases = [
                ("org__repo-1", [CATEGORIES[0], CATEGORIES[1]]),
                ("org__repo-2", [CATEGORIES[1], CATEGORIES[2]]),
                ("org__repo-3", [CATEGORIES[2], CATEGORIES[3]]),
                ("org__repo-4", [CATEGORIES[3], CATEGORIES[0]]),
            ]
            records = []
            for case_id, categories in cases:
                packet = root / f"{case_id}.json"
                packet.write_text(json.dumps({
                    "task_id": case_id, "problem_statement": "full issue text",
                    "assets": [{"asset_id": asset_id, "source_ids": ["issue:x#1:body"]}],
                }))
                records.append({
                    "case_id": case_id, "status": "complete",
                    "annotation": annotation(case_id, categories),
                    "packet": str(packet),
                    "packet_sha256": hashlib.sha256(packet.read_bytes()).hexdigest(),
                    "source_archive": str(root / "archive.json"),
                })
            validate.return_value = {"records": records}
            archive_assets.return_value = ({
                "pr_url": "https://github.com/org/repo/pull/1",
                "pr_title": "visual fix", "created_at": "2025-01-01",
                "merged_at": "2025-01-02", "base_ref": "main", "merge_sha": "abc",
                "path": str(root / "archive.json"), "sha256": "f" * 64,
                "status": "complete",
            }, [{"asset_id": asset_id, "path": str(asset_path),
                 "sha256": asset_id, "media_type": "image/png", "source_ids": []}])
            config = root / "config.json"
            config.write_text(json.dumps({
                "schema_version": "capability-candidate-pool-config-v2",
                "records": [{"case_id": case_id, "capability_run": str(run)}
                            for case_id, _ in cases],
            }))
            output = root / "output"
            result = build(config, output, required_per_category=2)
            self.assertTrue(result["quota_met"])
            self.assertEqual([row["count"] for row in result["distribution"]], [2] * 4)
            self.assertEqual(result["unique_pr_count"], 4)
            self.assertEqual(result["multi_label_pr_count"], 4)
            rendered = (output / "16_11_06_candidate_pool.html").read_text()
            self.assertIn("full issue text", rendered)
            self.assertIn("16_11_05_assets", rendered)
            self.assertIn('aria-label="按视觉能力筛选"', rendered)
            self.assertIn('data-category="interaction_temporal_understanding"', rendered)
            self.assertIn('data-categories="rendering_appearance_understanding spatial_layout_understanding"', rendered)
            self.assertIn("card.hidden=!show", rendered)
            self.assertTrue((output / "16_11_05_assets" / f"{asset_id}.png").is_file())

            view = root / "view"
            view_manifest = render_snapshot(output, view)
            self.assertFalse(view_manifest["model_invoked"])
            self.assertEqual(view_manifest["asset_count"], 4)
            self.assertEqual(
                (view / "16_11_05_candidate_pool.json").read_bytes(),
                (output / "16_11_05_candidate_pool.json").read_bytes(),
            )
            self.assertIn("按视觉能力筛选", (view / "16_11_06_candidate_pool.html").read_text())

            translation = root / "translations.json"
            source_row = result["records"][0]
            translation.write_text(json.dumps({
                "schema_version": "human-review-zh-translations-v1",
                "notice": "curator display only",
                "items": [{
                    "case_id": source_row["case_id"],
                    "source_text_sha256": _translation_source_sha(source_row),
                    "pr_title_zh": "视觉修复",
                    "problem_statement_zh": "完整题面中文翻译",
                }],
            }))
            translated_view = root / "translated-view"
            translated_manifest = render_snapshot(
                output, translated_view, [translation])
            self.assertEqual(translated_manifest["translation_count"], 1)
            translated_html = (
                translated_view / "16_11_06_candidate_pool.html").read_text()
            self.assertIn("完整题面中文翻译", translated_html)
            self.assertIn(_translation_source_sha(source_row), translated_html)

    def test_video_uses_a_generated_preview_frame_and_keeps_original_link(self):
        rendered = _render_page({
            "distribution": [],
            "records": [{
                "case_id": "org__repo-1",
                "capability_categories": [],
                "warnings": [],
                "visual_capabilities": [],
                "classification_version": "visual-capability-classifier-v4",
                "migrated_from_v3": False,
                "classification": "annotation.json",
                "classification_sha256": "a" * 64,
                "packet": "packet.json",
                "packet_sha256": "b" * 64,
                "archive": {"pr_url": "https://github.com/org/repo/pull/1", "pr_title": "fix"},
                "rationale": "visual evidence",
                "problem_statement": "issue",
                "assets": [{
                    "asset_id": "c" * 64,
                    "display_path": "/view/16_11_05_assets/video.mp4",
                    "media_type": "video/mp4",
                }],
            }],
        }, Path("/view"))
        self.assertIn('preload="auto"', rendered)
        self.assertIn("data-preview-frame", rendered)
        self.assertIn("video.poster=canvas.toDataURL", rendered)
        self.assertIn("打开原视频", rendered)

    def test_rejects_duplicate_pr_even_for_multi_label_pool(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            config.write_text(json.dumps({
                "schema_version": "capability-candidate-pool-config-v2",
                "records": [{"case_id": "same"}, {"case_id": "same"}],
            }))
            with self.assertRaisesRegex(ValueError, "appear only once"):
                build(config, root / "output")


if __name__ == "__main__":
    unittest.main()
