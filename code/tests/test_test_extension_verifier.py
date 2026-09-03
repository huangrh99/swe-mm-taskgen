from pathlib import Path
import hashlib
import tempfile
import unittest

from report_pipeline import test_extension_verifier as subject


class TestExtensionVerifierTests(unittest.TestCase):
    def packet(self):
        return {
            "task_id": "o__r-1",
            "frozen_visual_classification": {
                "atomic_visual_constraints": [{
                    "constraint_id": "constraint_001",
                    "decision_critical": "是",
                }],
            },
            "existing_tests": {"file_hashes": {"test/a.js": "a" * 64}},
            "repository_test_context": {
                "working_directory": ".", "target_command": "npm test",
                "allowed_test_commands": [{"command_id": "frozen_target",
                    "working_directory": ".", "command": "npm test"}],
                "writable_test_roots": ["test/"], "test_collection_roots": ["test/"],
            },
        }

    def annotation(self):
        return {
            "schema_version": "existing-tests-extension-v3",
            "task_id": "o__r-1",
            "status": "no_additional_tests_needed",
            "behavioral_contract": {
                "observable_requirements": [{
                    "requirement_id": "constraint_001",
                    "contract": "The rendered connection contains only its two docking points.",
                }],
                "preserved_behaviors": ["Unrelated connections retain their prior routing."],
                "implementation_variation": (
                    "Any implementation producing the required rendered geometry is accepted."),
            },
            "coverage": [{
                "requirement_id": "constraint_001",
                "visual_category": "空间布局与几何理解",
                "coverage": "直接覆盖",
                "existing_test_ids": ["layout-straight"],
                "assertion_summary": "Rendered waypoints contain only source and target docking points.",
                "reason": "The assertion observes the public geometry rather than implementation text.",
            }],
            "test_bundles": [],
            "oracle_quality_plan": {
                "status": "proposed_not_executed",
                "curator_only": True,
                "negative_variants": [{
                    "variant_id": "keeps-extra-bend",
                    "defect_preserved_or_introduced": "Leaves an extra internal waypoint.",
                    "expected_failure_test_ids": ["layout-straight"],
                }],
                "equivalent_positive_variant": {
                    "description": "Uses a different routing helper but returns two docking points.",
                    "expected_pass_test_ids": ["layout-straight"],
                },
            },
            "missing_context": [],
            "summary": "Existing functional assertions cover the requirement.",
            "human_review_required": False,
            "human_review_reasons": [],
        }

    def test_accepts_complete_functional_coverage_without_new_bundle(self):
        subject.validate_annotation(self.annotation(), self.packet(), subject.SCHEMA)

    def test_rejects_missing_decision_critical_requirement(self):
        value = self.annotation()
        value["coverage"] = []
        with self.assertRaisesRegex(ValueError, "decision-critical"):
            subject.validate_annotation(value, self.packet(), subject.SCHEMA)

    def test_rejects_test_patch_that_writes_outside_test_root(self):
        value = self.annotation()
        value["status"] = "additional_tests_proposed"
        value["test_bundles"] = [{
            "bundle_id": "gap-1",
            "template_test_ids": ["layout-straight"],
            "target_requirement_ids": ["constraint_001"],
            "target_visual_capabilities": ["空间布局与几何理解"],
            "coverage_gap_reason": "Need another observable case.",
            "why_assertions_measure_requirements": "Checks rendered geometry.",
            "oracle_type": "numeric_layout_geometry_assertion",
            "predicted_transition": "candidate_f2p",
            "predicted_base_behavior": "Incorrect waypoint count.",
            "predicted_reference_behavior": "Two docking points.",
            "working_directory": ".",
            "test_command": "npm test",
            "stable_test_ids": ["gap-1"],
            "files": [{"path": "lib/gold.js", "operation": "add",
                       "content": "it('gap-1', () => true);", "sha256_before": None}],
            "unified_test_patch": "--- /dev/null\n+++ b/lib/gold.js\n@@ -0,0 +1 @@\n+it('gap-1', () => true);",
            "result_parser": "Mocha JSON",
            "environment_assumptions": [],
            "execution_preflight": {
                "command_evidence": "Uses the frozen npm test command.",
                "collection_evidence": "The frozen runner collects test files.",
                "import_and_mock_evidence": ["Uses only the supplied assertion helper."],
                "observable_oracle_evidence": "Reads rendered geometry.",
                "parallel_isolation": "Uses no shared port or temporary file.",
                "precondition_failures": ["Fails if either endpoint is absent."],
            },
            "equivalence_self_check": "Accepts any implementation with the same geometry.",
            "vacuous_pass_checks": ["Assert both endpoints exist before comparing geometry."],
            "surface_signal_resistance": "Requires rendered geometry, not a source token.",
        }]
        with self.assertRaisesRegex(ValueError, "outside"):
            subject.validate_annotation(value, self.packet(), subject.SCHEMA)

    def test_human_visual_input_rejects_fixed_after_asset(self):
        review = {"rows": [{
            "case_id": "o__r-1", "problem_statement_leak_free": True,
            "decision": "keep", "text_only_sufficient": "no", "ocr_replaceable": "no",
            "images": [{"solver_visible": True, "contains_fixed_after": True,
                        "contains_solution_evidence": False}],
        }]}
        with self.assertRaisesRegex(ValueError, "leakage"):
            subject._human_row(review, "o__r-1")

    def test_human_visual_input_requires_explicit_keep(self):
        review = {"rows": [{
            "case_id": "o__r-1", "problem_statement_leak_free": True,
            "decision": "needs_review", "text_only_sufficient": "no",
            "ocr_replaceable": "no",
            "images": [{"solver_visible": True, "contains_fixed_after": False,
                        "contains_solution_evidence": False}],
        }]}
        with self.assertRaisesRegex(ValueError, "human keep"):
            subject._human_row(review, "o__r-1")

    def test_solver_visible_assets_bind_only_hash_verified_local_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"approved visual bytes"
            digest = hashlib.sha256(payload).hexdigest()
            (root / f"asset_01_{digest[:12]}.png").write_bytes(payload)
            (root / "asset_02_badbadbadbad.png").write_bytes(b"wrong")
            assets = subject._solver_visible_assets({"images": [
                {"asset_id": digest, "role": "before_only", "solver_visible": True},
                {"asset_id": "b" * 64, "role": "after_only", "solver_visible": False},
            ]}, [root])
            self.assertEqual(len(assets), 1)
            self.assertEqual(assets[0]["attachment_status"], "bound")
            self.assertEqual(Path(assets[0]["local_path"]).read_bytes(), payload)

    def test_prepare_packet_v3_freezes_command_and_collection_roots(self):
        packet = self.packet()
        del packet["repository_test_context"]["allowed_test_commands"]
        del packet["repository_test_context"]["test_collection_roots"]
        prepared = subject.prepare_packet_v3(packet)
        context = prepared["repository_test_context"]
        self.assertEqual(context["allowed_test_commands"][0]["command"], "npm test")
        self.assertEqual(context["test_collection_roots"], ["test/"])
        self.assertEqual(prepared["verifier_contract_version"],
                         "existing-tests-extension-v3")

    def test_accepts_harbor_tests_root_for_generated_bundle(self):
        packet = self.packet()
        packet["repository_test_context"].update({
            "working_directory": ".", "target_command": "bash /tests/test.sh",
            "allowed_test_commands": [{"command_id": "frozen_target",
                "working_directory": ".", "command": "bash /tests/test.sh"}],
            "writable_test_roots": ["tests/"], "test_collection_roots": ["tests/"],
        })
        value = self.annotation()
        value["status"] = "additional_tests_proposed"
        value["test_bundles"] = [{
            "bundle_id": "gap-1", "template_test_ids": ["layout-straight"],
            "target_requirement_ids": ["constraint_001"],
            "target_visual_capabilities": ["空间布局与几何理解"],
            "coverage_gap_reason": "Need observable case.",
            "why_assertions_measure_requirements": "Checks rendered geometry.",
            "oracle_type": "numeric_layout_geometry_assertion",
            "predicted_transition": "candidate_f2p",
            "predicted_base_behavior": "Wrong geometry.",
            "predicted_reference_behavior": "Correct geometry.",
            "working_directory": ".", "test_command": "bash /tests/test.sh",
            "stable_test_ids": ["gap-1"],
            "files": [{"path": "tests/gap.js", "operation": "add",
                       "content": "it('gap-1', () => assert(true));", "sha256_before": None}],
            "unified_test_patch": "--- /dev/null\n+++ b/tests/gap.js\n@@ -0,0 +1 @@\n+it('gap-1', () => assert(true));", "result_parser": "JSON",
            "environment_assumptions": [],
            "execution_preflight": {
                "command_evidence": "Uses the frozen Harbor command.",
                "collection_evidence": "The frozen harness collects tests/gap.js.",
                "import_and_mock_evidence": ["Uses only supplied globals."],
                "observable_oracle_evidence": "Reads rendered geometry.",
                "parallel_isolation": "Uses no shared resources.",
                "precondition_failures": ["Requires the rendered subject."],
            },
            "equivalence_self_check": "Accepts alternative implementations.",
            "vacuous_pass_checks": ["Require the rendered subject to exist."],
            "surface_signal_resistance": "Checks observable geometry.",
        }]
        subject.validate_annotation(value, packet, subject.SCHEMA)

    def test_prompt_requires_functional_not_source_equivalence(self):
        prompt = subject.PROMPT.read_text()
        self.assertIn("observable functional equivalence", prompt)
        self.assertIn("not textual or structural equality", prompt)
        self.assertIn("must never output final `FAIL_TO_PASS`", prompt)
        self.assertIn("implementation-independent behavioral contract", prompt)
        self.assertIn("vacuous", prompt)
        self.assertIn("equivalent correct implementation", prompt)
        self.assertIn("allowed_test_commands", prompt)
        self.assertIn("fixed ports", prompt)

    def test_rejects_non_frozen_test_command(self):
        value = self.annotation()
        value["status"] = "additional_tests_proposed"
        bundle = self._bundle()
        bundle["test_command"] = "npm test -- invented"
        value["test_bundles"] = [bundle]
        with self.assertRaisesRegex(ValueError, "frozen allowed command"):
            subject.validate_annotation(value, self.packet(), subject.SCHEMA)

    def test_rejects_stable_id_absent_from_emitted_test(self):
        value = self.annotation()
        value["status"] = "additional_tests_proposed"
        bundle = self._bundle()
        bundle["files"][0]["content"] = "assert(true);"
        value["test_bundles"] = [bundle]
        with self.assertRaisesRegex(ValueError, "parser-visible"):
            subject.validate_annotation(value, self.packet(), subject.SCHEMA)

    def test_runner_replaces_model_diff_with_deterministic_valid_patch(self):
        value = self.annotation()
        value["status"] = "additional_tests_proposed"
        bundle = self._bundle()
        bundle["unified_test_patch"] = (
            "--- /dev/null\n+++ b/test/gap.js\n@@ -0,0 +1,2 @@\n"
            "+it('generated: straight geometry', () => {});")
        value["test_bundles"] = [bundle]
        subject.validate_annotation(value, self.packet(), subject.SCHEMA)
        generated = bundle["unified_test_patch"]
        self.assertIn("@@ -0,0 +1 @@", generated)
        subject._validate_unified_hunk_counts(generated)

    def test_rejects_relative_import_without_supplied_module_bytes(self):
        value = self.annotation()
        value["status"] = "additional_tests_proposed"
        bundle = self._bundle()
        bundle["files"][0]["content"] = (
            "import Subject from '../src/subject';\n"
            "it('generated: straight geometry', () => Subject());")
        value["test_bundles"] = [bundle]
        with self.assertRaisesRegex(ValueError, "without supplied bytes"):
            subject.validate_annotation(value, self.packet(), subject.SCHEMA)

    def _bundle(self):
        return {
            "bundle_id": "gap-1", "template_test_ids": ["layout-straight"],
            "target_requirement_ids": ["constraint_001"],
            "target_visual_capabilities": ["空间布局与几何理解"],
            "coverage_gap_reason": "Need observable coverage.",
            "why_assertions_measure_requirements": "Checks public geometry.",
            "oracle_type": "numeric_layout_geometry_assertion",
            "predicted_transition": "candidate_f2p",
            "predicted_base_behavior": "Wrong geometry.",
            "predicted_reference_behavior": "Correct geometry.",
            "working_directory": ".", "test_command": "npm test",
            "stable_test_ids": ["generated: straight geometry"],
            "files": [{"path": "test/gap.js", "operation": "add",
                       "content": "it('generated: straight geometry', () => {});",
                       "sha256_before": None}],
            "unified_test_patch": "--- /dev/null\n+++ b/test/gap.js\n@@ -0,0 +1 @@\n+it('generated: straight geometry', () => {});",
            "result_parser": "Mocha", "environment_assumptions": [],
            "execution_preflight": {
                "command_evidence": "Frozen command.",
                "collection_evidence": "Collected root.",
                "import_and_mock_evidence": ["No imports."],
                "observable_oracle_evidence": "Public geometry.",
                "parallel_isolation": "No shared resources.",
                "precondition_failures": ["Requires subject."],
            },
            "equivalence_self_check": "Alternative structure passes.",
            "vacuous_pass_checks": ["Require subject."],
            "surface_signal_resistance": "Checks public output.",
        }

    def test_rejects_missing_behavioral_contract(self):
        value = self.annotation()
        del value["behavioral_contract"]
        with self.assertRaises(Exception):
            subject.validate_annotation(value, self.packet(), subject.SCHEMA)

    def test_rejects_oracle_quality_ids_not_present_in_coverage_or_bundles(self):
        value = self.annotation()
        value["oracle_quality_plan"]["negative_variants"][0][
            "expected_failure_test_ids"] = ["unknown-test"]
        with self.assertRaisesRegex(ValueError, "oracle-quality test identity"):
            subject.validate_annotation(value, self.packet(), subject.SCHEMA)

    def test_insufficient_context_can_name_future_oracle_ids_without_bundle(self):
        value = self.annotation()
        value["status"] = "coverage_gap_but_insufficient_context"
        value["coverage"][0].update(
            coverage="当前信息不足", existing_test_ids=[], assertion_summary="")
        value["oracle_quality_plan"]["negative_variants"][0][
            "expected_failure_test_ids"] = ["future-generated-test"]
        value["oracle_quality_plan"]["equivalent_positive_variant"][
            "expected_pass_test_ids"] = ["future-generated-test"]
        value["missing_context"] = ["Complete source module bytes"]
        subject.validate_annotation(value, self.packet(), subject.SCHEMA)

    def test_accepts_oracle_quality_ids_from_measured_regression_tests(self):
        packet = self.packet()
        packet["existing_tests"]["measured_transitions"] = [{
            "test_id": "measured-p2p", "observed_type": "P2P",
        }]
        value = self.annotation()
        value["oracle_quality_plan"]["equivalent_positive_variant"][
            "expected_pass_test_ids"] = ["layout-straight", "measured-p2p"]
        subject.validate_annotation(value, packet, subject.SCHEMA)

    def test_audit_renders_behavior_contract_and_unexecuted_oracle_quality_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            packet = self.packet()
            packet["existing_tests"].update({
                "measured_counts": {"F2P": 1, "P2P": 1},
                "repeats_per_state": 3,
            })
            target = subject._render(output, packet, {
                "status": "complete", "annotation": self.annotation()})
            page = target.read_text()
            self.assertIn("实现无关的行为契约", page)
            self.assertIn("Oracle-quality validation 计划", page)
            self.assertIn("proposed_not_executed", page)
            self.assertIn("未执行不得视为通过", page)


if __name__ == "__main__":
    unittest.main()
