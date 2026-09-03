import json
import tempfile
import unittest
from pathlib import Path

from report_pipeline.calibration_ui import audit, render


class CalibrationUiTests(unittest.TestCase):
    def test_render_and_audit_bound_two_gate_interface(self):
        fixtures = Path(__file__).resolve().parent / "fixtures"
        base = fixtures / "carbon_20978_archive"
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "18_40_review.html"
            result = render(
                base / "18_01_candidate_dossier.json",
                base / "18_02_test_manifest.json",
                base / "18_07_browser_f2p_p2p_measurement.json",
                fixtures / "tasks/carbon-design-system__carbon-20978",
                base / "18_40_test_review_context.json",
                output,
                base / "18_45_07_verifier_result.json",
                base / "18_40_review_queue.json",
            )
            text = output.read_text()
            self.assertIn("carbon-design-system/carbon · PR #20978", text)
            self.assertIn("第一项人工核验：视觉输入是否必要", text)
            self.assertIn("PR #20978</a> 是本题采用的已合并修复 PR", text)
            self.assertIn("Issue #20120</a> · 标题", text)
            self.assertIn("Issue #20849</a> · 正文", text)
            self.assertIn("不是另外两个 PR", text)
            self.assertIn("第二项人工核验：F2P/P2P 测试是否有效", text)
            self.assertIn("OCR", text)
            self.assertIn("AI 标签输入框应显示蓝色渐变装饰", text)
            self.assertIn("为何属于 F2P/P2P", text)
            self.assertIn("查看技术断言", text)
            self.assertIn("f2p_ai_gradient_decorator", text)
            self.assertIn("p2p_selected_item_state", text)
            self.assertIn("双重核验队列：第 1/1 题", text)
            self.assertIn("目前只有 1 题材料完整", text)
            self.assertIn("目前没有下一题完成 dossier、F2P/P2P 和前后测量", text)
            self.assertIn("完整 Verifier 输出（未截断）", text)
            self.assertIn("visual-verifier-v1", text)
            self.assertIn("text-only-repair-sufficiency-v1", text)
            self.assertIn("source-scope-verifier-v1", text)
            self.assertIn("Agent 收到的完整题面（未截断）", text)
            self.assertIn('id="image-panel" class="hidden"', text)
            self.assertIn('id="post-reveal" class="hidden"', text)
            self.assertIn('id="visual-model-summary" class="muted hidden"', text)
            self.assertIn('id="reveal-images"', text)
            self.assertIn('id="visual-reviewer"', text)
            self.assertIn('id="test-reviewer"', text)
            self.assertIn("text_first_recorded_at", text)
            self.assertIn("images_revealed_at", text)
            self.assertIn("必须填写核验人", text)
            self.assertIn("先保存无图判断，再揭示原图", text)
            self.assertNotIn("max-height:330px", text)
            self.assertEqual(result["asset_count"], 4)
            self.assertEqual(result["test_count"], 8)
            self.assertEqual(result["queue_position"], 1)
            self.assertEqual(result["queue_size"], 1)
            self.assertTrue(Path(result["seed"]).is_file())
            self.assertTrue(Path(result["manifest"]).is_file())
            record = audit(output, Path(temporary) / "audit.json")
            self.assertEqual(record["status"], "passed")
            self.assertEqual(record["asset_count"], 4)
            self.assertEqual(record["test_count"], 8)
            self.assertTrue(record["offline"])
            self.assertEqual(record["event_attributes"], 0)

    def test_rejects_changed_measurement_inventory(self):
        fixtures = Path(__file__).resolve().parent / "fixtures"
        base = fixtures / "carbon_20978_archive"
        with tempfile.TemporaryDirectory() as temporary:
            measurement = json.loads((base / "18_07_browser_f2p_p2p_measurement.json").read_text())
            measurement["transitions"] = measurement["transitions"][1:]
            changed = Path(temporary) / "changed.json"
            changed.write_text(json.dumps(measurement))
            with self.assertRaisesRegex(ValueError, "frozen test inventory"):
                render(
                    base / "18_01_candidate_dossier.json",
                    base / "18_02_test_manifest.json",
                    changed,
                    fixtures / "tasks/carbon-design-system__carbon-20978",
                    base / "18_40_test_review_context.json",
                    Path(temporary) / "bad.html",
                )


if __name__ == "__main__":
    unittest.main()
