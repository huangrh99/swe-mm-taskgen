import unittest
from pathlib import Path
import tempfile
import json
from unittest import mock

from report_pipeline import v4_test_campaign as subject


class V4TestCampaignTests(unittest.TestCase):
    def test_rejects_bundle_without_literal_stable_id(self):
        result = {
            "schema_version": "v4-test-constructor-v1", "task_id": "o__r-1",
            "status": "test_bundle_proposed",
            "repository_observations": {"framework": "Jest", "package_manager": "npm",
                "working_directory": ".", "manifest_paths": ["package.json"],
                "test_config_paths": [], "nearby_test_paths": [], "author_test_paths": []},
            "behavioral_contract": [{"requirement_id": "r1", "observable_behavior": "x",
                "preserved_behavior": "y", "oracle": "rendered state"}],
            "test_bundle": {"working_directory": ".", "test_command": "npm test",
                "stable_test_ids": ["stable-id"], "predicted_transition": "candidate_f2p",
                "files": [{"path": "test/a.test.js", "operation": "add",
                           "content": "it('another id', () => {});"}],
                "collection_evidence": "package script", "functional_oracle_evidence": "DOM",
                "equivalent_implementation_check": "public behavior",
                "incomplete_implementation_check": "missing state fails"},
            "missing_context": [], "summary": "candidate"
        }
        with self.assertRaisesRegex(ValueError, "stable test IDs"):
            subject._validate_result(result, "o__r-1", Path.cwd())

    def test_accepts_explicit_insufficient_context_without_bundle(self):
        result = {
            "schema_version": "v4-test-constructor-v1", "task_id": "o__r-1",
            "status": "insufficient_context",
            "repository_observations": {"framework": "unknown", "package_manager": "unknown",
                "working_directory": ".", "manifest_paths": [], "test_config_paths": [],
                "nearby_test_paths": [], "author_test_paths": []},
            "behavioral_contract": [{"requirement_id": "r1", "observable_behavior": "x",
                "preserved_behavior": "y", "oracle": "not available"}],
            "test_bundle": None, "missing_context": ["fixture"], "summary": "blocked"
        }
        subject._validate_result(result, "o__r-1", Path.cwd())

    def test_normalises_exact_bound_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(subject._normalise_working_directory(str(root), root), ".")

    def test_rejects_absolute_workdir_outside_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "escapes bound repository"):
                subject._normalise_working_directory("/var/elsewhere", Path(directory))

    def test_partial_archive_is_accepted_when_consumed_sections_are_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "archive.json"
            value = {"status": "partial", "instance_id": "o__r-1", "sections": {
                "pull_request": {"status": "complete"},
                "files": {"status": "complete"},
                "diff": {"status": "complete"},
                "assets": {"status": "partial"},
            }}
            archive.write_text(json.dumps(value))
            case = {"case_id": "o__r-1", "source_bindings": {
                "source_archive": str(archive),
                "source_archive_sha256": subject._sha(archive),
            }}
            with mock.patch.object(subject, "WORKSPACE_ROOT", Path("/")):
                self.assertEqual(subject._archive(case)["status"], "partial")

    def test_rejects_duplicate_file_paths_and_dependency_install(self):
        base = {
            "schema_version": "v4-test-constructor-v1", "task_id": "o__r-1",
            "status": "test_bundle_proposed",
            "repository_observations": {"framework": "Jest", "package_manager": "npm",
                "working_directory": ".", "manifest_paths": [], "test_config_paths": [],
                "nearby_test_paths": [], "author_test_paths": []},
            "behavioral_contract": [{"requirement_id": "r1", "observable_behavior": "x",
                "preserved_behavior": "y", "oracle": "z"}],
            "test_bundle": {"working_directory": ".", "test_command": "npm test",
                "stable_test_ids": ["stable-id"], "predicted_transition": "candidate_f2p",
                "files": [{"path": "test/a.js", "operation": "add",
                           "content": "it('stable-id', () => {});"}],
                "collection_evidence": "x", "functional_oracle_evidence": "x",
                "equivalent_implementation_check": "x",
                "incomplete_implementation_check": "x"},
            "missing_context": [], "summary": "x"}
        duplicate = json.loads(json.dumps(base))
        duplicate["test_bundle"]["files"].append(dict(duplicate["test_bundle"]["files"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate emitted"):
            subject._validate_result(duplicate, "o__r-1", Path.cwd())
        install = json.loads(json.dumps(base))
        install["test_bundle"]["test_command"] = "npm ci && npm test"
        with self.assertRaisesRegex(ValueError, "frozen environment"):
            subject._validate_result(install, "o__r-1", Path.cwd())


if __name__ == "__main__":
    unittest.main()
