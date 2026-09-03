import json
import contextlib
import io
from pathlib import Path
import tempfile
import unittest

from report_pipeline import cli
from report_pipeline.v4_test_campaign_audit import render


class V4TestCampaignAuditTests(unittest.TestCase):
    def _campaign(self, root: Path) -> tuple[Path, str]:
        case_id = "owner__repo-7"
        case = root / "20_17_02_model_runs" / case_id
        case.mkdir(parents=True)
        (case / "20_17_01_packet.json").write_text(json.dumps({
            "task_id": case_id, "repository": "owner/repo",
            "v4": {"labels": ["spatial"]},
        }))
        (case / "20_17_07_status.json").write_text(json.dumps({
            "case_id": case_id, "status": "complete", "model": "fixture",
        }))
        (case / "20_17_06_final.json").write_text(json.dumps({
            "status": "test_bundle_proposed", "summary": "<safe>",
            "repository_observations": {"nearby_test_paths": ["old.test.js"],
                                        "author_test_paths": ["author.test.js"]},
            "behavioral_contract": [{"requirement_id": "r1",
                "observable_behavior": "visible", "preserved_behavior": "stable",
                "oracle": "DOM"}],
            "test_bundle": {"working_directory": ".", "test_command": "npm test",
                "stable_test_ids": ["stable one"], "files": [
                    {"path": "new.test.js", "operation": "add", "content": ""}],
                "collection_evidence": "collected", "functional_oracle_evidence": "purpose"},
        }))
        summary = root / "20_17_08_summary.json"
        summary.write_text(json.dumps({
            "schema_version": "v4-test-construction-campaign-v1",
            "records": [{"case_id": case_id, "status": "complete"}],
        }))
        return summary, case_id

    def test_renders_summary_with_contract_bundle_paths_and_placeholder(self):
        with tempfile.TemporaryDirectory(dir="tmp") as value:
            summary, case_id = self._campaign(Path(value))
            result = render(summary, Path(value) / "audit")
            page = Path(result["output"]).read_text()
            for expected in (case_id, "spatial", "old.test.js", "author.test.js",
                             "new.test.js", "npm test", "stable one", "Not measured"):
                self.assertIn(expected, page)
            self.assertIn("&lt;safe&gt;", page)

    def test_renders_running_directory_and_optional_measurement(self):
        with tempfile.TemporaryDirectory(dir="tmp") as value:
            root = Path(value)
            _, case_id = self._campaign(root)
            measurement = root / "measurement.json"
            measurement.write_text(json.dumps({
                "schema_version": "v4-provisional-base-gold-measurement-v1",
                "records": [{"case_id": case_id, "status": "measured_provisional",
                    "transitions": [{"test_id": "stable one", "base_status": "fail",
                        "gold_status": "pass", "classification": "provisional_f2p"}]}],
            }))
            result = render(root / "20_17_02_model_runs", root / "audit.html", measurement)
            page = Path(result["output"]).read_text()
            self.assertEqual(1, result["measurement_count"])
            self.assertIn("provisional_f2p", page)

    def test_public_cli_renders_campaign(self):
        with tempfile.TemporaryDirectory(dir="tmp") as value:
            root = Path(value)
            summary, case_id = self._campaign(root)
            output = root / "cli-audit"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                status = cli.main(["render-v4-test-campaign", "--input", str(summary),
                                   "--output", str(output)])
            self.assertEqual(0, status)
            self.assertIn(case_id, (output / "20_17_09_audit.html").read_text())
            self.assertIn('"case_count": 1', stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
