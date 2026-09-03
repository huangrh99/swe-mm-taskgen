import json
import tempfile
import unittest
from pathlib import Path

from report_pipeline.paths import TMP_ROOT
from report_pipeline.provisional_chain_audit import run


class ProvisionalChainAuditTests(unittest.TestCase):
    def test_binds_measured_controls_and_keeps_invalid_smoke_out_of_pass5(self):
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            instance = "owner__repo-1"
            category = root / "category.json"
            category.write_text(json.dumps({"rows": [{
                "case_id": instance, "counted": True,
                "primary_visual_category": "混合视觉能力",
                "strict_multimodal_admission": "非文字视觉信息候选不可替代",
            }]}) + "\n")
            baseline = [
                {"test_id": "f2p", "class": "F2P", "status": "fail"},
                {"test_id": "p2p", "class": "P2P", "status": "pass"},
            ]
            reference = [
                {"test_id": "f2p", "class": "F2P", "status": "pass"},
                {"test_id": "p2p", "class": "P2P", "status": "pass"},
            ]
            transitions = [
                {"test_id": "f2p", "matches": True},
                {"test_id": "p2p", "matches": True},
            ]
            source = root / "source.json"
            source.write_text(json.dumps({
                "baseline": {"test_manifest_sha256": "same", "results": baseline,
                             "scope": "source semantics"},
                "reference": {"test_manifest_sha256": "same", "results": reference},
                "measurement": {"all_transitions_match": True,
                                "transitions": transitions},
            }) + "\n")
            browser = root / "browser.json"
            browser.write_text(json.dumps({"all_transitions_match": True,
                                           "transitions": transitions,
                                           "oracle_kind": "chromium"}) + "\n")
            controls = root / "controls.json"
            controls.write_text(json.dumps({
                "candidate_id": instance,
                "status": "baseline_and_oracle_controls_passed",
                "task_material_sha256": "task",
                "controls": {
                    "baseline_nop": {"reward": 0, "results": baseline},
                    "oracle": {"reward": 1, "results": reference},
                },
            }) + "\n")
            smoke = root / "smoke.json"
            smoke.write_text(json.dumps({
                "formal_pass5": False,
                "classification": "invalid_trial_agent_timeout",
                "valid_behavioral_trial_count": 0,
                "terminal_evidence": {"trial_exception": "AgentTimeoutError",
                                      "trajectory_present": True},
            }) + "\n")
            result = run(instance, category, source, browser, controls, [smoke],
                         root / "out")
            self.assertEqual("measured_pending_human_semantic_gate",
                             result["gates"]["tests_measurement"])
            self.assertEqual("passed_provisional_only",
                             result["gates"]["harbor_controls"])
            self.assertEqual(0, result["k3"]["valid_trial_count"])
            self.assertEqual(5, result["k3"]["remaining_valid_trials"])
            self.assertFalse(result["formal_benchmark_admission"])
            self.assertTrue((root / "out/19_41_02_provisional_technical_chain.html").is_file())


if __name__ == "__main__":
    unittest.main()
