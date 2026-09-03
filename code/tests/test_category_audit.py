import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from report_pipeline.category_audit import (
    COUNTED_CATEGORIES, CATEGORIES, _qualification_from_source, run, summarize,
)
from report_pipeline.paths import TMP_ROOT


class CategoryAuditTest(unittest.TestCase):
    @staticmethod
    def _row(case_id, categories, status="complete"):
        if isinstance(categories, str):
            categories = [categories]
        return {"case_id": case_id, "visual_capability": {"status": status, "annotation": {
            "schema_version": "visual-capability-classifier-v4",
            "task_id": case_id,
            "visual_capabilities": [{
                "category": category,
                "importance": "core" if index == 0 else "supporting",
                "visual_evidence": f"evidence {category}",
                "task_relation": f"relation {category}",
            } for index, category in enumerate(categories)],
        }}}

    @staticmethod
    def _qualified(rows):
        return {row["case_id"]: {"qualified": True, "reasons": []} for row in rows}

    def test_four_capability_pools_each_require_five(self):
        rows = [self._row(f"case-{category}-{index}", category)
                for category in CATEGORIES for index in range(5)]
        result = summarize(rows, {}, self._qualified(rows))
        self.assertTrue(result["gate_passed"])
        self.assertEqual(result["qualified_count"], 20)
        self.assertEqual(result["capability_membership_count"], 20)
        self.assertEqual(result["multi_label_count"], 0)
        self.assertEqual([item["category"] for item in result["distribution"]],
                         list(COUNTED_CATEGORIES))
        self.assertEqual({item["count"] for item in result["distribution"]}, {5})

    def test_multi_label_pr_counts_once_in_each_capability_pool(self):
        rows = [self._row(f"case-{category}-{index}", category)
                for category in CATEGORIES for index in range(5)]
        rows.append(self._row("multi", [CATEGORIES[0], CATEGORIES[1]]))
        result = summarize(rows, {}, self._qualified(rows))
        self.assertTrue(result["gate_passed"])
        self.assertEqual(result["qualified_count"], 21)
        self.assertEqual(result["capability_membership_count"], 22)
        self.assertEqual(result["multi_label_count"], 1)
        self.assertEqual([item["count"] for item in result["distribution"]], [6, 6, 5, 5])

    def test_duplicate_capability_is_rejected(self):
        rows = [self._row("bad", [CATEGORIES[0], CATEGORIES[0]])]
        result = summarize(rows, {}, self._qualified(rows))
        self.assertEqual(result["qualified_count"], 0)
        self.assertIn("missing_duplicated_or_invalid_visual_capability",
                      result["rows"][0]["exclusion_reasons"])

    def test_failed_classification_and_external_source_exclusions_do_not_count(self):
        rows = [self._row("failed", CATEGORIES[0], status="failed"),
                self._row("source", CATEGORIES[0])]
        result = summarize(rows, {"source": ["source_scope_unresolved"]},
                           self._qualified(rows))
        self.assertEqual(result["qualified_count"], 0)
        self.assertEqual(result["distribution"][0]["deficit"], 5)
        self.assertEqual(result["rows"][1]["exclusion_reasons"], ["source_scope_unresolved"])

    def test_missing_or_failed_qualification_never_counts(self):
        rows = [self._row("missing", CATEGORIES[0]),
                self._row("partial", CATEGORIES[0])]
        result = summarize(rows, {}, {
            "partial": {"qualified": False,
                        "reasons": ["source_sections_incomplete:comments"]}})
        self.assertEqual(0, result["qualified_count"])
        self.assertEqual(["qualification_evidence_missing"],
                         result["rows"][0]["exclusion_reasons"])
        self.assertEqual(["source_sections_incomplete:comments"],
                         result["rows"][1]["exclusion_reasons"])

    def test_upstream_model_failure_does_not_become_source_disqualification(self):
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            required_sections = {
                name: {"status": "complete"} for name in {
                    "pull_request", "comments", "reviews", "review_comments",
                    "commits", "files", "diff", "patch", "closing_issues",
                    "linked_issues", "assets", "timeline", "merge_commit",
                    "merge_anchor_evidence",
                }
            }
            media_root = root / "11_http_archive/assets"
            media_root.mkdir(parents=True)
            media = media_root / "image.png"
            media.write_bytes(b"visual")
            media_sha = hashlib.sha256(media.read_bytes()).hexdigest()
            required_sections["assets"] = {"status": "partial", "items": [
                {"status": "complete", "sha256": media_sha,
                 "local_path": "assets/image.png"},
                {"status": "unavailable", "reason": "unrelated comment QR HTTP 404"},
            ]}
            required_sections["consistency"] = {
                "status": "partial", "atomic_snapshot": False,
                "reason": "bounded non-transactional GitHub observation",
            }
            issue_title = "Visual defect"
            required_sections["linked_issues"] = {"status": "partial", "items": [
                {"repo": "owner/repo", "number": 2,
                 "detail": {"status": "complete", "data": {"title": issue_title}},
                 "comments": {"status": "complete"}, "labels": {"status": "complete"},
                 "timeline": {"status": "complete"}},
                {"repo": "owner/repo", "number": 999,
                 "detail": {"status": "unavailable"}},
            ]}
            archive = root / "archive.json"
            archive.write_text(json.dumps({"status": "partial", "sections": required_sections}))
            archive_sha = hashlib.sha256(archive.read_bytes()).hexdigest()
            source_packet = root / "source_packet.json"
            source_packet.write_text(json.dumps({
                "provenance": {"source_archive": str(archive),
                               "source_archive_sha256": archive_sha},
                "problem_sources": [{
                    "source_id": "owner/repo#2:title", "kind": "issue", "field": "title",
                    "original_text_sha256": hashlib.sha256(issue_title.encode()).hexdigest(),
                    "text": issue_title,
                    "text_sha256": hashlib.sha256(issue_title.encode()).hexdigest(),
                }],
                "withheld": ["pull_request_prose", "comments", "reviews", "commits",
                             "diff", "patch", "tests", "reference_code"],
            }))
            source_packet_sha = hashlib.sha256(source_packet.read_bytes()).hexdigest()
            classification_packet = root / "classification_packet.json"
            classification_packet.write_text(json.dumps({
                "task_id": "owner__repo-1", "problem_statement": "visual defect",
                "assets": [{"asset_id": media_sha, "attachment_index": 1,
                            "source_ids": ["issue:owner/repo#2:body"]}],
            }))
            classification_packet_sha = hashlib.sha256(
                classification_packet.read_bytes()).hexdigest()
            result_path = root / "result.json"
            result_path.write_text(json.dumps({
                "case_id": "owner__repo-1", "status": "failed",
                "error": "ValidationError: provider output had an extra field",
                "packet": str(source_packet), "packet_sha256": source_packet_sha,
            }))
            qualification = _qualification_from_source({
                "case_id": "owner__repo-1",
                "source_result_sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(),
                "source_archive_sha256": archive_sha,
                "packet": str(classification_packet),
                "packet_sha256": classification_packet_sha,
            }, result_path)
            self.assertTrue(qualification["qualified"])
            self.assertEqual([], qualification["reasons"])
            self.assertEqual("failed", qualification["upstream_verifier_status"])
            self.assertIn("extra field", qualification["upstream_verifier_error"])
            self.assertEqual("partial", qualification["source_archive_status"])
            self.assertEqual("partial", qualification["consistency_status"])
            self.assertEqual(1, qualification["solver_visible_asset_count"])
            self.assertEqual(1, qualification["solver_visible_problem_source_count"])
            self.assertEqual(1, qualification["unbound_source_failure_count"])
            self.assertEqual(1, qualification["unbound_asset_failure_count"])

    @patch("report_pipeline.category_audit.validate_classification_run")
    def test_run_binds_input_and_rejects_unknown_exclusion(self, validate):
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            source = root / "source"; source.mkdir()
            manifest = root / "classification.json"
            manifest.write_text(json.dumps({"source_run": str(source),
                                            "records": [self._row("known", CATEGORIES[0])]}) + "\n")
            exclusions = root / "exclusions.json"
            exclusions.write_text(json.dumps({"schema_version": "visual-category-exclusions-v1",
                                              "exclusions": {"unknown": ["bad source"]}}) + "\n")
            with self.assertRaisesRegex(ValueError, "unknown case"):
                run(manifest, root / "out", exclusions)
            validate.assert_called_once_with(source.resolve(), manifest.resolve())

    @patch("report_pipeline.category_audit._qualification_from_source",
           return_value={"qualified": True, "reasons": []})
    @patch("report_pipeline.category_audit.validate_classification_run")
    def test_run_combines_disjoint_classification_runs_once(self, validate, qualify):
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            manifests = []
            for run_index, category in enumerate((CATEGORIES[0], CATEGORIES[3]), 1):
                source = root / f"source-{run_index}"
                source.mkdir()
                (source / "16_03_result_0001.json").write_text("{}\n")
                row = self._row(f"case-{run_index}", category)
                manifest = root / f"classification-{run_index}.json"
                manifest.write_text(json.dumps({"source_run": str(source),
                                                "records": [row]}) + "\n")
                manifests.append(manifest)
            result = run(manifests, root / "out")
            self.assertEqual("visual-capability-distribution-v4", result["schema_version"])
            self.assertEqual(2, len(result["classifications"]))
            self.assertEqual(2, result["qualified_count"])
            self.assertEqual(2, result["capability_membership_count"])
            self.assertEqual(2, validate.call_count)
            self.assertEqual(2, qualify.call_count)
            rendered = (root / "out/16_03_09_03_category_distribution.html").read_text()
            self.assertIn("case-1", rendered)
            self.assertIn("case-2", rendered)
            self.assertIn("全部结果", rendered)

    @patch("report_pipeline.category_audit.validate_classification_run")
    def test_run_rejects_duplicate_case_across_runs(self, validate):
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            manifests = []
            for run_index in (1, 2):
                source = root / f"source-{run_index}"
                source.mkdir()
                manifest = root / f"classification-{run_index}.json"
                manifest.write_text(json.dumps({"source_run": str(source),
                                                "records": [self._row("same", CATEGORIES[0])]}) + "\n")
                manifests.append(manifest)
            with self.assertRaisesRegex(ValueError, "appears more than once"):
                run(manifests, root / "out")


if __name__ == "__main__":
    unittest.main()
