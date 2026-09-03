import json
import tempfile
import unittest
from pathlib import Path

from analysis.scripts.step_08_03_probe_linked_issue_media import run
from analysis.scripts.step_11_02_archive_selected_candidate_waves import selection
from report_pipeline.paths import TMP_ROOT


class LinkedIssueMediaProbeTests(unittest.TestCase):
    @staticmethod
    def row(repo, number, title="Routine correction"):
        return {
            "repo": repo, "number": number, "title": title,
            "body": "Fixes #42", "created_at": "2025-02-01T00:00:00Z",
            "merged_at": "2025-02-02T00:00:00Z",
            "base": {"ref": "main", "repo": {"default_branch": "main"}},
        }

    def test_no_visual_keywords_are_required_and_repo_quota_is_hard(self):
        rows = [self.row("a/r", index) for index in range(1, 5)]
        rows += [self.row("b/r", index) for index in range(1, 3)]
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            source.write_text("".join(json.dumps(row) + "\n" for row in rows))
            result = run(source, [], root / "out", 10, 2, "fixture")
            self.assertEqual(4, result["selected_count"])
            self.assertEqual({"a/r": 2, "b/r": 2}, result["repository_counts"])
            self.assertFalse(result["boundary"]["pr_visual_keywords_required"])
            _, identities, _ = selection(root / "out")
            self.assertEqual(4, len(identities))

    def test_rows_without_linked_issue_are_not_probed(self):
        row = self.row("a/r", 1)
        row["body"] = "No linked issue"
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            source.write_text(json.dumps(row) + "\n")
            result = run(source, [], root / "out", 10, 2, "fixture")
            self.assertEqual(0, result["selected_count"])
            ledger = json.loads((root / "out/08_03_03_linked_issue_probe_audit.jsonl").read_text())
            self.assertEqual(["no_statically_confirmed_issue"], ledger["reasons"])

    def test_zero_limit_is_exhaustive_and_does_not_apply_repository_quota(self):
        rows = [self.row("a/r", index) for index in range(1, 5)]
        rows += [self.row("b/r", index) for index in range(1, 3)]
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            source.write_text("".join(json.dumps(row) + "\n" for row in rows))
            result = run(source, [], root / "out", 0, 1, "fixture")
            self.assertEqual(6, result["selected_count"])
            self.assertTrue(result["selection_exhaustive"])
            self.assertIsNone(result["per_repository_quota"])
            self.assertEqual({"a/r": 4, "b/r": 2}, result["repository_counts"])


if __name__ == "__main__":
    unittest.main()
