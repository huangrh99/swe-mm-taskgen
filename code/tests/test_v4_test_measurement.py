import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from report_pipeline import cli
from report_pipeline import v4_test_measurement as subject
from report_pipeline.paths import TMP_ROOT


def _git(command, cwd):
    return subprocess.run(["git", *command], cwd=cwd, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)


class V4TestMeasurementTests(unittest.TestCase):
    def setUp(self):
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="v4-measurement-test-",
                                                      dir=TMP_ROOT)
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def _repository(self):
        repository = self.root / "repositories/carbon"
        repository.mkdir(parents=True)
        _git(["init"], repository)
        _git(["config", "user.name", "Test"], repository)
        _git(["config", "user.email", "test@example.com"], repository)
        (repository / "calc.py").write_text("def value():\n    return 0\n")
        _git(["add", "calc.py"], repository)
        _git(["commit", "-m", "base"], repository)
        base = _git(["rev-parse", "HEAD"], repository).stdout.strip()
        patch = """diff --git a/calc.py b/calc.py
--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def value():
-    return 0
+    return 1
"""
        return repository, base, patch

    def _campaign(self, base, reference_patch):
        campaign = self.root / "campaign"
        case_id = "carbon-design-system__carbon-22019"
        case = campaign / "20_17_02_model_runs" / case_id
        case.mkdir(parents=True)
        packet = {
            "schema_version": "v4-test-constructor-packet-v1",
            "task_id": case_id,
            "repository": "carbon-design-system/carbon",
            "base_commit": base,
            "reference_head": "f" * 40,
            "reference_diff": reference_patch,
        }
        test_content = """import unittest
import calc

class GeneratedContract(unittest.TestCase):
    def test_STABLE_F2P(self):
        self.assertEqual(calc.value(), 1)

    def test_STABLE_P2P(self):
        self.assertIsInstance(calc.value(), int)

# STABLE_MISSING is deliberately parser-visible but not collected.
if __name__ == '__main__':
    unittest.main()
"""
        result = {
            "schema_version": "v4-test-constructor-v1",
            "task_id": case_id,
            "status": "test_bundle_proposed",
            "repository_observations": {
                "framework": "unittest", "package_manager": "python",
                "working_directory": ".", "manifest_paths": [],
                "test_config_paths": [], "nearby_test_paths": [],
                "author_test_paths": [],
            },
            "behavioral_contract": [{
                "requirement_id": "r1", "observable_behavior": "value is one",
                "preserved_behavior": "value remains an integer", "oracle": "return value",
            }],
            "test_bundle": {
                "working_directory": ".",
                "test_command": "python3 -m unittest -v test_generated.py",
                "stable_test_ids": ["STABLE_F2P", "STABLE_P2P", "STABLE_MISSING"],
                "predicted_transition": "unknown_until_measured",
                "files": [{"path": "test_generated.py", "operation": "add",
                           "content": test_content}],
                "collection_evidence": "explicit unittest file",
                "functional_oracle_evidence": "public return value",
                "equivalent_implementation_check": "any implementation returning one passes",
                "incomplete_implementation_check": "zero fails",
            },
            "missing_context": [], "summary": "fixture",
        }
        (case / "20_17_01_packet.json").write_text(json.dumps(packet))
        (case / "20_17_06_final.json").write_text(json.dumps(result))
        summary = {
            "schema_version": "v4-test-construction-campaign-v1",
            "records": [{"case_id": case_id, "repository": packet["repository"],
                         "status": "complete", "model_result_status": result["status"]}],
        }
        (campaign / "20_17_08_summary.json").write_text(json.dumps(summary))
        return campaign, case_id

    def test_exact_base_and_gold_classify_f2p_p2p_and_missing(self):
        _, base, patch = self._repository()
        campaign, case_id = self._campaign(base, patch)
        output = self.root / "measurement"

        result = subject.run(campaign, self.root / "repositories", output,
                             workers=1, timeout=30, setup_timeout=30)

        self.assertEqual({"measurement_rejected": 1}, result["counts"])
        record = result["records"][0]
        self.assertEqual("measurement_rejected", record["status"])
        self.assertEqual("evidence", record["failure_ledger"])
        self.assertEqual("target_test_not_observed", record["failure_class"])
        self.assertTrue(record["retryable"])
        transitions = {item["test_id"]: item for item in record["transitions"]}
        self.assertEqual("provisional_f2p", transitions["STABLE_F2P"]["classification"])
        self.assertEqual("provisional_p2p", transitions["STABLE_P2P"]["classification"])
        self.assertEqual("missing", transitions["STABLE_MISSING"]["classification"])
        self.assertEqual(["STABLE_F2P"], record["provisional_FAIL_TO_PASS"])
        self.assertEqual(["STABLE_P2P"], record["provisional_PASS_TO_PASS"])
        self.assertEqual([transitions["STABLE_MISSING"]], record["unresolved"])
        case_output = output / "20_19_01_case_runs" / case_id
        for name in ("20_19_03_base_stdout.txt", "20_19_03_base_stderr.txt",
                     "20_19_03_gold_stdout.txt", "20_19_03_gold_stderr.txt",
                     "20_19_04_base_run.json", "20_19_04_gold_run.json",
                     "20_19_05_transitions.json"):
            self.assertTrue((case_output / name).is_file(), name)
        self.assertFalse(list(self.root.rglob("v4-measurement-*/*/.git")))

    def test_technical_error_and_semantic_failure_stay_separate(self):
        technical = subject._test_status("stable", "command not found", 127,
                                         "technical_error")
        semantic = subject._test_status("stable", "stable ... FAIL", 1, "completed")
        missing = subject._test_status("stable", "zero tests", 0, "completed")
        self.assertEqual("error", technical["status"])
        self.assertEqual("fail", semantic["status"])
        self.assertEqual("missing", missing["status"])
        self.assertEqual("error", subject.classify_transition("error", "pass"))
        self.assertEqual("fail_to_fail", subject.classify_transition("fail", "fail"))

    def test_reconciles_only_observed_base_failure_with_hidden_gold_pass(self):
        base = {"test_id": "stable", "status": "fail",
                "evidence_lines": ["stable FAILED"]}
        gold = {"test_id": "stable", "status": "missing", "evidence_lines": []}
        run = {"execution_status": "completed",
               "suite_total": {"failed": 1, "passed": 99,
                               "line": "TOTAL: 1 FAILED, 99 SUCCESS"}}
        subject._reconcile_hidden_gold_pass(base, gold, run)
        self.assertEqual("pass", gold["status"])
        self.assertEqual("inferred_from_identical_bundle_and_base_collection",
                         gold["evidence_kind"])
        unseen_base = {"test_id": "p2p", "status": "missing", "evidence_lines": []}
        unseen_gold = {"test_id": "p2p", "status": "missing", "evidence_lines": []}
        subject._reconcile_hidden_gold_pass(unseen_base, unseen_gold, run)
        self.assertEqual("missing", unseen_gold["status"])

    def test_rejects_shell_control_and_unapproved_executable(self):
        for command in ("python3 test.py; rm x", "curl https://example.com",
                        "python3 $(echo test.py)"):
            with self.subTest(command=command):
                with self.assertRaises(ValueError):
                    subject._validate_command(command)

    def test_public_cli_dispatches_measurement(self):
        campaign = self.root / "campaign"
        repositories = self.root / "repositories"
        campaign.mkdir()
        repositories.mkdir()
        expected = {"counts": {"measured_provisional": 1}}
        with mock.patch("report_pipeline.v4_test_measurement.run", return_value=expected) as run:
            status = cli.main([
                "measure-v4-tests", "--campaign", str(campaign),
                "--repositories", str(repositories), "--output", str(self.root / "out"),
                "--workers", "2", "--timeout", "9", "--setup-timeout", "8",
            ])
        self.assertEqual(0, status)
        run.assert_called_once_with(campaign, repositories.resolve(), self.root / "out",
                                    workers=2, timeout=9, setup_timeout=8,
                                    backend="clone", image_prefix="visual-env-build")

    def test_docker_backend_inspects_head_builds_runs_offline_and_cleans_tags(self):
        output = self.root / "docker-output"
        output.mkdir()
        temporary = self.root / "docker-contexts"
        temporary.mkdir()
        base_commit = "a" * 40
        bundle = {
            "working_directory": "packages/widget",
            "test_command": "npm test -- generated",
            "stable_test_ids": ["STABLE_F2P", "STABLE_P2P"],
            "files": [{"path": "packages/widget/generated.test.js", "operation": "add",
                       "content": "// STABLE_F2P\n// STABLE_P2P\n"}],
        }
        calls = []

        def completed(command, stdout="", stderr="", returncode=0):
            return subprocess.CompletedProcess(command, returncode, stdout, stderr)

        def fake_run(command, **kwargs):
            calls.append(command)
            if command[:3] == ["docker", "image", "inspect"]:
                return completed(command, '[{"Id":"sha256:fixture"}]')
            if "rev-parse" in command:
                return completed(command, base_commit + "\n")
            if command[:3] == ["docker", "image", "tag"]:
                return completed(command, "tagged\n")
            if command[:2] == ["docker", "build"]:
                return completed(command, "build complete\n")
            if command[:3] == ["docker", "image", "rm"]:
                return completed(command, "removed\n")
            if command[:2] == ["docker", "run"]:
                tag = command[-3]
                if "-base-" in tag:
                    return completed(command, "STABLE_F2P FAIL\nSTABLE_P2P PASS\n", returncode=1)
                return completed(command, "STABLE_F2P PASS\nSTABLE_P2P PASS\n")
            self.fail(f"unexpected command: {command}")

        with mock.patch.object(subject, "_run", side_effect=fake_run):
            setup, base, gold = subject._measure_docker_arms(
                "Example__Repo-1", base_commit,
                "diff --git a/a b/a\n--- a/a\n+++ b/a\n",
                bundle, "visual-env-build", temporary, output, 30, 30)

        self.assertEqual("sha256:fixture", setup["base_image"]["image_id"])
        self.assertEqual("fail", base["record"]["tests"][0]["status"])
        self.assertEqual("pass", gold["record"]["tests"][0]["status"])
        head_call = next(call for call in calls if "rev-parse" in call)
        self.assertIn("--network", head_call)
        self.assertEqual("none", head_call[head_call.index("--network") + 1])
        run_calls = [call for call in calls if call[:2] == ["docker", "run"]
                     and "rev-parse" not in call]
        self.assertEqual(2, len(run_calls))
        build_calls = [call for call in calls if call[:2] == ["docker", "build"]]
        self.assertEqual(2, len(build_calls))
        self.assertTrue(all(call[call.index("--network") + 1] == "none"
                            for call in build_calls))
        for call in run_calls:
            self.assertEqual("none", call[call.index("--network") + 1])
            self.assertEqual("/app/packages/widget", call[call.index("--workdir") + 1])
            self.assertEqual(bundle["test_command"], call[-1])
        self.assertEqual(3, len([call for call in calls
                                if call[:3] == ["docker", "image", "rm"]]))
        gold_dockerfile = (temporary / "docker-gold/Dockerfile").read_text()
        self.assertLess(gold_dockerfile.index("git -C /app apply"),
                        gold_dockerfile.index("generated.test.js"))
        self.assertTrue((output / "20_19_02_docker_base_build_stdout.txt").is_file())
        self.assertTrue((output / "20_19_03_gold_stdout.txt").is_file())

    def test_public_cli_allows_docker_without_local_repositories(self):
        campaign = self.root / "campaign-docker"
        campaign.mkdir()
        expected = {"counts": {"measured_provisional": 1}}
        with mock.patch("report_pipeline.v4_test_measurement.run", return_value=expected) as run:
            status = cli.main([
                "measure-v4-tests", "--campaign", str(campaign),
                "--backend", "docker", "--image-prefix", "fixture",
                "--output", str(self.root / "docker-out"),
            ])
        self.assertEqual(0, status)
        run.assert_called_once_with(campaign, None, self.root / "docker-out",
                                    workers=4, timeout=1800, setup_timeout=600,
                                    backend="docker", image_prefix="fixture")


if __name__ == "__main__":
    unittest.main()
