import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from report_pipeline.source_tests import compare, run


class SourceTestsTest(unittest.TestCase):
    def test_real_carbon_frozen_transitions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@invalid"], check=True)
            target = repo / "feature.txt"
            target.write_text("stable\n")
            subprocess.run(["git", "-C", str(repo), "add", "feature.txt"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
            baseline_commit = subprocess.check_output(
                ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
            target.write_text("stable\nfixed\n")
            subprocess.run(["git", "-C", str(repo), "commit", "-qam", "fix"], check=True)
            reference_commit = subprocess.check_output(
                ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"tests": [
                {"test_id": "f", "class": "F2P", "path": "feature.txt",
                 "contains_all": ["fixed"], "contains_none": [],
                 "expected_transition": "fail->pass"},
                {"test_id": "p", "class": "P2P", "path": "feature.txt",
                 "contains_all": ["stable"], "contains_none": [],
                 "expected_transition": "pass->pass"},
            ]}))
            measured = compare(
                manifest, run(manifest, repo, baseline_commit),
                run(manifest, repo, reference_commit),
            )
            self.assertTrue(measured["all_transitions_match"])
            self.assertEqual(measured["semantic_calibration"], "pending_human_review")
            self.assertFalse(measured["pixel_oracle_present"])


if __name__ == "__main__":
    unittest.main()
