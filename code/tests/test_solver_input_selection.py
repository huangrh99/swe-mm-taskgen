import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from report_pipeline.solver_input_selection import run


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class SolverInputSelectionTests(unittest.TestCase):
    def test_separates_issue_proposal_from_human_and_failure_queues(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archives = []
            for index, status in enumerate(("complete", "complete", "partial"), 1):
                path = root / f"archive-{index}.json"
                path.write_text(json.dumps({"repo": "o/r", "number": index,
                                            "status": status}))
                archives.append(path)
            records = [
                self._record("o__r-1", archives[0], "issue_derived"),
                self._record("o__r-2", archives[1], "pr_derived"),
                self._record("o__r-3", archives[2], "issue_derived"),
                {"case_id": "o__r-4", "status": "failed",
                 "source_archive": str(archives[0]),
                 "source_archive_sha256": sha(archives[0])},
            ]
            source = root / "role-run"
            source.mkdir()
            (source / "08_04_03_results.json").write_text("{}")
            with patch("report_pipeline.solver_input_selection.validate_run",
                       return_value={"status": "complete", "records": records}):
                manifest = run([source], root / "out")
            self.assertEqual(1, manifest["selected_count"])
            self.assertEqual(3, manifest["human_followup_count"])
            selected = json.loads((root / "out/08_05_01_issue_derived_selected.jsonl").read_text())
            self.assertEqual("o/r#1", selected["pr_id"])
            followup = [json.loads(line) for line in
                        (root / "out/08_05_02_human_followup.jsonl").read_text().splitlines()]
            self.assertEqual({"human_authored_pr_derived_statement_required",
                              "source_archive_not_complete",
                              "image_role_semantic_validation_failed"},
                             {row["reason"] for row in followup})

    def test_rejects_duplicate_pr_across_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "archive.json"
            archive.write_text(json.dumps({"repo": "o/r", "number": 1,
                                           "status": "complete"}))
            record = self._record("o__r-1", archive, "issue_derived")
            runs = [root / "a", root / "b"]
            for source in runs:
                source.mkdir()
                (source / "08_04_03_results.json").write_text("{}")
            with patch("report_pipeline.solver_input_selection.validate_run",
                       return_value={"status": "complete", "records": [record]}):
                with self.assertRaisesRegex(ValueError, "duplicate PR"):
                    run(runs, root / "out")

    def test_exact_case_filter_keeps_only_requested_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records = []
            for number in (1, 2):
                archive = root / f"archive-{number}.json"
                archive.write_text(json.dumps({"repo": "o/r", "number": number,
                                               "status": "complete"}))
                records.append(self._record(f"o__r-{number}", archive, "issue_derived"))
            source = root / "role-run"
            source.mkdir()
            (source / "08_04_03_results.json").write_text("{}")
            with patch("report_pipeline.solver_input_selection.validate_run",
                       return_value={"status": "complete", "records": records}):
                manifest = run([source], root / "out", case_ids=["o__r-2"])
            self.assertEqual(1, manifest["source_record_count"])
            self.assertEqual(["o__r-2"], manifest["requested_case_ids"])
            selected = json.loads(
                (root / "out/08_05_01_issue_derived_selected.jsonl").read_text())
            self.assertEqual("o/r#2", selected["pr_id"])

    def test_retry_run_can_only_replace_matching_failed_base_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "archive.json"
            archive.write_text(json.dumps({"repo": "o/r", "number": 1,
                                           "status": "complete"}))
            failed = {"case_id": "o__r-1", "status": "failed",
                      "source_archive": str(archive),
                      "source_archive_sha256": sha(archive)}
            recovered = self._record("o__r-1", archive, "issue_derived")
            base, retry = root / "base", root / "retry"
            for source in (base, retry):
                source.mkdir()
                (source / "08_04_03_results.json").write_text("{}")
            values = {base.resolve(): {"status": "complete", "records": [failed]},
                      retry.resolve(): {"status": "complete", "records": [recovered]}}
            with patch("report_pipeline.solver_input_selection.validate_run",
                       side_effect=lambda path: values[path.resolve()]):
                manifest = run([base], root / "out", [retry])
            self.assertEqual(1, manifest["selected_count"])
            self.assertEqual(1, manifest["retry_run_count"])

    def test_allows_only_irrelevant_asset_gaps_for_issue_derived_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "archive.json"
            archive.write_text(json.dumps({
                "repo": "o/r", "number": 1, "status": "partial",
                "sections": {
                    "pull_request": {"status": "complete"},
                    "linked_issues": {"status": "complete"},
                    "closing_issues": {"status": "complete"},
                    "consistency": {"status": "complete"},
                    "assets": {"status": "partial"},
                },
            }))
            record = self._record("o__r-1", archive, "issue_derived")
            packet = root / "packet.json"
            packet.write_text(json.dumps({"assets": [{
                "asset_id": "a" * 64,
                "origin_kinds": ["issue"],
                "download_statuses": ["complete"],
                "attachment_index": 1,
            }]}))
            record["packet"] = str(packet)
            record["packet_sha256"] = sha(packet)
            source = root / "role-run"
            source.mkdir()
            (source / "08_04_03_results.json").write_text("{}")
            with patch("report_pipeline.solver_input_selection.validate_run",
                       return_value={"status": "complete", "records": [record]}):
                manifest = run([source], root / "out")
            self.assertEqual(1, manifest["selected_count"])
            selected = json.loads(
                (root / "out/08_05_01_issue_derived_selected.jsonl").read_text())
            self.assertEqual(["unselected_asset_download_gaps_ignored"],
                             selected["archive_warnings"])

    def test_partial_linked_issue_or_selected_unknown_asset_is_not_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, (linked_status, origins) in enumerate(
                    (("partial", ["issue"]), ("complete", ["unknown"])), 1):
                archive = root / f"archive-{index}.json"
                archive.write_text(json.dumps({
                    "repo": "o/r", "number": index, "status": "partial",
                    "sections": {
                        "linked_issues": {"status": linked_status},
                        "assets": {"status": "partial"},
                    },
                }))
                record = self._record(f"o__r-{index}", archive, "issue_derived")
                packet = root / f"packet-{index}.json"
                packet.write_text(json.dumps({"assets": [{
                    "asset_id": "a" * 64, "origin_kinds": origins,
                    "download_statuses": ["complete"], "attachment_index": 1,
                }]}))
                record["packet"] = str(packet)
                record["packet_sha256"] = sha(packet)
                source = root / f"role-run-{index}"
                source.mkdir()
                (source / "08_04_03_results.json").write_text("{}")
                with patch("report_pipeline.solver_input_selection.validate_run",
                           return_value={"status": "complete", "records": [record]}):
                    manifest = run([source], root / f"out-{index}")
                self.assertEqual(0, manifest["selected_count"])
                self.assertEqual({"source_archive_not_complete": 1},
                                 manifest["followup_reason_counts"])

    def test_both_route_reduces_to_issue_only_without_exposing_pr_asset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "archive.json"
            archive.write_text(json.dumps({"repo": "o/r", "number": 1,
                                           "status": "complete"}))
            issue_id, pr_id = "i" * 64, "p" * 64
            packet = root / "packet.json"
            packet.write_text(json.dumps({"assets": [
                {"asset_id": issue_id, "origin_kinds": ["issue"],
                 "occurrences": [{"source_id": "o/r#9:body"}]},
                {"asset_id": pr_id, "origin_kinds": ["pr"],
                 "occurrences": [{"source_id": "pr:body"}]},
            ]}))
            record = self._record("o__r-1", archive, "both")
            record["packet"] = str(packet)
            record["packet_sha256"] = sha(packet)
            record["annotation"]["before_candidate_asset_ids"] = [pr_id, issue_id]
            source = root / "role-run"
            source.mkdir()
            (source / "08_04_03_results.json").write_text("{}")
            with patch("report_pipeline.solver_input_selection.validate_run",
                       return_value={"status": "complete", "records": [record]}):
                manifest = run([source], root / "out")
            self.assertEqual(1, manifest["selected_count"])
            selected = json.loads(
                (root / "out/08_05_01_issue_derived_selected.jsonl").read_text())
            self.assertEqual("issue_derived", selected["source_route"])
            self.assertEqual("both", selected["original_source_route"])
            self.assertEqual([issue_id], selected["before_candidate_asset_ids"])
            self.assertEqual([pr_id], selected["excluded_pr_candidate_asset_ids"])
            self.assertEqual("use_issue_text", selected["problem_statement_action"])

    def test_both_route_with_issue_crop_ambiguity_stays_in_followup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "archive.json"
            archive.write_text(json.dumps({"repo": "o/r", "number": 1,
                                           "status": "complete"}))
            issue_id = "i" * 64
            packet = root / "packet.json"
            packet.write_text(json.dumps({"assets": [{
                "asset_id": issue_id, "origin_kinds": ["issue"],
                "occurrences": [{"source_id": "o/r#9:body"}],
            }]}))
            record = self._record("o__r-1", archive, "both")
            record["packet"] = str(packet)
            record["packet_sha256"] = sha(packet)
            record["annotation"]["before_candidate_asset_ids"] = [issue_id]
            record["annotation"]["crop_review_asset_ids"] = [issue_id]
            source = root / "role-run"
            source.mkdir()
            (source / "08_04_03_results.json").write_text("{}")
            with patch("report_pipeline.solver_input_selection.validate_run",
                       return_value={"status": "complete", "records": [record]}):
                manifest = run([source], root / "out")
            self.assertEqual(0, manifest["selected_count"])
            self.assertEqual({"human_route_and_asset_allowlist_required": 1},
                             manifest["followup_reason_counts"])

    def test_recalls_high_confidence_historical_issue_expected_design(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "archive.json"
            archive.write_text(json.dumps({"repo": "o/r", "number": 1,
                                           "status": "complete"}))
            expected_id = "e" * 64
            packet = root / "packet.json"
            packet.write_text(json.dumps({"assets": [{
                "asset_id": expected_id, "origin_kinds": ["issue"],
                "occurrences": [{"source_id": "o/r#8:body"}],
            }]}))
            record = self._record("o__r-1", archive, "no_candidate")
            record["packet"] = str(packet)
            record["packet_sha256"] = sha(packet)
            record["annotation"]["before_candidate_asset_ids"] = []
            record["annotation"]["images"] = [{
                "asset_id": expected_id, "observed": True,
                "role": "expected_design", "contains_fixed_after": "no",
                "contains_solution_evidence": "no", "task_relationship": "explicit",
                "confidence": "high", "agent_visibility_recommendation": "human_review",
            }]
            source = root / "role-run"
            source.mkdir()
            (source / "08_04_03_results.json").write_text("{}")
            with patch("report_pipeline.solver_input_selection.validate_run",
                       return_value={"status": "complete", "records": [record]}):
                manifest = run([source], root / "out")
            self.assertEqual(1, manifest["selected_count"])
            selected = json.loads(
                (root / "out/08_05_01_issue_derived_selected.jsonl").read_text())
            self.assertEqual([expected_id], selected["before_candidate_asset_ids"])
            self.assertEqual("historical_issue_expected_design", selected["candidate_basis"])
            self.assertEqual("pending", selected["expected_design_human_confirmation"])

    def test_does_not_recall_expected_design_with_solution_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "archive.json"
            archive.write_text(json.dumps({"repo": "o/r", "number": 1,
                                           "status": "complete"}))
            expected_id = "e" * 64
            packet = root / "packet.json"
            packet.write_text(json.dumps({"assets": [{
                "asset_id": expected_id, "origin_kinds": ["issue"],
                "occurrences": [{"source_id": "o/r#8:body"}],
            }]}))
            record = self._record("o__r-1", archive, "no_candidate")
            record["packet"] = str(packet)
            record["packet_sha256"] = sha(packet)
            record["annotation"]["before_candidate_asset_ids"] = []
            record["annotation"]["images"] = [{
                "asset_id": expected_id, "observed": True,
                "role": "expected_design", "contains_fixed_after": "no",
                "contains_solution_evidence": "yes", "task_relationship": "explicit",
                "confidence": "high", "agent_visibility_recommendation": "human_review",
            }]
            source = root / "role-run"
            source.mkdir()
            (source / "08_04_03_results.json").write_text("{}")
            with patch("report_pipeline.solver_input_selection.validate_run",
                       return_value={"status": "complete", "records": [record]}):
                manifest = run([source], root / "out")
            self.assertEqual(0, manifest["selected_count"])

    @staticmethod
    def _record(case_id, archive, route):
        return {
            "case_id": case_id,
            "status": "complete",
            "source_archive": str(archive),
            "source_archive_sha256": sha(archive),
            "annotation": {
                "source_path_recommendation": route,
                "before_candidate_asset_ids": ["a" * 64],
                "crop_review_asset_ids": [],
                "video_review_asset_ids": [],
                "retry_asset_ids": [],
                "problem_statement_action": ("use_issue_text" if route == "issue_derived"
                                             else "draft_pr_derived"),
            },
        }


if __name__ == "__main__":
    unittest.main()
