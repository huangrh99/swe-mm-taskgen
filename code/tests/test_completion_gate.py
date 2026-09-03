import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from report_pipeline import completion_gate
from report_pipeline.category_audit import COUNTED_CATEGORIES, CATEGORIES, summarize
from report_pipeline.paths import TMP_ROOT


class CompletionGateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=TMP_ROOT)
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def _binding(path):
        return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    def test_self_signed_minimal_evidence_can_never_false_ready(self):
        evidence = self.root / "evidence"; evidence.mkdir()
        values = {
            "category.json": {"schema_version": "visual-category-distribution-v3",
                              "gate_passed": True,
                              "distribution": [{"category": item, "count": 5}
                                               for item in COUNTED_CATEGORIES]},
            "freeze.json": {"schema_version": "pipeline-freeze-manifest-v1",
                            "formal_promotion_ready": {"status": "ready",
                                                       "blocking_limitations": []}},
            "tests.json": {"schema_version": "formal-test-run-v1", "status": "passed",
                           "passed": 999, "failed": 0, "errors": 0},
            "reviews.json": {"schema_version": "independent-review-gate-v1",
                             "reviews": [{"reviewer_id": str(i), "status": "passed"}
                                         for i in range(3)]},
        }
        bindings = {}
        for name, value in values.items():
            path = evidence / name; path.write_text(json.dumps(value) + "\n")
            bindings[name] = self._binding(path)
        tasks = []
        for index in range(5):
            ledger = evidence / f"ledger-{index}.json"
            frozen = evidence / f"frozen-{index}.json"
            ledger.write_text(json.dumps({"status": "completed", "current_state": "frozen",
                                          "mode": "real"}) + "\n")
            frozen.write_text(json.dumps({"instance_id": f"owner__repo-{index + 1}",
                                          "mode": "real"}) + "\n")
            tasks.append({"instance_id": f"owner__repo-{index + 1}",
                          "state_ledger": self._binding(ledger),
                          "frozen_manifest": self._binding(frozen)})
        html = evidence / "audit.html"; html.write_text(" ".join(COUNTED_CATEGORIES) + " Pass@5 F2P P2P 人工")
        packet = evidence / "completion_packet.json"
        packet.write_text(json.dumps({
            "schema_version": "visual-exam-completion-packet-v1", "report_root": "report",
            "category_distribution": bindings["category.json"],
            "pipeline_freeze": bindings["freeze.json"],
            "full_test_run": bindings["tests.json"], "review_gate": bindings["reviews.json"],
            "iid_tasks": tasks, "submission_html": self._binding(html),
        }) + "\n")
        result = completion_gate.validate(packet)
        self.assertEqual("not_complete", result["status"])
        codes = {item["code"] for item in result["errors"]}
        self.assertEqual({"completion_packet_not_in_formal_evidence"}, codes)

    @patch("report_pipeline.completion_gate._qualification_from_source",
           side_effect=lambda *_: {"qualified": True, "reasons": []})
    @patch("report_pipeline.completion_gate.validate_classification_run")
    def test_category_gate_recomputes_multiple_disjoint_runs(self, validate_run, qualify):
        def record(case_id, categories):
            return {
                "case_id": case_id,
                "visual_capability": {"status": "complete", "annotation": {
                    "schema_version": "visual-capability-classifier-v4",
                    "task_id": case_id,
                    "visual_capabilities": [{
                        "category": category,
                        "importance": "core" if category_index == 0 else "supporting",
                        "visual_evidence": f"evidence-{category}",
                        "task_relation": f"relation-{category}",
                    } for category_index, category in enumerate(categories)],
                }},
            }

        records = [record(f"case-{category_index}-{case_index}", [category])
                   for category_index, category in enumerate(COUNTED_CATEGORIES)
                   for case_index in range(5)]
        records.append(record("case-multi-label", COUNTED_CATEGORIES[:2]))
        groups = (records[:11], records[11:])
        bindings = []
        qualifications = {}
        for run_index, group in enumerate(groups, 1):
            source = self.root / f"source-{run_index}"; source.mkdir()
            for index in range(1, len(group) + 1):
                (source / f"16_03_result_{index:04d}.json").write_text("{}\n")
            manifest = self.root / f"classification-{run_index}.json"
            manifest.write_text(json.dumps({"source_run": str(source),
                                            "records": group}) + "\n")
            binding = self._binding(manifest)
            binding["path"] = manifest.relative_to(
                completion_gate.WORKSPACE_ROOT).as_posix()
            binding["source_run"] = source.relative_to(
                completion_gate.WORKSPACE_ROOT).as_posix()
            bindings.append(binding)
            for item in group:
                qualifications[item["case_id"]] = {
                    "qualified": True, "reasons": [],
                    "classification": manifest.relative_to(
                        completion_gate.WORKSPACE_ROOT).as_posix(),
                    "classification_sha256": hashlib.sha256(
                        manifest.read_bytes()).hexdigest(),
                }
        value = {
            "schema_version": "visual-capability-distribution-v4",
            "classifications": bindings,
            "exclusions": None,
            **summarize(records, {}, qualifications),
        }
        self.assertEqual([5] * len(COUNTED_CATEGORIES),
                         [item["required"] for item in value["distribution"]])
        self.assertEqual([6, 6, 5, 5],
                         [item["count"] for item in value["distribution"]])
        self.assertEqual(21, value["qualified_count"])
        self.assertEqual(22, value["capability_membership_count"])
        self.assertEqual(1, value["multi_label_count"])
        errors = []
        self.assertTrue(completion_gate._recompute_category_gate(value, errors), errors)
        self.assertEqual([], errors)
        self.assertEqual(2, validate_run.call_count)
        self.assertEqual(21, qualify.call_count)

        forged = {**value, "multi_label_count": 0}
        forged_errors = []
        self.assertFalse(completion_gate._recompute_category_gate(forged, forged_errors))
        self.assertEqual([{"code": "category_gate_invalid",
                           "reason": "category_gate_recompute_mismatch:multi_label_count"}],
                         forged_errors)

    @patch("report_pipeline.completion_gate._bound_file")
    @patch("report_pipeline.completion_gate._validate_formal_task")
    @patch("report_pipeline.completion_gate._check_review_gate", return_value=True)
    @patch("report_pipeline.completion_gate._check_full_test_run", return_value=True)
    @patch("report_pipeline.completion_gate._recompute_category_gate", return_value=True)
    @patch("report_pipeline.completion_gate._require_formal_freeze_ready")
    @patch("report_pipeline.completion_gate._validate_pipeline_freeze")
    @patch("report_pipeline.completion_gate.validate_submission")
    def test_orchestrator_passes_only_after_strict_validators_succeed(
            self, static, validate_freeze, require_freeze, category, tests, reviews,
            validate_task, bound_file):
        evidence = self.root / "evidence"; evidence.mkdir()
        packet = evidence / "completion_packet.json"
        html = evidence / "final_pipeline_audit.html"
        ids = [f"owner__repo-{index}" for index in range(1, 6)]
        html.write_text(" ".join([*COUNTED_CATEGORIES, *ids, "Pass@5 F2P P2P 人工"]))
        freeze_path = evidence / "freeze.json"; freeze_path.write_text("{}\n")
        test_path = evidence / "final_full_test_run.json"; test_path.write_text("{}\n")
        packet.write_text(json.dumps({
            "schema_version": "visual-exam-completion-packet-v1", "report_root": "report",
            "pipeline_freeze": {}, "category_distribution": {}, "full_test_run": {},
            "review_gate": {}, "iid_tasks": [{"instance_id": item} for item in ids],
            "submission_html": {},
        }) + "\n")
        static.return_value = {"status": "static_layout_complete_not_exam_ready",
                               "tasks": [{"instance_id": item,
                                          "status": "valid_static_contract"} for item in ids]}
        validate_freeze.return_value = (freeze_path, {"formal_inventory": True})
        validate_task.side_effect = [(item, index == 0) for index, item in enumerate(ids)]
        bound_file.return_value = html

        def formal_json(_binding, label, _errors, required_root=completion_gate.EVIDENCE_ROOT):
            if label == "category_distribution": return {"gate": True}, evidence / "category.json"
            if label == "full_test_run": return {"status": "passed"}, test_path
            if label == "review_gate": return {"reviews": []}, evidence / "reviews.json"
            raise AssertionError(label)

        with patch("report_pipeline.completion_gate.EVIDENCE_ROOT", evidence), \
             patch("report_pipeline.completion_gate._formal_json", side_effect=formal_json):
            result = completion_gate.validate(packet)
        self.assertEqual("passed", result["status"])
        self.assertEqual(5, validate_task.call_count)
        require_freeze.assert_called_once()
        category.assert_called_once()
        tests.assert_called_once()
        reviews.assert_called_once()


if __name__ == "__main__":
    unittest.main()
