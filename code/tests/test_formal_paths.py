import unittest
from pathlib import Path

from report_pipeline.paths import CODE_ROOT, REPORT_ROOT, RUNS_ROOT, TMP_ROOT, WORKSPACE_ROOT
from pr_crawler import repair_sufficiency, visual_verifier


class FormalPathTests(unittest.TestCase):
    def test_authoritative_roots_do_not_point_inside_code_tree(self):
        expected_repository = Path(__file__).resolve().parents[2]
        self.assertEqual(WORKSPACE_ROOT, expected_repository)
        self.assertEqual(REPORT_ROOT, expected_repository)
        self.assertEqual(CODE_ROOT, expected_repository / "code")
        self.assertEqual(TMP_ROOT, expected_repository / ".runtime/tmp")
        self.assertEqual(RUNS_ROOT, expected_repository / ".runtime/runs")
        self.assertTrue(visual_verifier.PROMPT.is_relative_to(CODE_ROOT))
        self.assertTrue(repair_sufficiency.PROMPT.is_relative_to(CODE_ROOT))


if __name__ == "__main__":
    unittest.main()
