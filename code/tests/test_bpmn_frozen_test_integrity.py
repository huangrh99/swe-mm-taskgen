import unittest
from pathlib import Path


CASE_TESTS = (
    Path(__file__).resolve().parents[2]
    / "cases/bpmn-io__bpmn-js-2396/tests"
)


class BpmnFrozenTestIntegrityTests(unittest.TestCase):
    def test_bootstrap_does_not_patch_agent_visible_test_tree(self):
        bootstrap = (CASE_TESTS / "test.sh").read_text()
        self.assertNotIn("git -C /testbed apply", bootstrap)

    def test_grader_copies_frozen_tests_into_private_harness(self):
        grader = (CASE_TESTS / "sweb_grade.py").read_text()
        self.assertIn("shutil.copytree(payload, frozen_tests)", grader)
        self.assertNotIn("inventory(APP / 'test')", grader)

    def test_runner_loads_only_private_frozen_suites(self):
        runner = (CASE_TESTS / "sweb_runner.cjs").read_text()
        self.assertIn("const testRoot = '/harness/test';", runner)
        self.assertIn("require(testRoot + '/config/karma.unit.js')", runner)
        self.assertIn("test: testRoot", runner)
        self.assertNotIn("root + '/test/config/karma.unit.js'", runner)


if __name__ == "__main__":
    unittest.main()
