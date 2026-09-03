import unittest

from report_pipeline.v3_v4_conversion import _convert_annotation


def base(constraints):
    return {
        "schema_version": "visual-capability-classifier-v3",
        "strict_multimodal_admission": "非文字视觉信息候选不可替代",
        "human_review_required": False,
        "atomic_visual_constraints": constraints,
    }


class V3V4ConversionTests(unittest.TestCase):
    def test_direct_categories_and_mixed_constraints_become_multi_label(self):
        status, capabilities, reasons = _convert_annotation(base([
            {"constraint_id": "constraint_001",
             "visual_category": "外观与渲染属性理解",
             "decision_critical": "是", "direct_visual_evidence": "blue fill",
             "description": "preserve the blue fill"},
            {"constraint_id": "constraint_002",
             "visual_category": "空间布局与几何理解",
             "decision_critical": "是", "direct_visual_evidence": "aligned edges",
             "description": "align both edges"},
        ]))
        self.assertEqual(status, "converted")
        self.assertEqual(reasons, [])
        self.assertEqual([item["category"] for item in capabilities], [
            "rendering_appearance_understanding", "spatial_layout_understanding"])
        self.assertEqual([item["importance"] for item in capabilities], ["core", "core"])

    def test_domain_semantics_is_never_guessed(self):
        status, capabilities, reasons = _convert_annotation(base([
            {"constraint_id": "constraint_001",
             "visual_category": "图形符号与领域语义理解",
             "decision_critical": "是", "direct_visual_evidence": "domain arrow",
             "description": "preserve arrow meaning"},
        ]))
        self.assertEqual(status, "needs_review_unmapped_domain")
        self.assertEqual(capabilities, [])
        self.assertIn("cannot be mapped", reasons[0])

    def test_non_strict_v3_is_excluded(self):
        value = base([])
        value["strict_multimodal_admission"] = "图片有帮助但文字已足够"
        status, capabilities, _ = _convert_annotation(value)
        self.assertEqual(status, "excluded_not_strict_visual")
        self.assertEqual(capabilities, [])


if __name__ == "__main__":
    unittest.main()
