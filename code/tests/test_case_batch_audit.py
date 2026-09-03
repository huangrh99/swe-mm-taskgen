import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from report_pipeline.case_batch_audit import audit_case, run


class CaseBatchAuditTest(unittest.TestCase):
    def _case(self, root: Path, case_id: str = "owner__repo-1") -> Path:
        case = root / case_id
        evidence = case / "01_visual_review" / "review.json"
        evidence.parent.mkdir(parents=True)
        evidence.write_text("{}\n")
        artifact = {"path": "01_visual_review/review.json",
                    "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
                    "size_bytes": evidence.stat().st_size, "storage": "copied"}
        manifest = {
            "schema_version": "report-case-archive-v1", "case_id": case_id,
            "repository": "owner/repo", "pr_number": 1, "state": "candidate",
            "archived_at": "2026-09-02T00:00:00+00:00",
            "sections": {name: ([artifact] if name == "visual_review" else []) for name in
                         ("visual_review", "source_archive", "test_construction", "measurements", "harbor")},
            "pipeline_status": {"source_archived": "complete", "test_construction": "not_started",
                                "base_gold_measurement": "not_started", "harbor_empty": "not_started",
                                "harbor_oracle": "not_started"},
            "blockers": [], "notes": [],
        }
        (case / "00_case_manifest.json").write_text(json.dumps(manifest))
        return case

    def test_validates_bound_artifacts_and_renders_batch(self):
        Path("tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir="tmp") as value:
            root = Path(value)
            case = self._case(root)
            self.assertTrue(audit_case(case)["valid"])
            result = run(root, [case.name], root / "batch")
            self.assertEqual(1, result["valid_archive_count"])
            self.assertIn(case.name, (root / "batch/00_batch_audit.html").read_text())

    def test_detects_artifact_mutation(self):
        Path("tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir="tmp") as value:
            case = self._case(Path(value))
            (case / "01_visual_review/review.json").write_text("changed")
            result = audit_case(case)
            self.assertFalse(result["valid"])
            self.assertIn("visual_review:0:sha256_mismatch", result["errors"])

    def test_reports_bound_verifier_result(self):
        with tempfile.TemporaryDirectory(dir="tmp") as value:
            root = Path(value)
            case = self._case(root)
            result_path = case / "03_test_construction/20_11_verifier_run_01/20_11_06_result.json"
            result_path.parent.mkdir(parents=True)
            result_path.write_text(json.dumps({"status": "complete", "annotation": {
                "status": "additional_tests_proposed", "test_bundles": [{"bundle_id": "b1"}],
                "human_review_required": True}}))
            manifest_path = case / "00_case_manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["sections"]["test_construction"].append({
                "path": result_path.relative_to(case).as_posix(),
                "sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(),
                "size_bytes": result_path.stat().st_size, "storage": "copied"})
            manifest_path.write_text(json.dumps(manifest))
            verifier = audit_case(case)["verifier_runs"]
            self.assertEqual("additional_tests_proposed", verifier[0]["verdict"])
            self.assertEqual(1, verifier[0]["bundle_count"])

    def test_accepts_generated_verifier_artifact(self):
        with tempfile.TemporaryDirectory(dir="tmp") as value:
            case = self._case(Path(value))
            manifest_path = case / "00_case_manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["sections"]["visual_review"][0]["storage"] = "generated"
            manifest_path.write_text(json.dumps(manifest))
            self.assertTrue(audit_case(case)["valid"])


if __name__ == "__main__":
    unittest.main()
