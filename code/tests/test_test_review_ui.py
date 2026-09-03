import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from report_pipeline.paths import TMP_ROOT
from report_pipeline import test_review_ui as subject


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value))


class TestReviewUiTests(unittest.TestCase):
    def verifier_fixture(self, root: Path, *, repeats: int = 3, bundle: bool = False) -> Path:
        source = root / "verifier"
        source.mkdir()
        packet = {
            "task_id": "owner__repo-7",
            "frozen_visual_classification": {"atomic_visual_constraints": [{
                "constraint_id": "constraint_001", "decision_critical": "是",
                "description": "The rendered item remains left of its peer",
                "visual_category": "空间布局与几何理解",
            }]},
            "measurement_boundary": {
                "correctness_target": "observable functional equivalence, not source-code equality"
            },
            "existing_tests": {
                "repeats_per_state": repeats,
                "measured_counts": {"F2P": 1, "P2P": 1},
                "measured_transitions": [
                    {"test_id": "f2p_layout", "class": "F2P", "expected": "fail->pass",
                     "actual": "fail->pass", "matches": True,
                     "source": "vlm_generated_test"},
                    {"test_id": "p2p_other", "class": "P2P", "expected": "pass->pass",
                     "actual": "pass->pass", "matches": True,
                     "source": "author_or_existing_component_test"},
                ],
                "files": [{"path": "tests/layout.test.js", "sha256": "a" * 64,
                           "content": "expect(render()).toEqual('left');"},
                          {"path": "package.json", "sha256": "b" * 64,
                           "content": "{\"scripts\":{\"test\":\"mocha\"}}"},
                          {"path": "tests/generated.test.js", "sha256": "c" * 64,
                           "content": "expect(visual()).toEqual('left');"}],
                "author_test_patch": (
                    "diff --git a/tests/layout.test.js b/tests/layout.test.js\n"
                    "--- a/tests/layout.test.js\n+++ b/tests/layout.test.js\n"
                    "+it('p2p_other', () => expect(other()).toBe(true));\n"),
                "current_generated_test": {"path": "tests/generated.test.js"},
            },
        }
        bundles = []
        if bundle:
            bundles = [{
                "bundle_id": "gap_01", "stable_test_ids": ["new_visual_test"],
                "predicted_transition": "candidate_f2p",
                "predicted_base_behavior": "wrong order",
                "predicted_reference_behavior": "correct order",
                "why_assertions_measure_requirements": "Reads computed geometry",
                "unified_test_patch": "--- /dev/null\n+++ b/tests/new.test.js\n+expect(x).toBe(1)",
                "files": [{"path": "tests/new.test.js", "operation": "add",
                           "content": "expect(x).toBe(1)"}],
            }]
        result = {
            "task_id": "owner__repo-7", "status": "complete",
            "annotation": {
                "task_id": "owner__repo-7",
                "status": "additional_tests_proposed" if bundle else "no_additional_tests_needed",
                "summary": "Functional coverage analysis",
                "coverage": [{
                    "requirement_id": "constraint_001", "coverage": "直接覆盖",
                    "assertion_summary": "Checks public rendered geometry",
                    "reason": "Behavioral oracle", "existing_test_ids": ["f2p_layout"],
                }],
                "test_bundles": bundles,
            },
        }
        packet_path = source / subject.PACKET_NAME
        result_path = source / subject.RESULT_NAME
        dump(packet_path, packet)
        dump(result_path, result)
        dump(source / subject.MANIFEST_NAME, {
            "packet": {"sha256": sha(packet_path)},
            "result": {"sha256": sha(result_path)},
        })
        return source

    def test_render_shows_constraints_code_measurement_and_functional_boundary(self):
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            source = self.verifier_fixture(root)
            manifest = subject.render(source, root / "review")
            self.assertEqual(1, manifest["candidate_count"])
            self.assertEqual(1, manifest["approval_eligible_count"])
            page = (root / "review/20_12_02_test_review.html").read_text()
            self.assertIn("决策关键视觉约束 → 测试", page)
            self.assertIn("observable functional equivalence", page)
            self.assertIn("expect(render()).toEqual", page)
            self.assertIn("已知假阳性风险", page)
            self.assertIn("已知假阴性风险", page)
            self.assertNotIn("审核人", page)
            payload = json.loads((root / "review/20_12_01_review_payload.json").read_text())
            self.assertTrue(payload["cases"][0]["measurement"]["approval_eligible"])
            case = payload["cases"][0]
            self.assertEqual(["tests/layout.test.js"],
                             [x["path"] for x in case["pr_author_test_files"]])
            self.assertEqual(["tests/generated.test.js"],
                             [x["path"] for x in case[
                                 "verifier_generated_test_files"]])
            self.assertEqual(["package.json"],
                             [x["path"] for x in case["repository_context_files"]])
            semantics = {row["test_id"]: row for row in case["test_semantics"]}
            self.assertEqual("Verifier 生成", semantics["f2p_layout"]["origin_label"])
            self.assertEqual("verifier_generated", semantics["f2p_layout"]["origin"])
            self.assertEqual("Checks public rendered geometry",
                             semantics["f2p_layout"]["purpose"])
            self.assertEqual("F2P", semantics["f2p_layout"]["classification"])
            self.assertEqual("base_gold_measured",
                             semantics["f2p_layout"]["classification_basis"])
            self.assertIn("回归保护", semantics["p2p_other"]["purpose"])
            self.assertEqual("pr_author_test", semantics["p2p_other"]["origin"])
            self.assertIn("测试输入来源与目的", page)
            self.assertIn("测试目的", page)
            self.assertIn("Verifier 生成的候选测试", page)
            self.assertNotIn("本轮 Verifier 前由造题流程生成的测试", page)
            self.assertNotIn("本轮 Verifier 新生成测试", page)

    def test_repeats_below_three_blocks_approval_in_page_and_export_audit(self):
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            source = self.verifier_fixture(root, repeats=1)
            output = root / "review"
            subject.render(source, output)
            payload = json.loads((output / "20_12_01_review_payload.json").read_text())
            case = payload["cases"][0]
            self.assertFalse(case["measurement"]["approval_eligible"])
            self.assertIn("repeats gate", case["measurement"]["approval_blockers"][0])
            decisions = root / "decisions.json"
            dump(decisions, {
                "schema_version": "test-review-human-export-v1",
                "source_manifest_sha256": payload["source_manifest_sha256"],
                "rows": [{
                    "task_id": case["task_id"],
                    "candidate_binding_sha256": case["candidate_binding_sha256"],
                    "decision": "approved", "reason": "looks fine",
                    "false_positive_risks": "none", "false_negative_risks": "none",
                }],
            })
            with self.assertRaisesRegex(ValueError, "approval hard gate failed"):
                subject.audit(output, decisions)

    def test_unmeasured_generated_bundle_is_visible_and_blocks_approval(self):
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            source = self.verifier_fixture(root, bundle=True)
            output = root / "review"
            manifest = subject.render(source, output)
            self.assertEqual(0, manifest["approval_eligible_count"])
            page = (output / "20_12_02_test_review.html").read_text()
            self.assertIn("完整 unified diff", page)
            self.assertIn("new_visual_test", page)
            payload = json.loads((output / "20_12_01_review_payload.json").read_text())
            self.assertEqual("gap_01", payload["cases"][0]["measurement"][
                "unmeasured_bundles"][0]["bundle_id"])
            generated = payload["cases"][0]["verifier_generated_test_files"]
            self.assertEqual(["tests/generated.test.js", "tests/new.test.js"],
                             [item["path"] for item in generated])
            self.assertEqual(["prior_run", "current_run"],
                             [item["generation_scope"] for item in generated])

    def test_reject_export_is_hash_bound_and_auditable_without_reviewer(self):
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            source = self.verifier_fixture(root, repeats=1)
            output = root / "review"
            subject.render(source, output)
            payload = json.loads((output / "20_12_01_review_payload.json").read_text())
            case = payload["cases"][0]
            decisions = root / "decisions.json"
            dump(decisions, {
                "schema_version": "test-review-human-export-v1",
                "source_manifest_sha256": payload["source_manifest_sha256"],
                "rows": [{
                    "task_id": case["task_id"],
                    "candidate_binding_sha256": case["candidate_binding_sha256"],
                    "decision": "revision_requested", "reason": "repeat twice more",
                    "false_positive_risks": "style proxy may pass incorrectly",
                    "false_negative_risks": "alternate layout may be valid",
                }],
            })
            audited = subject.audit(output, decisions)
            self.assertEqual(1, audited["human_export"]["counts"]["revision_requested"])
            self.assertTrue((output / "20_12_04_human_audit.json").is_file())


if __name__ == "__main__":
    unittest.main()
