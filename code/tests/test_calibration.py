import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import jsonschema

from report_pipeline.calibration import apply


class CalibrationTests(unittest.TestCase):
    def test_workflow_uses_the_shared_human_audit_validator(self):
        from report_pipeline import calibration, workflow

        self.assertIs(workflow.validate_human_gate_audit,
                      calibration.validate_human_gate_audit)

    def _v2(self, dossier, measurement, manifest, task, test_context):
        from report_pipeline.calibration import task_directory_checksum

        manifest_value = json.loads(manifest.read_text())
        return {
            "schema_version": "dual-human-calibration-v2",
            "candidate_id": json.loads(dossier.read_text())["candidate_id"],
            "dossier_sha256": hashlib.sha256(dossier.read_bytes()).hexdigest(),
            "test_manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
            "test_review_context_sha256": hashlib.sha256(test_context.read_bytes()).hexdigest(),
            "measurement_sha256": hashlib.sha256(measurement.read_bytes()).hexdigest(),
            "task_directory_checksum": task_directory_checksum(task),
            "multimodal_necessity": {
                "state": "approved", "reviewer": "visual-reviewer",
                "reason": "layout cannot be reconstructed from the text",
                "reviewed_at": "2026-09-01T01:00:00Z",
                "text_only_sufficiency": "insufficient", "ocr_replaceable": "no",
                "non_text_visual_fact": "the label divider and spacing are visible",
                "evidence_asset_ids": [json.loads(dossier.read_text())["leakage_policy"]["safe_agent_assets"][0]["asset_id"]],
                "text_only_notes": "text names the component but not the target layout",
                "text_first_recorded_at": "2026-09-01T00:59:00Z",
                "images_revealed_at": "2026-09-01T00:59:01Z",
            },
            "f2p_p2p_semantic_validity": {
                "state": "approved", "reviewer": "test-reviewer",
                "reason": "each requirement and neighboring regression is covered",
                "reviewed_at": "2026-09-01T01:05:00Z", "coverage": "complete",
                "missing_behaviors": None,
                "test_reviews": [
                    {"test_id": item["test_id"], "class": item["class"],
                     "decision": "valid", "reason": "matches the stated behavior"}
                    for item in manifest_value["tests"]
                ],
            },
        }

    def test_both_human_gates_are_required_for_final_admission(self):
        root = Path(__file__).resolve().parent / "fixtures/carbon_20978_archive"
        source = root / "18_01_candidate_dossier.json"
        measurement = root / "18_07_browser_f2p_p2p_measurement.json"
        with tempfile.TemporaryDirectory() as temporary:
            dossier = Path(temporary) / "dossier.json"; dossier.write_bytes(source.read_bytes())
            for semantics in ("pending", "approved"):
                decision = Path(temporary) / f"{semantics}.json"
                value = {"schema_version": "dual-human-calibration-v1",
                         "candidate_id": json.loads(dossier.read_text())["candidate_id"],
                         "dossier_sha256": hashlib.sha256(dossier.read_bytes()).hexdigest(),
                         "measurement_sha256": hashlib.sha256(measurement.read_bytes()).hexdigest(),
                         "multimodal_necessity": {"state": "approved", "reviewer": "r1", "reason": "pixels", "reviewed_at": "2026-09-01T00:00:00Z"},
                         "f2p_p2p_semantic_validity": {"state": semantics,
                             "reviewer": "r2" if semantics == "approved" else None,
                             "reason": "tests" if semantics == "approved" else None,
                             "reviewed_at": "2026-09-01T00:01:00Z" if semantics == "approved" else None}}
                decision.write_text(json.dumps(value))
                result = apply(dossier, measurement, decision, Path(temporary) / f"out-{semantics}.json")
                self.assertFalse(result["benchmark_eligibility"]["may_enter_final_taskset"])
                self.assertEqual("executable_candidate",
                                 result["benchmark_eligibility"]["current_stage"])
                self.assertIn("dual_human_calibration_v2_required",
                              result["benchmark_eligibility"]["blocking_human_gates"])
                self.assertEqual(result["test_calibration"]["measurement_state"], "executed_all_transitions_match")

    def test_binding_tamper_is_rejected(self):
        root = Path(__file__).resolve().parent / "fixtures/carbon_20978_archive"
        dossier = root / "18_01_candidate_dossier.json"
        measurement = root / "18_07_browser_f2p_p2p_measurement.json"
        with tempfile.TemporaryDirectory() as temporary:
            decision = Path(temporary) / "decision.json"
            decision.write_text(json.dumps({"schema_version": "dual-human-calibration-v1",
                "candidate_id": json.loads(dossier.read_text())["candidate_id"],
                "dossier_sha256": "0" * 64, "measurement_sha256": hashlib.sha256(measurement.read_bytes()).hexdigest(),
                "multimodal_necessity": {"state": "pending", "reviewer": None, "reason": None, "reviewed_at": None},
                "f2p_p2p_semantic_validity": {"state": "pending", "reviewer": None, "reason": None, "reviewed_at": None}}))
            with self.assertRaisesRegex(ValueError, "binding changed"):
                apply(dossier, measurement, decision, Path(temporary) / "out.json")

    def test_v2_requires_both_detailed_bound_reviews(self):
        fixtures = Path(__file__).resolve().parent / "fixtures"
        base = fixtures / "carbon_20978_archive"
        dossier = base / "18_01_candidate_dossier.json"
        measurement = base / "18_07_browser_f2p_p2p_measurement.json"
        manifest = base / "18_02_test_manifest.json"
        test_context = base / "18_40_test_review_context.json"
        task = fixtures / "tasks/carbon-design-system__carbon-20978"
        with tempfile.TemporaryDirectory() as temporary:
            decision = Path(temporary) / "decision.json"
            value = self._v2(dossier, measurement, manifest, task, test_context)
            from report_pipeline.paths import REPORT_ROOT
            schema = json.loads((REPORT_ROOT / "schemas/dual_human_calibration_v2.schema.json").read_text())
            jsonschema.validate(value, schema)
            without_reveal = json.loads(json.dumps(value))
            del without_reveal["multimodal_necessity"]["images_revealed_at"]
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(without_reveal, schema)
            decision.write_text(json.dumps(value))
            result = apply(dossier, measurement, decision, Path(temporary) / "out.json",
                           manifest, task, test_context)
            self.assertTrue(result["benchmark_eligibility"]["may_enter_final_taskset"])
            self.assertEqual(len(result["test_calibration"]["human_semantic_calibration"]["test_reviews"]), 8)

            value["multimodal_necessity"]["ocr_replaceable"] = "yes"
            decision.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "non-OCR"):
                apply(dossier, measurement, decision, Path(temporary) / "bad.json",
                      manifest, task, test_context)

            value = self._v2(dossier, measurement, manifest, task, test_context)
            value["f2p_p2p_semantic_validity"]["test_reviews"][0]["decision"] = "unclear"
            decision.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "reasoned valid decisions"):
                apply(dossier, measurement, decision, Path(temporary) / "bad-tests.json",
                      manifest, task, test_context)

            value = self._v2(dossier, measurement, manifest, task, test_context)
            value["multimodal_necessity"]["reviewer"] = "   "
            decision.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "reviewer"):
                apply(dossier, measurement, decision, Path(temporary) / "anonymous.json",
                      manifest, task, test_context)

            value = self._v2(dossier, measurement, manifest, task, test_context)
            value["multimodal_necessity"]["text_first_recorded_at"] = None
            decision.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "text-first"):
                apply(dossier, measurement, decision, Path(temporary) / "no-text-first.json",
                      manifest, task, test_context)

            value = self._v2(dossier, measurement, manifest, task, test_context)
            value["multimodal_necessity"]["text_first_recorded_at"] = "2026-09-01T00:59:00"
            decision.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "timezone"):
                apply(dossier, measurement, decision, Path(temporary) / "naive-time.json",
                      manifest, task, test_context)

            value = self._v2(dossier, measurement, manifest, task, test_context)
            value["multimodal_necessity"]["images_revealed_at"] = "2026-09-01T01:01:00Z"
            decision.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "before images were revealed"):
                apply(dossier, measurement, decision, Path(temporary) / "review-before-reveal.json",
                      manifest, task, test_context)


if __name__ == "__main__":
    unittest.main()
