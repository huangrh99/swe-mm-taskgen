import json
import shutil
import tempfile
import unittest
from pathlib import Path

from report_pipeline.harbor_measurement import build
from report_pipeline.paths import TMP_ROOT, WORKSPACE_ROOT
from report_pipeline.workflow import _sha256


class HarborMeasurementTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="measurement-producer-", dir=TMP_ROOT))
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.task = self.root / "task"
        for name in ("tests", "environment"):
            (self.task / name).mkdir(parents=True)
        (self.task / "tests/config.json").write_text(json.dumps({
            "FAIL_TO_PASS": ["f"], "PASS_TO_PASS": ["p"]}) + "\n")
        (self.task / "tests/test_manifest.json").write_text(json.dumps({
            "tests": [{"test_id": "f", "class": "F2P"},
                      {"test_id": "p", "class": "P2P"}]}) + "\n")
        (self.task / "tests/test.sh").write_text("#!/bin/sh\n")
        (self.task / "environment/base_image.json").write_text(json.dumps({
            "image_id": "sha256:" + "a" * 64}) + "\n")
        self.dossier = self.root / "dossier.json"
        self.dossier.write_text(json.dumps({
            "candidate_id": "owner__repo-1", "repository": "owner/repo",
            "git": {"baseline_sha": "b" * 40, "reference_sha": "c" * 40}}) + "\n")

    def _oracle_quality(self) -> Path:
        negative = self.root / "negative-result.json"
        positive = self.root / "equivalent-result.json"
        negative.write_text('{"reward":0}\n')
        positive.write_text('{"reward":1}\n')
        quality = self.root / "oracle-quality.json"
        quality.write_text(json.dumps({
            "schema_version": "oracle-quality-validation-v1",
            "instance_id": "owner__repo-1",
            "test_manifest_sha256": _sha256(self.task / "tests/test_manifest.json"),
            "status": "passed", "curator_only": True, "solver_visible": False,
            "negative_variants": [{
                "variant_id": "keeps-defect", "patch_sha256": "e" * 64,
                "result": {"path": str(negative), "sha256": _sha256(negative)},
                "expected_reward": 0, "observed_reward": 0,
                "failed_test_ids": ["f"],
            }],
            "equivalent_positive_variant": {
                "variant_id": "alternative-fix", "patch_sha256": "f" * 64,
                "result": {"path": str(positive), "sha256": _sha256(positive)},
                "expected_reward": 1, "observed_reward": 1,
                "passed_test_ids": ["f", "p"],
            },
        }) + "\n")
        return quality

    def _trial(self, side: str, index: int) -> Path:
        trial = self.root / f"{side}-{index}"
        (trial / "verifier").mkdir(parents=True)
        result = trial / "result.json"
        result.write_text(json.dumps({
            "id": f"{side}-{index}", "started_at": "2026-09-01T00:00:00Z",
            "finished_at": "2026-09-01T00:01:00Z", "exception_info": None,
            "task_checksum": "d" * 64,
            "agent_info": {"name": "nop" if side == "baseline" else "oracle"},
            "config": {"job_id": f"job-{side}", "task": {"path": self.task.resolve().relative_to(WORKSPACE_ROOT).as_posix()},
                       "environment": {"type": "docker"}},
            "verifier_result": {"rewards": {"reward": 0 if side == "baseline" else 1}},
        }) + "\n")
        statuses = ["fail", "pass"] if side == "baseline" else ["pass", "pass"]
        reward = int(all(status == "pass" for status in statuses))
        (trial / "verifier/test_results.json").write_text(json.dumps({
            "reward": reward,
            "results": [{"test_id": "f", "class": "F2P", "status": statuses[0]},
                        {"test_id": "p", "class": "P2P", "status": statuses[1]}],
            "summary": {"pass": statuses.count("pass"), "fail": statuses.count("fail"),
                        "skip": 0, "missing": 0, "error": 0},
            "contract_errors": [],
        }) + "\n")
        return result

    def test_produces_two_repeated_bound_runs_per_side(self):
        baseline = [self._trial("baseline", index) for index in (1, 2)]
        reference = [self._trial("reference", index) for index in (1, 2)]
        output = self.root / "measurement"
        result = build(self.task, self.dossier, baseline, reference,
                       self._oracle_quality(), output)
        self.assertEqual(result["baseline_run_count"], 2)
        self.assertEqual(result["reference_run_count"], 2)
        measurement = json.loads((output / "measurement.json").read_text())
        self.assertEqual(len(measurement["baseline_runs"]), 2)
        self.assertEqual(len(measurement["reference_runs"]), 2)
        side = json.loads((output / "baseline_01.json").read_text())
        self.assertEqual(side["command"], ["/tests/test.sh"])
        self.assertEqual(side["agent"], "nop")
        self.assertEqual(side["native_task_checksum"], "d" * 64)
        self.assertEqual(side["harbor_result"]["sha256"], _sha256(baseline[0]))
        self.assertEqual(measurement["oracle_quality_validation"]["sha256"],
                         _sha256(self._oracle_quality()))

    def test_rejects_wrong_execution_role(self):
        baseline = [self._trial("baseline", index) for index in (1, 2)]
        reference = [self._trial("reference", index) for index in (1, 2)]
        value = json.loads(baseline[0].read_text())
        value["agent_info"]["name"] = "model-agent"
        baseline[0].write_text(json.dumps(value) + "\n")
        with self.assertRaisesRegex(ValueError, "trial_semantics_invalid"):
            build(self.task, self.dossier, baseline, reference,
                  self._oracle_quality(), self.root / "bad-measurement")

    def test_rejects_equivalent_variant_that_does_not_pass_every_frozen_test(self):
        quality = self._oracle_quality()
        value = json.loads(quality.read_text())
        value["equivalent_positive_variant"]["passed_test_ids"] = ["f"]
        quality.write_text(json.dumps(value) + "\n")
        baseline = [self._trial("baseline", index) for index in (1, 2)]
        reference = [self._trial("reference", index) for index in (1, 2)]
        with self.assertRaisesRegex(ValueError, "did_not_pass_all_tests"):
            build(self.task, self.dossier, baseline, reference, quality,
                  self.root / "bad-oracle-quality")


if __name__ == "__main__":
    unittest.main()
