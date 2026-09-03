import tempfile
import unittest
import json
from pathlib import Path

from report_pipeline.review_html import audit, render


class ReviewHtmlTest(unittest.TestCase):
    def test_full_evidence_is_rendered_and_escaped(self):
        base = Path(__file__).resolve().parent / "fixtures/carbon_20978_archive"
        with tempfile.TemporaryDirectory() as temporary:
            negative_controls = Path(temporary) / "negative_controls.json"
            negative_controls.write_text(json.dumps({
                "status": "all_controls_passed",
                "controls": {
                    "baseline": {
                        "reward": 0,
                        "control_passed": True,
                        "outcome_class": "behavioral_failure",
                        "expected_outcome": "nop must fail behavior assertions",
                        "summary": {"pass": 0, "fail": 1, "skip": 0, "missing": 0, "error": 0},
                    }
                },
            }))
            run_proposal = Path(temporary) / "run_proposal.json"
            run_proposal.write_text(json.dumps({
                "status": "awaiting_authorization",
                "agent": {"model_id": "ep-fixture-k3", "adapter": "kimi-code"},
                "pass_at_5": {"concurrency": 5,
                    "maximum_model_calls_for_five_valid_trials": 50,
                    "absolute_model_call_upper_bound_with_all_retries": 75},
            }))
            output = Path(temporary) / "review.html"
            render(base / "18_01_candidate_dossier.json", base / "18_02_test_manifest.json",
                   base / "18_03_f2p_p2p_source_measurement.json", output,
                   run_proposal_path=run_proposal,
                   negative_controls_path=negative_controls)
            text = output.read_text()
            self.assertIn("f2p_ai_gradient_decorator", text)
            self.assertIn("human_calibration_state", text)
            self.assertIn("自动准入不等于人工确认", text)
            self.assertIn("门 1 · Multimodal 必要性", text)
            self.assertIn("门 2 · F2P/P2P 语义有效性", text)
            self.assertIn("Harbor 结构化负向与隔离控制", text)
            self.assertIn("all_controls_passed", text)
            self.assertIn("Pass@5 冻结提案（尚未调用）", text)
            self.assertIn("ep-fixture-k3", text)
            self.assertIn("run_proposal", text)
            self.assertNotIn("Gemini Pass@5", text)
            self.assertNotIn("gemini_run_proposal", text)
            self.assertNotIn("<script", text)

            record = audit(output, Path(temporary) / "audit.json")
            self.assertEqual(record["status"], "passed")
            self.assertEqual(record["image_count"], 4)
            self.assertFalse(record["browser_rendering_claimed"])
            self.assertEqual(record["event_attributes"], 0)

            dossier = json.loads((base / "18_01_candidate_dossier.json").read_text())
            dossier["leakage_policy"]["safe_agent_assets"][0]["asset_id"] = '<img onerror="alert(1)">'
            injected = Path(temporary) / "injected.json"; injected.write_text(json.dumps(dossier))
            injected_html = Path(temporary) / "injected.html"
            render(injected, base / "18_02_test_manifest.json",
                   base / "18_03_f2p_p2p_source_measurement.json", injected_html,
                   negative_controls_path=negative_controls)
            self.assertNotIn('<img onerror=', injected_html.read_text())
            self.assertEqual(audit(injected_html, Path(temporary) / "injected_audit.json")["event_attributes"], 0)


if __name__ == "__main__":
    unittest.main()
