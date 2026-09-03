import json
import tempfile
import unittest
from pathlib import Path

from report_pipeline.harbor_controls import audit
from report_pipeline.harbor_negative_controls import CONTROL_SPECS
from report_pipeline.paths import TMP_ROOT
from report_pipeline.workflow import _portable, _sha256, _task_inventory


class HarborControlAuditTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, Path, Path, Path]:
        task = root / "task"
        (task / "tests").mkdir(parents=True)
        tests = [
            {"test_id": "f", "class": "F2P"},
            {"test_id": "p", "class": "P2P"},
        ]
        (task / "tests/config.json").write_text(json.dumps({
            "instance_id": "owner__repo-1", "FAIL_TO_PASS": ["f"], "PASS_TO_PASS": ["p"]
        }))
        (task / "tests/test_manifest.json").write_text(json.dumps({"tests": tests}))
        (task / "instruction.md").write_text("fix it")
        task_sha, _ = _task_inventory(task)
        sidecar = root / "task.export_manifest.json"
        sidecar.write_text(json.dumps({
            "schema_version": "visual-harbor-export-v1",
            "task_material_sha256": task_sha,
        }))
        (root / "task.export_manifest.json.commit.json").write_text(json.dumps({
            "schema_version": "visual-harbor-export-commit-v1",
            "task_material_sha256": task_sha,
            "sidecar_sha256": _sha256(sidecar),
            "transaction_sha256": "0" * 64,
        }))

        def job(name: str, oracle: bool) -> Path:
            job_root = root / name
            trial = job_root / "trial"
            (trial / "verifier").mkdir(parents=True)
            (job_root / "result.json").write_text("{}")
            agent = "oracle" if oracle else "nop"
            reward = 1 if oracle else 0
            (trial / "result.json").write_text(json.dumps({
                "exception_info": None, "task_checksum": "b" * 64,
                "agent_info": {"name": agent},
                "config": {"task": {"path": _portable(task)}},
                "verifier_result": {"rewards": {"reward": reward}},
            }))
            rows = [
                {"test_id": "f", "class": "F2P", "status": "pass" if oracle else "fail",
                 "failure_class": None if oracle else "functional_assertion_mismatch"},
                {"test_id": "p", "class": "P2P", "status": "pass", "failure_class": None},
            ]
            summary = {state: sum(row["status"] == state for row in rows)
                       for state in ("pass", "fail", "skip", "missing", "error")}
            (trial / "verifier/test_results.json").write_text(json.dumps({
                "reward": reward, "summary": summary, "results": rows,
                "contract_errors": [],
            }))
            return job_root

        negative = root / "negative-controls.json"
        negative.write_text(json.dumps({
            "schema_version": "visual-harbor-negative-controls-v1",
            "status": "all_controls_passed",
            "canonical_task_material_sha256": task_sha,
            "completed_controls": len(CONTROL_SPECS),
            "controls": {kind: {"control_passed": True}
                         for _name, kind, _agent in CONTROL_SPECS},
        }))
        return task, job("nop", False), job("oracle", True), negative

    def test_audits_formal_task_without_agent_visible_export_manifest(self):
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as temporary:
            root = Path(temporary)
            task, nop, oracle, negative = self._fixture(root)
            result = audit(task, nop, oracle, root / "audit.json", "simulation", negative)
            self.assertEqual(result["schema_version"], "pipeline-harbor-controls-v1")
            self.assertEqual(result["instance_id"], "owner__repo-1")
            self.assertEqual(result["harbor_task_checksum"], "b" * 64)
            self.assertEqual([item["role"] for item in result["runs"]],
                             ["baseline_nop", "oracle"])

    def test_rejects_detail_summary_disagreement(self):
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as temporary:
            root = Path(temporary)
            task, nop, oracle, negative = self._fixture(root)
            details = nop / "trial/verifier/test_results.json"
            value = json.loads(details.read_text())
            value["summary"]["fail"] = 0
            details.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "verifier_summary_mismatch"):
                audit(task, nop, oracle, root / "audit.json", "simulation", negative)

    def test_rejects_harbor_exception_even_when_reward_matches(self):
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as temporary:
            root = Path(temporary)
            task, nop, oracle, negative = self._fixture(root)
            (oracle / "trial/result.json").write_text(json.dumps({
                "exception_info": {"exception_type": "InjectedError"}
            }))
            with self.assertRaisesRegex(ValueError, "raw Harbor control semantics"):
                audit(task, nop, oracle, root / "audit.json", "simulation", negative)


if __name__ == "__main__":
    unittest.main()
