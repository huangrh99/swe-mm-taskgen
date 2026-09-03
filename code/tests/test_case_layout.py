import json
import tempfile
import unittest
from pathlib import Path

from report_pipeline.case_layout import migrate_case


class CaseLayoutTests(unittest.TestCase):
    def test_promotes_runtime_and_keeps_meta_outputs_inside_case_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            case = Path(temporary) / "owner__repo-1"
            evidence = Path(temporary) / "evidence/owner__repo-1"
            task = case / "05_harbor/task"
            for directory in ("environment", "solution", "tests"):
                (task / directory).mkdir(parents=True, exist_ok=True)
            for relative in (
                "environment/Dockerfile", "environment/base_image.json", "instruction.md",
                "solution/solve.sh", "solution/gold.patch", "task.toml", "tests/config.json",
                "tests/sweb_grade.py", "tests/test.patch", "tests/test.sh",
            ):
                path = task / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(relative)
            (case / "01_visual_review").mkdir()
            (case / "00_case_manifest.json").write_text(json.dumps({
                "schema_version": "report-case-archive-v1", "case_id": case.name,
                "sections": {},
            }))
            result = migrate_case(case, evidence)
            self.assertTrue(result["submit_ready_layout"])
            self.assertTrue((case / "instruction.md").is_file())
            self.assertTrue((case / "meta/01_visual_review").is_dir())
            self.assertEqual(
                {path.name for path in case.iterdir()},
                {
                    "environment", "instruction.md", "solution", "task.toml", "tests",
                    "meta", "outputs",
                },
            )


if __name__ == "__main__":
    unittest.main()
