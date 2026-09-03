import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from PIL import Image

from report_pipeline import pr_image_roles as subject


class PrImageRoleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.run = self.root / "source"
        assets = self.run / "11_http_archive" / "assets"
        assets.mkdir(parents=True)
        image = assets / "same"
        Image.new("RGB", (12, 8), "red").save(image, format="PNG")
        self.digest = hashlib.sha256(image.read_bytes()).hexdigest()
        self.archive = self.run / "11_record_0001.json"
        value = {
            "schema_version": 1, "instance_id": "o__r-7", "repo": "o/r", "number": 7,
            "status": "complete",
            "sections": {
                "pull_request": {"data": {"html_url": "https://github.com/o/r/pull/7",
                    "title": "fix visual bug", "body": "Before screenshot. Fix uses width 17."}},
                "linked_issues": {"items": [{"number": 6, "data": {
                    "number": 6, "html_url": "https://github.com/o/r/issues/6",
                    "title": "broken layout", "body": "The panel overlaps."}}]},
                "comments": {"items": []},
                "assets": {"items": [
                    {"url": "https://x/pr.png", "sources": ["pr:body"], "status": "complete",
                     "sha256": self.digest, "media_type": "image/png", "local_path": "assets/same"},
                    {"url": "https://x/issue.png", "sources": ["issue:o/r#6:body"],
                     "status": "complete", "sha256": self.digest,
                     "media_type": "image/png", "local_path": "assets/same"},
                    {"url": "https://x/missing.png", "sources": ["comments:1"],
                     "status": "failed", "reason": "HTTP 503"}
                ]}
            },
            "archival_view": {"media": [
                {"url": "https://x/pr.png", "occurrences": [{"source_id": "pr:body"}]},
                {"url": "https://x/issue.png", "occurrences": [{"source_id": "o/r#6:body"}]},
                {"url": "https://x/missing.png", "occurrences": [{"source_id": "comments:1"}]}
            ]}
        }
        self.archive.write_text(json.dumps(value))

    def tearDown(self):
        self.temporary.cleanup()

    def annotation(self, packet):
        first, missing = [item["asset_id"] for item in packet["assets"]]
        return {
            "schema_version": "pr-image-role-leakage-v1", "case_id": "o__r-7",
            "images": [
                {"asset_id": first, "observed": True, "role": "before_only",
                 "role_evidence": "Issue and PR identify the broken state",
                 "shows_actual_bug": "yes", "contains_fixed_after": "no",
                 "contains_solution_evidence": "no", "task_relationship": "explicit",
                 "agent_visibility_recommendation": "recommend_before_candidate",
                 "crop": {"needed": False, "feasible": "no", "normalized_box": None,
                          "reason": "not a composite"},
                 "requires_human_review": True, "reason": "candidate only",
                 "confidence": "high"},
                {"asset_id": missing, "observed": False, "role": "unclear",
                 "role_evidence": "pixels unavailable", "shows_actual_bug": "unknown",
                 "contains_fixed_after": "unknown", "contains_solution_evidence": "unknown",
                 "task_relationship": "unknown",
                 "agent_visibility_recommendation": "retry_or_video_review",
                 "crop": {"needed": False, "feasible": "unknown", "normalized_box": None,
                          "reason": "unavailable"},
                 "requires_human_review": True, "reason": "retry independently",
                 "confidence": "low"}
            ],
            "source_path_recommendation": "issue_derived",
            "before_candidate_asset_ids": [first], "curator_only_asset_ids": [],
            "crop_review_asset_ids": [], "retry_asset_ids": [missing],
            "video_review_asset_ids": [],
            "problem_statement_action": "use_issue_text", "leakage_summary": "PR prose is curator-only",
            "limitations": ["one failed download"]
        }

    def test_packet_normalizes_same_bytes_and_retains_all_origins(self):
        packet, images = subject.build_packet(self.archive, self.root / "inputs")
        self.assertEqual(2, len(packet["assets"]))
        duplicate = packet["assets"][0]
        self.assertEqual(2, duplicate["normalized_duplicate_count"])
        self.assertEqual({"issue", "pr"}, set(duplicate["origin_kinds"]))
        self.assertEqual(self.digest, duplicate["asset_id"])
        self.assertEqual(1, len(images))
        subject.validate(self.annotation(packet), packet)

    def test_after_image_cannot_be_recommended_as_before(self):
        packet, _ = subject.build_packet(self.archive, self.root / "inputs")
        annotation = self.annotation(packet)
        annotation["images"][0]["role"] = "after_only"
        with self.assertRaisesRegex(ValueError, "before_policy"):
            subject.validate(annotation, packet)

    def test_failed_download_is_not_a_semantic_exclusion(self):
        packet, _ = subject.build_packet(self.archive, self.root / "inputs")
        annotation = self.annotation(packet)
        annotation["images"][1]["agent_visibility_recommendation"] = "exclude"
        annotation["retry_asset_ids"] = []
        annotation["curator_only_asset_ids"] = [packet["assets"][1]["asset_id"]]
        with self.assertRaisesRegex(ValueError, "unattached_requires_retry"):
            subject.validate(annotation, packet)

    def test_run_prepare_only_writes_auditable_html(self):
        output = self.root / "output"
        result = subject.run(archives=[self.archive], archive_manifests=[], output=output)
        self.assertEqual({"complete": 0, "prepared": 1, "failed": 0}, result["counts"])
        self.assertTrue(Path(result["audit_html"]).is_file())
        manifest = json.loads((output / "08_04_03_results.json").read_text())
        self.assertFalse(manifest["model_invoked"])
        self.assertTrue(manifest["boundary"]["verifier_cannot_approve_formal_task"])
        subject.validate_run(output)

    def test_no_attached_still_image_is_routed_by_code_without_api_call(self):
        value = json.loads(self.archive.read_text())
        value["sections"]["assets"]["items"] = []
        value["archival_view"]["media"] = []
        self.archive.write_text(json.dumps(value))

        class Evaluator:
            backend = "gemini"
            profile = {"model": "fixture", "protocol": "chat", "endpoint": "fixture"}
            accepted_response_models = ["fixture"]
            max_tokens = 100
            attempts = 1
            credential_identity = {"source_kind": "test_fixture", "key_name": "FIXTURE",
                                   "fingerprint": "3" * 64}
            calls = 0

            def __call__(inner, **_kwargs):
                inner.calls += 1
                raise AssertionError("unobservable case must not call provider")

        evaluator = Evaluator()
        output = self.root / "unobservable-output"
        result = subject.run(archives=[self.archive], archive_manifests=[], output=output,
                             evaluator=evaluator)
        self.assertEqual(0, evaluator.calls)
        self.assertEqual("deterministic_unobservable", result["records"][0]["decision_method"])
        self.assertEqual("no_candidate",
                         result["records"][0]["annotation"]["source_path_recommendation"])
        subject.validate_run(output)

    def test_video_contact_sheet_is_attached_and_hash_bound(self):
        value = json.loads(self.archive.read_text())
        video = self.run / "11_http_archive" / "assets" / "video"
        video.write_bytes(b"fixture-video")
        video_digest = hashlib.sha256(video.read_bytes()).hexdigest()
        value["sections"]["assets"]["items"] = [{
            "url": "https://x/before.mp4", "sources": ["pr:body"],
            "status": "complete", "sha256": video_digest,
            "media_type": "video/mp4", "local_path": "assets/video",
        }]
        value["archival_view"]["media"] = [{
            "url": "https://x/before.mp4",
            "occurrences": [{"source_id": "pr:body"}],
        }]
        self.archive.write_text(json.dumps(value))

        def prepare(_source, target):
            target.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (24, 16), "blue").save(target)
            return target, {
                "kind": "video_contact_sheet", "source_sha256": video_digest,
                "derived_sha256": subject._sha(target), "duration_seconds": 3.0,
                "sampled_timestamps_seconds": [0, .5, 1, 1.5, 2, 2.5],
                "layout": {"order": "left_to_right_top_to_bottom", "columns": 3,
                           "rows": 2},
                "ffmpeg_sha256": "f" * 64, "ffmpeg_version": "fixture",
            }

        with mock.patch.object(subject, "_prepare_video_contact_sheet", prepare):
            packet, images = subject.build_packet(self.archive, self.root / "inputs")
        self.assertEqual(1, len(images))
        self.assertEqual("video_contact_sheet",
                         packet["assets"][0]["model_input_representation"]["kind"])
        self.assertEqual(1, packet["assets"][0]["attachment_index"])

    def test_temporal_before_sequence_can_be_recommended(self):
        packet, _ = subject.build_packet(self.archive, self.root / "inputs")
        annotation = self.annotation(packet)
        annotation["images"][0]["role"] = "temporal_sequence"
        subject.validate(annotation, packet)

    def test_duplicate_pr_across_manifests_is_rejected(self):
        manifest = self.run / "11_manifest.json"
        manifest.write_text(json.dumps({"files": {self.archive.name: subject._sha(self.archive)}}))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            subject._archive_paths([self.archive], [manifest])

    def test_stage11_orchestration_is_a_single_hash_bound_input(self):
        manifest = self.run / "11_manifest.json"
        manifest.write_text(json.dumps({"files": {
            self.archive.name: subject._sha(self.archive)}}))
        quality = self.root / "11_03_archive_quality.json"
        quality.write_text("{}")
        orchestration = self.root / "11_02_manifest.json"
        orchestration.write_text(json.dumps({
            "schema_version": "selected-candidate-stage11-waves-v1",
            "quality_audit": {"path": str(quality), "sha256": subject._sha(quality)},
            "previous_runs": [{"path": str(self.run),
                               "manifest_sha256": subject._sha(manifest)}],
            "waves": [],
        }))
        self.assertEqual([self.archive.resolve()],
                         subject._archive_paths([], [], [orchestration]))

    def test_model_validation_failure_gets_one_semantic_retry(self):
        class Evaluator:
            backend = "gemini"
            profile = {"model": "fixture", "protocol": "chat", "endpoint": "fixture"}
            accepted_response_models = ["fixture"]
            max_tokens = 100
            attempts = 1
            credential_identity = {"source_kind": "test_fixture", "key_name": "FIXTURE",
                                   "fingerprint": "1" * 64}
            calls = 0

            def __call__(inner, **kwargs):
                inner.calls += 1
                work = Path(kwargs["workdir"])
                for name in ("10_api_invocation.json", "10_attempt_01.json",
                             "10_provider_response_01.json"):
                    (work / name).write_text("{}")
                packet = kwargs["packet"]
                annotation = self.annotation(packet)
                if inner.calls == 1:
                    annotation["unexpected"] = True
                (work / "09_model_raw.json").write_text(json.dumps(annotation))
                return annotation, {"backend": "gemini", "model": "fixture",
                                    "requested_model": "fixture", "attempts": 1}

        output = self.root / "retry-output"
        evaluator = Evaluator()
        result = subject.run(archives=[self.archive], archive_manifests=[], output=output,
                             evaluator=evaluator)
        self.assertEqual(2, evaluator.calls)
        self.assertEqual(1, result["counts"]["complete"])
        invocation = json.loads((output / "08_04_03_results.json").read_text())[
            "records"][0]["invocation"]
        self.assertEqual(2, invocation["semantic_validation_attempts"])
        self.assertEqual(1, len(invocation["prior_validation_failures"]))
        subject.validate_run(output)

    def test_provider_failure_is_recorded_and_retried_without_stopping_batch(self):
        class Evaluator:
            backend = "gemini"
            profile = {"model": "fixture", "protocol": "chat", "endpoint": "fixture"}
            accepted_response_models = ["fixture"]
            max_tokens = 100
            attempts = 1
            credential_identity = {"source_kind": "test_fixture", "key_name": "FIXTURE",
                                   "fingerprint": "4" * 64}
            calls = 0

            def __call__(inner, **kwargs):
                inner.calls += 1
                work = Path(kwargs["workdir"])
                (work / "10_api_invocation.json").write_text("{}")
                if inner.calls == 1:
                    raise TimeoutError("fixture timeout")
                annotation = self.annotation(kwargs["packet"])
                (work / "09_model_raw.json").write_text(json.dumps(annotation))
                return annotation, {"backend": "gemini", "model": "fixture",
                                    "requested_model": "fixture", "attempts": 1}

        output = self.root / "provider-retry-output"
        evaluator = Evaluator()
        result = subject.run(archives=[self.archive], archive_manifests=[], output=output,
                             evaluator=evaluator)
        self.assertEqual(2, evaluator.calls)
        invocation = result["records"][0]["invocation"]
        self.assertEqual([{"attempt": 1, "error_type": "TimeoutError",
                           "status_code": None}], invocation["prior_provider_failures"])
        self.assertIn("o__r-7", result["checkpoints"])
        subject.validate_run(output)

    def test_tampered_asset_is_a_retryable_case_failure_and_batch_continues(self):
        corrupt_run = self.root / "corrupt-source"
        corrupt_asset = corrupt_run / "11_http_archive/assets/corrupt"
        corrupt_asset.parent.mkdir(parents=True)
        corrupt_asset.write_bytes(b"not an image")
        corrupt_archive = corrupt_run / "11_record_0001.json"
        value = json.loads(self.archive.read_text())
        value["instance_id"] = "o__r-8"
        value["number"] = 8
        value["sections"]["assets"]["items"] = [{
            "url": "https://x/corrupt.png", "sources": ["issue:o/r#6:body"],
            "status": "complete", "sha256": "0" * 64, "media_type": "image/png",
            "local_path": "assets/corrupt",
        }]
        value["archival_view"]["media"] = [{
            "url": "https://x/corrupt.png",
            "occurrences": [{"source_id": "o/r#6:body"}],
        }]
        corrupt_archive.write_text(json.dumps(value))

        output = self.root / "corrupt-output"
        result = subject.run(
            archives=[corrupt_archive, self.archive], archive_manifests=[], output=output)

        self.assertEqual({"complete": 0, "prepared": 1, "failed": 1}, result["counts"])
        failed, prepared = result["records"]
        self.assertEqual("input_preparation", failed["failure_class"])
        self.assertEqual("technical_failure", failed["decision_method"])
        self.assertIsNone(failed["packet"])
        self.assertEqual("prepared", prepared["status"])
        self.assertIn("o__r-8", result["checkpoints"])
        self.assertIn("o__r-7", result["checkpoints"])
        subject.validate_run(output)

    def test_unexpected_interruption_preserves_completed_checkpoints(self):
        output = self.root / "interrupted-output"
        original = subject._render
        try:
            def interrupt(*_args, **_kwargs):
                raise KeyboardInterrupt("fixture stop")
            subject._render = interrupt
            with self.assertRaises(KeyboardInterrupt):
                subject.run(archives=[self.archive], archive_manifests=[], output=output)
        finally:
            subject._render = original

        self.assertTrue((output / "08_04_03_checkpoints/case_0001.json").is_file())
        interruption = json.loads((output / "08_04_99_interrupted.json").read_text())
        self.assertEqual("KeyboardInterrupt", interruption["error_type"])
        self.assertEqual(1, interruption["completed_checkpoint_count"])

    def test_three_invalid_semantic_attempts_are_bound_as_semantic_failure(self):
        class Evaluator:
            backend = "gemini"
            profile = {"model": "fixture", "protocol": "chat", "endpoint": "fixture"}
            accepted_response_models = ["fixture"]
            max_tokens = 100
            attempts = 1
            credential_identity = {"source_kind": "test_fixture", "key_name": "FIXTURE",
                                   "fingerprint": "2" * 64}
            calls = 0

            def __call__(inner, **kwargs):
                inner.calls += 1
                work = Path(kwargs["workdir"])
                annotation = self.annotation(kwargs["packet"])
                annotation["unexpected"] = True
                (work / "09_model_raw.json").write_text(json.dumps(annotation))
                return annotation, {"backend": "gemini", "model": "fixture",
                                    "requested_model": "fixture", "attempts": 1}

        output = self.root / "three-invalid-output"
        evaluator = Evaluator()
        result = subject.run(archives=[self.archive], archive_manifests=[], output=output,
                             evaluator=evaluator)
        self.assertEqual(3, evaluator.calls)
        self.assertEqual(1, result["counts"]["failed"])
        record = result["records"][0]
        self.assertEqual("semantic_validation", record["failure_class"])
        self.assertEqual(3, record["invocation"]["semantic_validation_attempts"])
        self.assertEqual(3, len(record["invocation"]["semantic_attempt_records"]))
        subject.validate_run(output)

    def test_audit_rejects_changed_packet_even_when_result_exists(self):
        output = self.root / "tamper-output"
        subject.run(archives=[self.archive], archive_manifests=[], output=output)
        packet = output / "08_04_01_packets/case_0001/08_04_01_packet.json"
        packet.write_text(packet.read_text() + " ")
        with self.assertRaisesRegex(ValueError, "packet binding"):
            subject.validate_run(output)


if __name__ == "__main__":
    unittest.main()
