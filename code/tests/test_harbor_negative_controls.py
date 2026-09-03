import hashlib
import json
import fcntl
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from report_pipeline.harbor_negative_controls import _expected, _make_variant, _material_checksum, run
from report_pipeline.paths import TMP_ROOT


class HarborNegativeControlTests(unittest.TestCase):
    def setUp(self):
        fixtures = Path(__file__).resolve().parent / "fixtures"
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=TMP_ROOT)
        self.root = Path(self.temporary.name)
        source = fixtures / "tasks/carbon-design-system__carbon-20978"
        self.canonical = self.root / "canonical"
        shutil.copytree(source, self.canonical)
        legacy_grader = self.canonical / "tests/verify.py"
        if legacy_grader.is_file():
            legacy_grader.rename(self.canonical / "tests/sweb_grade.py")
        (self.canonical / "tests/test.patch").touch()
        self.variants = self.root / "variants"; self.variants.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def test_skip_is_coherent_but_non_rewarding(self):
        task = _make_variant(self.canonical, self.variants, "skip", "skip")
        manifest_path = task / "tests/test_manifest.json"
        inventory = json.loads((task / "tests/frozen_inventory.json").read_text())
        self.assertFalse(json.loads(manifest_path.read_text())["tests"][0]["enabled"])
        self.assertEqual(inventory["test_manifest_sha256"], hashlib.sha256(manifest_path.read_bytes()).hexdigest())
        integrity_path = task / "tests/integrity_manifest.json"
        integrity = json.loads(integrity_path.read_text())
        self.assertEqual(integrity["files"]["/tests/test_manifest.json"],
                         hashlib.sha256(manifest_path.read_bytes()).hexdigest())
        self.assertIn(hashlib.sha256(integrity_path.read_bytes()).hexdigest(),
                      (task / "tests/test.sh").read_text())

    def test_missing_id_keeps_original_frozen_inventory(self):
        original = json.loads((self.canonical / "tests/frozen_inventory.json").read_text())
        task = _make_variant(self.canonical, self.variants, "missing", "missing_id")
        inventory = json.loads((task / "tests/frozen_inventory.json").read_text())
        manifest = json.loads((task / "tests/test_manifest.json").read_text())
        self.assertEqual(inventory["expected_tests"], original["expected_tests"])
        self.assertEqual(len(manifest["tests"]), len(original["expected_tests"]) - 1)

    def test_tamper_does_not_refresh_embedded_hash(self):
        task = _make_variant(self.canonical, self.variants, "tamper", "tamper")
        actual = hashlib.sha256((task / "tests/test_manifest.json").read_bytes()).hexdigest()
        self.assertNotIn(actual, (task / "tests/test.sh").read_text())

    def test_preservation_adds_explicit_control_assertion(self):
        task = _make_variant(self.canonical, self.variants, "preserve", "preservation")
        manifest = json.loads((task / "tests/test_manifest.json").read_text())
        self.assertEqual(manifest["tests"][-1]["test_id"], "control_preserve_candidate_change")
        self.assertIn("harbor-preservation-sentinel", (task / "solution/solve.sh").read_text())

    def test_resource_variant_constrains_memory_and_verifier_time(self):
        task = _make_variant(self.canonical, self.variants, "resource", "resource")
        config = (task / "task.toml").read_text()
        self.assertIn("memory_mb = 32", config)
        self.assertIn("[verifier]\ntimeout_sec = 0.001", config)

    def test_variant_manifest_binds_current_provisional_parent(self):
        task = _make_variant(self.canonical, self.variants, "baseline", "baseline")
        control = json.loads((task / "control_manifest.json").read_text())
        self.assertEqual(control["control_kind"], "baseline")
        self.assertNotEqual(control["parent_task_material_sha256"], "3d8c9edc45ba73c9952426c757c4b2202a2e46fc5daf9d674b190e8914b65792")

    def test_runtime_integrity_probes_protected_paths(self):
        task = _make_variant(self.canonical, self.variants, "runtime", "runtime_integrity")
        solution = (task / "solution/solve.sh").read_text()
        manifest = json.loads((task / "tests/test_manifest.json").read_text())
        self.assertIn("touch /tests/agent-write-probe", solution)
        self.assertIn("printf x >> /usr/bin/python3", solution)
        self.assertEqual(manifest["tests"][-1]["test_id"], "control_runtime_paths_read_only")

    def test_full_run_uses_provisional_task_and_control_manifests(self):
        harbor = self.root / "harbor"
        harbor.write_text("#!/bin/sh\n")
        run_variants = self.root / "run-variants"
        jobs = self.root / "jobs"
        output = self.root / "result.json"
        expected_tests = json.loads((self.canonical / "tests/frozen_inventory.json").read_text())[
            "expected_tests"]
        detailed_results = []
        for item in expected_tests:
            failing = item["class"] == "F2P"
            detailed_results.append({
                "test_id": item["test_id"],
                "class": item["class"],
                "status": "fail" if failing else "pass",
                "failure_class": "functional_assertion_mismatch" if failing else None,
            })
        trial_result = {"exception_info": None}
        verifier_result = {
            "reward": 0,
            "summary": {"expected": 8, "fail": 4, "pass": 4, "skip": 0, "missing": 0, "error": 0},
            "contract_errors": [],
            "results": detailed_results,
        }
        trial = self.root / "trial"
        (trial / "verifier").mkdir(parents=True)
        (trial / "result.json").write_text(json.dumps(trial_result))
        (trial / "verifier/test_results.json").write_text(json.dumps(verifier_result))
        completed = type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        with patch.dict(os.environ, {"ARK_API_KEY": "must-not-reach-subprocess"}), \
             patch("report_pipeline.harbor_negative_controls.CONTROL_SPECS",
                   (("baseline", "baseline", "nop"),)), \
             patch("report_pipeline.harbor_negative_controls.subprocess.run",
                   return_value=completed) as invoked, \
             patch("report_pipeline.harbor_negative_controls._trial",
                   return_value=(trial, trial_result, verifier_result)):
            result = run(self.canonical, harbor, run_variants, jobs, output)

        self.assertEqual(result["canonical_task_material_sha256"], _material_checksum(self.canonical)[0])
        self.assertEqual(result["controls"]["baseline"]["task_material_sha256"],
                         json.loads((run_variants / "baseline/control_manifest.json").read_text())[
                             "task_material_sha256"])
        self.assertEqual(result["status"], "all_controls_passed")
        self.assertNotIn("ARK_API_KEY", invoked.call_args.kwargs["env"])

    def test_secret_in_command_output_is_never_persisted(self):
        harbor = self.root / "harbor"
        harbor.write_text("#!/bin/sh\n")
        completed = type("Completed", (), {
            "returncode": 1, "stdout": "", "stderr": "sk-12345678901234567890",
        })()
        with patch("report_pipeline.harbor_negative_controls.CONTROL_SPECS",
                   (("baseline", "baseline", "nop"),)), \
             patch("report_pipeline.harbor_negative_controls.subprocess.run",
                   return_value=completed):
            with self.assertRaisesRegex(ValueError, "command_log_secret_detected"):
                run(self.canonical, harbor, self.root / "variants-secret",
                    self.root / "jobs-secret", self.root / "result-secret.json")
        self.assertFalse((self.root / "jobs-secret/baseline_command.log").exists())

    def test_summary_without_matching_detailed_results_is_rejected(self):
        record = {
            "reward": 0,
            "summary": {"expected": 8, "fail": 4, "pass": 4, "skip": 0, "missing": 0, "error": 0},
            "contract_errors": [],
            "results": [],
            "expected_tests": json.loads((self.canonical / "tests/frozen_inventory.json").read_text())[
                "expected_tests"],
            "command_returncode": 0,
            "harbor_exception": None,
            "verifier_reached": True,
        }
        passed, reason = _expected("baseline", record)
        self.assertFalse(passed)
        self.assertIn("detailed result inventory", reason)

    def test_same_checkpoint_cannot_run_concurrently(self):
        harbor = self.root / "harbor"
        harbor.write_text("#!/bin/sh\n")
        output = self.root / "result.json"
        lock = self.root / ".negative-controls.lock"
        descriptor = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, "negative_controls_in_progress"):
                run(self.canonical, harbor, self.root / "run-variants",
                    self.root / "jobs", output)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def test_outputs_must_share_one_non_symlink_root(self):
        harbor = self.root / "harbor"
        harbor.write_text("#!/bin/sh\n")
        outside = self.root / "outside"
        outside.mkdir()
        with self.assertRaisesRegex(ValueError, "must_share_one_root"):
            run(self.canonical, harbor, outside / "variants", self.root / "jobs",
                self.root / "result.json")
        broken = self.root / "result.json"
        broken.symlink_to(self.root / "missing-result.json")
        with self.assertRaisesRegex(ValueError, "output_symlink"):
            run(self.canonical, harbor, self.root / "variants-2", self.root / "jobs-2",
                broken)

    def test_wrong_resource_exception_is_rejected(self):
        record = {
            "reward": None,
            "summary": None,
            "contract_errors": None,
            "results": None,
            "command_returncode": 0,
            "harbor_exception": {"exception_type": "DockerError", "exception_message": "daemon stopped"},
            "verifier_reached": False,
        }
        passed, _ = _expected("resource", record)
        self.assertFalse(passed)


if __name__ == "__main__":
    unittest.main()
