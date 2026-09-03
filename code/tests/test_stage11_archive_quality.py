import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from analysis.scripts.step_11_03_audit_source_archives import run
from report_pipeline.paths import TMP_ROOT


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Stage11ArchiveQualityTests(unittest.TestCase):
    def _archive(self, root, asset_status="complete", asset_reason=None):
        archive = root / "archive"
        archive.mkdir()
        source = archive / "11_source_prs.jsonl"
        source.write_text('{"repo":"o/r","number":1}\n')
        asset = {"url": "https://example.com/a.png", "status": asset_status,
                 "sha256": "same-content" if asset_status == "complete" else None}
        if asset_reason:
            asset["reason"] = asset_reason
        record = {
            "repo": "o/r", "number": 1, "status": "complete",
            "sections": {
                "pull_request": {"status": "complete"},
                "assets": {"status": "complete", "items": [asset, dict(asset)]},
            },
        }
        record_path = archive / "11_record_0001.json"
        record_path.write_text(json.dumps(record))
        manifest = {
            "status": "complete", "pr_ids": ["o/r#1"], "records": 1,
            "source_sha256": sha(source), "files": {record_path.name: sha(record_path)},
        }
        (archive / "11_manifest.json").write_text(json.dumps(manifest))
        return archive

    def test_duplicate_content_is_normalized_not_rejected(self):
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            archive = self._archive(root)
            result = run([archive], root / "quality.json")
            self.assertEqual("complete", result["status"])
            row = result["archives"][0]["rows"][0]
            self.assertEqual("ready_for_image_verifier", row["automatic_decision"])
            self.assertEqual(1, row["duplicate_content_groups"])
            self.assertFalse(row["semantic_rejection"])

    def test_transient_media_failure_is_retry_not_semantic_rejection(self):
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            archive = self._archive(root, "error", "TimeoutError")
            result = run([archive], root / "quality.json")
            self.assertEqual("partial", result["status"])
            row = result["archives"][0]["rows"][0]
            self.assertEqual("retry_required", row["automatic_decision"])
            self.assertFalse(row["semantic_rejection"])

    def test_unverified_optional_reference_failure_does_not_block_image_verifier(self):
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            archive = self._archive(root)
            record_path = archive / "11_record_0001.json"
            record = json.loads(record_path.read_text())
            record["status"] = "partial"
            record["sections"]["linked_issues"] = {
                "status": "partial",
                "items": [{
                    "repo": "o/r", "number": 420918,
                    "relationship": "text_reference", "confidence": "unverified",
                    "required_for_source_complete": False,
                    "source_scope": "curator_reference_only",
                    "detail": {"status": "unavailable"},
                    "comments": {"status": "complete"},
                    "labels": {"status": "complete"},
                    "timeline": {"status": "not_required",
                                 "reason": "unverified_text_reference"},
                }],
            }
            record_path.write_text(json.dumps(record))
            manifest_path = archive / "11_manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["status"] = "partial"
            manifest["files"][record_path.name] = sha(record_path)
            manifest_path.write_text(json.dumps(manifest))

            result = run([archive], root / "quality.json")
            row = result["archives"][0]["rows"][0]
            self.assertEqual("ready_for_image_verifier", row["automatic_decision"])
            self.assertEqual(["o/r#420918"], row["optional_reference_failures"])
            self.assertEqual([], row["source_failures"])


if __name__ == "__main__":
    unittest.main()
