import json
import tempfile
import unittest
from pathlib import Path

from analysis.scripts.step_08_03_select_balanced_visual_recall import BUCKETS, run
from analysis.scripts.step_11_02_archive_selected_candidate_waves import selection
from report_pipeline.paths import TMP_ROOT


class BalancedRecallSelectionTests(unittest.TestCase):
    @staticmethod
    def _row(number, title):
        return {
            "repo": "owner/repo", "number": number, "title": title,
            "body": f"Fixes #9\nBefore screenshot and expected result for {title}",
            "created_at": "2025-02-01T00:00:00Z", "merged_at": "2025-02-02T00:00:00Z",
            "base": {"ref": "main", "repo": {"default_branch": "main"}},
            "image_screening": {"assets": [{"media_kind": "image",
                                               "decoration_reason": None}]},
        }

    def test_selects_six_distinct_recall_buckets_without_claiming_v3_labels(self):
        titles = [
            "Visual color refresh", "Fix responsive layout overflow",
            "Correct modal tooltip state", "Repair drag animation sequence",
            "Correct map marker chart", "Figma redesign of animated chart layout",
        ]
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            source.write_text("".join(json.dumps(self._row(index, title)) + "\n"
                                      for index, title in enumerate(titles, 1)))
            result = run(source, [], root / "out", 1, "fixture-seed")
            self.assertEqual("complete", result["status"])
            self.assertEqual(6, result["selected_count"])
            self.assertEqual({bucket: 1 for bucket in BUCKETS}, result["selected_counts"])
            ledger = [json.loads(line) for line in
                      (root / "out/08_03_selection_ledger.jsonl").read_text().splitlines()]
            self.assertEqual(6, len({item["pr_id"] for item in ledger}))
            self.assertTrue(all(item["recall_only_not_v3_classification"] for item in ledger))

    def test_rejects_non_default_branch_pre2025_and_missing_issue_rows(self):
        good = self._row(1, "Fix responsive layout")
        bad_branch = self._row(2, "Fix responsive layout")
        bad_branch["base"]["ref"] = "release"
        old = self._row(3, "Fix responsive layout")
        old["created_at"] = "2024-12-31T00:00:00Z"
        no_issue = self._row(4, "Fix responsive layout")
        no_issue["body"] = "Screenshot only"
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            source.write_text("".join(json.dumps(item) + "\n"
                                      for item in (good, bad_branch, old, no_issue)))
            result = run(source, [], root / "out", 2, "fixture-seed")
            self.assertEqual(1, result["source_eligible_count"])
            self.assertEqual(1, result["selected_count"])
            self.assertEqual("partial", result["status"])

    def test_issue_probe_candidate_does_not_require_a_pr_body_image(self):
        row = self._row(1, "Fix map marker geometry")
        row["image_screening"] = {"assets": []}
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            source.write_text(json.dumps(row) + "\n")
            result = run(source, [], root / "out", 1, "fixture-seed", 3,
                         (BUCKETS[4],))
            self.assertEqual(1, result["source_eligible_count"])
            ledger = json.loads(
                (root / "out/08_03_selection_ledger.jsonl").read_text())
            self.assertEqual("issue_probe_required", ledger["signals"]["source_route_recall"])

    def test_generic_images_and_templates_do_not_create_all_category_signals(self):
        row = self._row(1, "Update copy")
        row["body"] = "Fixes #9\nClick through the UI. Before and after screenshots."
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            source.write_text(json.dumps(row) + "\n")
            result = run(source, [], root / "out", 1, "fixture-seed")
            self.assertEqual(0, result["selected_count"])
            self.assertEqual({bucket: 0 for bucket in BUCKETS},
                             result["candidate_counts_before_deduplication"])

    def test_maintenance_node_and_sequence_diagram_terms_do_not_fake_deficit_buckets(self):
        maintenance = self._row(1, "Bump actions/setup-node from 6 to 7")
        sequence = self._row(2, "Correct label position in sequence diagram")
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            source.write_text(json.dumps(maintenance) + "\n" + json.dumps(sequence) + "\n")
            targets = (BUCKETS[3], BUCKETS[4])
            result = run(source, [], root / "out", 2, "fixture-seed", 3, targets)
            self.assertEqual(1, result["source_eligible_count"])
            self.assertEqual(0, result["selected_counts"][BUCKETS[3]])
            self.assertEqual(1, result["selected_counts"][BUCKETS[4]])

    def test_stage11_orchestrator_accepts_hash_bound_balanced_manifest(self):
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            source.write_text(json.dumps(self._row(1, "Fix responsive layout")) + "\n")
            output = root / "out"
            run(source, [], output, 1, "fixture-seed")
            selected, ids, provenance = selection(output)
            self.assertEqual(["owner/repo#1"], ids)
            self.assertEqual(output / "08_03_selected_balanced_recall_prs.jsonl", selected)
            self.assertEqual(str(output), provenance["path"])

    def test_repository_cap_forces_cross_repo_recall_or_an_explicit_deficit(self):
        rows = []
        for index in range(1, 6):
            row = self._row(index, "Fix responsive layout overflow")
            row["repo"] = "dominant/repo"
            rows.append(row)
        alternative = self._row(6, "Fix responsive layout overflow")
        alternative["repo"] = "alternative/repo"
        rows.append(alternative)
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            source.write_text("".join(json.dumps(item) + "\n" for item in rows))
            result = run(source, [], root / "out", 3, "fixture-seed", 2)
            counts = result["selected_repository_counts"][BUCKETS[1]]
            self.assertEqual({"alternative/repo": 1, "dominant/repo": 2}, counts)
            self.assertEqual(3, result["selected_counts"][BUCKETS[1]])

    def test_repository_exclusion_stops_new_recall_without_rewriting_sources(self):
        carbon = self._row(1, "Fix responsive layout overflow")
        carbon["repo"] = "carbon-design-system/carbon"
        alternative = self._row(2, "Fix responsive layout overflow")
        alternative["repo"] = "alternative/repo"
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            source.write_text(json.dumps(carbon) + "\n" + json.dumps(alternative) + "\n")
            source_before = source.read_bytes()
            result = run(source, [], root / "out", 2, "fixture-seed", 3,
                         (BUCKETS[1],), ["Carbon-Design-System/Carbon"])
            ledger = [json.loads(line) for line in
                      (root / "out/08_03_selection_ledger.jsonl").read_text().splitlines()]
            self.assertEqual(["alternative/repo#2"], [item["pr_id"] for item in ledger])
            self.assertEqual(["carbon-design-system/carbon"],
                             result["excluded_repositories"])
            self.assertEqual(1, result["excluded_repository_source_count"])
            self.assertEqual(source_before, source.read_bytes())

    def test_repository_exclusion_rejects_non_owner_repo_values(self):
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            source.write_text(json.dumps(self._row(1, "Fix responsive layout")) + "\n")
            with self.assertRaisesRegex(ValueError, "invalid excluded repository"):
                run(source, [], root / "out", 1, "fixture-seed", 3,
                    (BUCKETS[1],), ["carbon"])

    def test_category_audit_exclusion_is_hash_bound(self):
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            source.write_text(json.dumps(self._row(1, "Fix responsive layout")) + "\n")
            result_path = root / "result.json"
            result_path.write_text(json.dumps({"pr_id": "owner/repo#1"}))
            import hashlib
            result_sha = hashlib.sha256(result_path.read_bytes()).hexdigest()
            audit = root / "audit.json"
            audit.write_text(json.dumps({
                "schema_version": "visual-category-distribution-v3",
                "rows": [{"source_qualification": {
                    "source_result": str(result_path),
                    "source_result_sha256": result_sha,
                }}],
            }))
            result = run(source, [], root / "out", 1, "fixture-seed", 3,
                         (BUCKETS[1],), (), [audit])
            self.assertEqual(0, result["selected_count"])
            self.assertEqual(1, result["excluded_category_audit_identity_count"])
            result_path.write_text('{}')
            with self.assertRaisesRegex(ValueError, "source result changed"):
                run(source, [], root / "tampered", 1, "fixture-seed", 3,
                    (BUCKETS[1],), (), [audit])

    def test_can_target_only_the_three_remaining_deficit_buckets(self):
        titles = ["Repair drag animation sequence", "Correct map marker chart",
                  "Figma redesign of animated chart layout"]
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            source.write_text("".join(json.dumps(self._row(index, title)) + "\n"
                                      for index, title in enumerate(titles, 1)))
            targets = (BUCKETS[3], BUCKETS[4], BUCKETS[5])
            result = run(source, [], root / "out", 1, "fixture-seed", 3, targets)
            self.assertEqual(list(targets), result["target_recall_buckets"])
            self.assertEqual(set(targets), set(result["selected_counts"]))
            self.assertEqual(3, result["selected_count"])

    def test_mixed_recall_accepts_before_after_with_multiple_title_capabilities(self):
        row = self._row(1, "Fix animated chart tooltip layout")
        row["body"] = "Fixes #9\nBefore screenshot, then after screenshot."
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            source.write_text(json.dumps(row) + "\n")
            result = run(source, [], root / "out", 1, "fixture-seed", 3,
                         (BUCKETS[-1],))
            self.assertEqual({BUCKETS[-1]: 1}, result["selected_counts"])
            ledger = json.loads(
                (root / "out/08_03_selection_ledger.jsonl").read_text())
            self.assertTrue(ledger["signals"]["compositional_multi_signal"])


if __name__ == "__main__":
    unittest.main()
