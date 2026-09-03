import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from report_pipeline.stage_orchestration import run


def _plan(stage: str, command: str, output: str = "result.json") -> dict:
    return {
        "schema_version": "pipeline-stage-plan-v1",
        "stage": stage,
        "steps": [{
            "id": "primary",
            "command": command,
            "arguments": ["--output", output],
            "outputs": [output],
        }],
        "metrics": [{"name": "remaining_prs", "path": output,
                     "pointer": "/counts/remaining_prs"}],
    }


class StageOrchestrationTest(unittest.TestCase):
    def test_each_public_stage_accepts_its_internal_family(self):
        cases = {
            "prepare-pr-pool": "filter-merged",
            "recall-and-archive": "probe-linked-issue-media",
            "construct-solver-inputs": "select-solver-inputs",
            "screen-multimodal-candidates": "build-capability-pool",
            "review-visual-gate": "audit-visual-gate-review",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, (stage, command) in enumerate(cases.items()):
                with self.subTest(stage=stage):
                    plan = root / f"plan-{index}.json"
                    plan.write_text(json.dumps(_plan(stage, command)))
                    result = run(stage, plan, root / f"run-{index}")
                    self.assertEqual("planned", result["status"])

    def test_dry_run_validates_allowlist_and_writes_plan_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = root / "plan.json"
            plan.write_text(json.dumps(_plan("prepare-pr-pool", "filter-merged")))
            result = run("prepare-pr-pool", plan, root / "run")
            self.assertEqual("planned", result["status"])
            self.assertEqual("planned", result["steps"][0]["status"])
            self.assertTrue((root / "run" / "stage_manifest.json").is_file())

    def test_cross_stage_command_is_rejected_before_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = root / "plan.json"
            plan.write_text(json.dumps(_plan(
                "prepare-pr-pool", "classify-pr-images")))
            with self.assertRaisesRegex(ValueError, "not allowed"):
                run("prepare-pr-pool", plan, root / "run")

    def test_execute_binds_outputs_logs_and_pr_count(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = root / "plan.json"
            plan.write_text(json.dumps(_plan("prepare-pr-pool", "filter-merged")))

            def runner(command, arguments, cwd):
                self.assertEqual("filter-merged", command)
                (cwd / "result.json").write_text(json.dumps(
                    {"counts": {"remaining_prs": 24}}))
                return subprocess.CompletedProcess([], 0, stdout="ok\n", stderr="")

            result = run("prepare-pr-pool", plan, root / "run", execute=True,
                         cwd=root, command_runner=runner)
            self.assertEqual("complete", result["status"])
            self.assertEqual({"remaining_prs": 24}, result["counts"])
            self.assertEqual("complete", result["steps"][0]["status"])
            self.assertEqual(1, len(result["steps"][0]["outputs"]))
            self.assertTrue((root / "run" / "logs" /
                             "primary.attempt_001.stdout.log").is_file())

    def test_resume_retries_failure_and_preserves_attempts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = root / "plan.json"
            plan.write_text(json.dumps(_plan("prepare-pr-pool", "filter-merged")))

            failed = run(
                "prepare-pr-pool", plan, root / "run", execute=True, cwd=root,
                command_runner=lambda *_: subprocess.CompletedProcess([], 9, "", "network"),
            )
            self.assertEqual("failed", failed["status"])

            def success(command, arguments, cwd):
                (cwd / "result.json").write_text(json.dumps(
                    {"counts": {"remaining_prs": 3}}))
                return subprocess.CompletedProcess([], 0, "ok", "")

            resumed = run(
                "prepare-pr-pool", plan, root / "run", execute=True, resume=True,
                cwd=root, command_runner=success,
            )
            self.assertEqual("complete", resumed["status"])
            self.assertEqual(2, len(resumed["steps"][0]["attempts"]))
            self.assertEqual({"remaining_prs": 3}, resumed["counts"])
            self.assertTrue((root / "run" / "logs" /
                             "primary.attempt_001.stderr.log").is_file())
            self.assertTrue((root / "run" / "logs" /
                             "primary.attempt_002.stdout.log").is_file())

    def test_metric_must_be_bound_to_a_declared_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = root / "plan.json"
            value = _plan("prepare-pr-pool", "filter-merged")
            value["metrics"][0]["path"] = "unbound.json"
            plan.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "declared step output"):
                run("prepare-pr-pool", plan, root / "run")

    def test_resume_rejects_changed_plan(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = root / "plan.json"
            value = _plan("prepare-pr-pool", "filter-merged")
            plan.write_text(json.dumps(value))
            run("prepare-pr-pool", plan, root / "run")
            value["steps"][0]["arguments"].append("--changed")
            plan.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "plan changed"):
                run("prepare-pr-pool", plan, root / "run", execute=True,
                    resume=True, cwd=root)


if __name__ == "__main__":
    unittest.main()
