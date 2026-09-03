import base64
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

import jsonschema

from report_pipeline import pre_review_classification as subject


class ChangeScaleTests(unittest.TestCase):
    def test_video_review_is_not_a_resumable_final_status(self):
        self.assertEqual(
            {"complete", "ineligible"},
            set(subject.RESUMABLE_FINAL_CAPABILITY_STATUSES),
        )
        self.assertNotIn(
            "requires_video_review",
            subject.RESUMABLE_FINAL_CAPABILITY_STATUSES,
        )

    def test_small_change_excludes_tests_and_lockfiles_but_keeps_raw_counts(self):
        result = subject.classify_change_scale([
            {"filename": "src/widget.ts", "status": "modified", "additions": 30, "deletions": 10},
            {"filename": "src/widget.test.ts", "status": "modified", "additions": 80, "deletions": 0},
            {"filename": "package-lock.json", "status": "modified", "additions": 500, "deletions": 500},
        ])
        self.assertEqual("小规模修改", result["label"])
        self.assertEqual(1, result["cleaned_source_file_count"])
        self.assertEqual(40, result["cleaned_changed_lines"])
        self.assertEqual(3, result["raw_changed_file_count"])
        self.assertEqual(1120, result["raw_changed_lines"])
        self.assertEqual(["test_code", "lockfile"],
                         [row["exclusion_reason"] for row in result["excluded_files"]])

    def test_medium_and_large_thresholds_are_exact(self):
        medium = subject.classify_change_scale([
            {"filename": "a.ts", "additions": 25, "deletions": 0},
            {"filename": "b.scss", "additions": 25, "deletions": 0},
            {"filename": "c.tsx", "additions": 25, "deletions": 0},
            {"filename": "d.js", "additions": 25, "deletions": 0},
        ])
        self.assertEqual("中规模修改", medium["label"])
        large_lines = subject.classify_change_scale([
            {"filename": "a.py", "additions": 101, "deletions": 0},
        ])
        self.assertEqual("大规模修改", large_lines["label"])
        large_files = subject.classify_change_scale([
            {"filename": f"src/{name}.go", "additions": 1, "deletions": 0}
            for name in "abcde"
        ])
        self.assertEqual("大规模修改", large_files["label"])

    def test_no_production_source_is_unresolved(self):
        result = subject.classify_change_scale([
            {"filename": "tests/a.test.js", "additions": 2, "deletions": 0},
        ])
        self.assertEqual("无法分类", result["label"])
        self.assertTrue(result["human_review_required"])

    def test_detectable_whitespace_only_source_change_is_excluded(self):
        result = subject.classify_change_scale([
            {"filename": "src/a.js", "additions": 1, "deletions": 1,
             "patch": "@@ -1 +1 @@\n-const x = 1;\n+const   x=1;"},
            {"filename": "src/b.js", "additions": 2, "deletions": 0,
             "patch": "@@ -1 +1,3 @@\n const y = 2;\n+use(y);\n+done();"},
        ])
        self.assertEqual("小规模修改", result["label"])
        self.assertEqual(["detected_whitespace_only_change"],
                         [row["exclusion_reason"] for row in result["excluded_files"]])


class PromptContractTests(unittest.TestCase):
    def test_duplicate_sha_records_bind_the_exact_solver_asset_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_root = root / "archive"
            media_root = archive_root / "11_http_archive/assets"
            media_root.mkdir(parents=True)
            first = media_root / "first.png"
            second = media_root / "second.png"
            first.write_bytes(b"same-image")
            second.write_bytes(b"same-image")
            asset_id = subject._sha(first)
            archive = {"sections": {"assets": {"items": [
                {"sha256": asset_id, "status": "complete",
                 "local_path": "assets/first.png"},
                {"sha256": asset_id, "status": "complete",
                 "local_path": "assets/second.png"},
            ]}}}
            resolved = subject._bound_media_path(
                {"asset_id": asset_id, "local_path": str(second.resolve())},
                archive_root / "11_record.json",
                archive,
            )
            self.assertEqual(second.resolve(), resolved)

    def test_authorization_excludes_source_media_binding_errors_without_aborting_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_root = root / "archive"
            media_root = archive_root / "11_http_archive/assets"
            media_root.mkdir(parents=True)
            media = media_root / "image.png"
            media.write_bytes(b"image")
            asset_id = subject._sha(media)
            archive_path = archive_root / "11_record.json"
            archive_path.write_text(json.dumps({"sections": {"assets": {"items": []}}}))
            row = {
                "case_id": "owner__repo-1",
                "result_sha256": "a" * 64,
                "packet_sha256": "b" * 64,
                "packet": {"provenance": {"source_archive": str(archive_path)}},
                "human_seed": {"problem_statement": "visual defect"},
                "assets": [{"asset_id": asset_id, "status": "available",
                            "local_path": str(media.resolve())}],
            }
            binding = subject._case_authorization_bindings([row])[0]
            self.assertFalse(binding["eligible_for_model_call"])
            self.assertEqual("invalid", binding["source_media_binding_status"])
            self.assertIn("matches=0", binding["source_media_binding_error"])

    def test_evaluator_contract_binds_credential_fingerprint_without_secret(self):
        from pr_crawler.api_engines import ApiEvaluator
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.dict("os.environ", {"AIDP_API_KEY": ""}, clear=False):
            root = Path(directory)
            first = root / "first.env"
            second = root / "second.env"
            first.write_text("AIDP_API_KEY=fixture-secret-one\n")
            second.write_text("AIDP_API_KEY=fixture-secret-two\n")
            one = subject._evaluator_contract(ApiEvaluator("gemini", key_file=first))
            two = subject._evaluator_contract(ApiEvaluator("gemini", key_file=second))
            self.assertNotEqual(one["credential_identity"]["fingerprint"],
                                two["credential_identity"]["fingerprint"])
            serialized = json.dumps(one)
            self.assertNotIn("fixture-secret-one", serialized)
            self.assertNotIn(str(first), serialized)

    def test_resume_contract_must_match_current_provider_authorization(self):
        prior = {"visual_capability": {"status": "complete", "invocation": {
            "evaluator_contract": {"backend": "k3", "requested_model": "k3"}}}}
        self.assertFalse(subject._resume_contract_compatible(
            prior, run_model=True,
            evaluator_contract={"backend": "gemini", "requested_model": "gemini"}))
        self.assertTrue(subject._resume_contract_compatible(
            prior, run_model=True,
            evaluator_contract={"backend": "k3", "requested_model": "k3"}))
        self.assertTrue(subject._resume_contract_compatible(
            prior, run_model=False, evaluator_contract=None))

    def test_authorization_identity_rejects_expired_or_noncanonical_output(self):
        identity = {"run_id": "expired-run", "nonce": "expired_nonce_0001",
                    "expires_at": "2020-01-01T00:00:00Z"}
        with self.assertRaisesRegex(ValueError, "authorization_expired"):
            subject._validate_authorization_identity(identity, subject.RUNS_ROOT / "expired")
        identity["expires_at"] = "2099-01-01T00:00:00Z"
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "output_outside_runs"):
                subject._validate_authorization_identity(identity, Path(directory))

    @staticmethod
    def _copy_frozen_classification(root):
        from analysis.scripts.step_16_04_export_human_review import (
            candidate_problem_statement, source_archive_documents)
        root.mkdir(parents=True, exist_ok=True)
        source_run = root / "source_run"
        source_run.mkdir()
        archive_root = root / "archive"
        media_root = archive_root / "11_http_archive/assets"
        media_root.mkdir(parents=True)
        media = media_root / "asset"
        media.write_bytes(b"fixture-image")
        asset_id = subject._sha(media)
        issue_title = "owner/repo#2:title"
        issue_body = "owner/repo#2:body"
        archive_path = archive_root / "11_record.json"
        archive = {
            "repo": "owner/repo", "number": 1, "instance_id": "owner__repo-1",
            "status": "complete",
            "sections": {
                "files": {"items": [{"filename": "src/a.js", "status": "modified",
                                        "additions": 2, "deletions": 1}]},
                "assets": {"items": [{"sha256": asset_id, "status": "complete",
                    "local_path": "assets/asset", "url": "https://example.test/image.png",
                    "sources": [f"issue:{issue_body}"]}]},
            },
            "archival_view": {"documents": [
                {"source_id": issue_title, "text": "Visual defect"},
                {"source_id": issue_body, "text": "The border must match the attached image."},
            ], "media": []},
        }
        archive_path.write_text(json.dumps(archive))
        source_packet = {
            "schema_version": "text-only-repair-packet-v1", "case_id": "owner__repo-1",
            "repository": "owner/repo", "pr_number": 1, "baseline_sha": "b" * 40,
            "problem_sources": [
                {"source_id": issue_title, "kind": "issue", "field": "title",
                 "url": "https://example.test/issues/2", "text": "Visual defect"},
                {"source_id": issue_body, "kind": "issue", "field": "body",
                 "url": "https://example.test/issues/2",
                 "text": "The border must match the attached image."},
            ],
            "withheld": ["reference_patch"],
            "provenance": {"source_archive": str(archive_path.resolve()),
                           "source_archive_sha256": subject._sha(archive_path)},
        }
        source_packet_path = source_run / "source_packet.json"
        source_packet_path.write_text(json.dumps(source_packet))
        curator = {"case_id": "owner__repo-1", "assets": [{
            "asset_id": asset_id, "sha256": asset_id, "status": "available",
            "url": "https://example.test/image.png", "source_ids": [issue_body],
            "local_path": str(media.resolve()), "display_index": 1,
        }]}
        curator_path = source_run / "curator.json"
        curator_path.write_text(json.dumps(curator))
        source_result = {
            "case_id": "owner__repo-1", "repository": "owner/repo", "pr_number": 1,
            "status": "complete", "packet": str(source_packet_path.resolve()),
            "packet_sha256": subject._sha(source_packet_path),
            "curator_assets": str(curator_path.resolve()),
            "curator_assets_sha256": subject._sha(curator_path),
        }
        source_result_path = source_run / "16_03_result_0001.json"
        source_result_path.write_text(json.dumps(source_result))
        source_manifest_path = source_run / "16_03_run_manifest.json"
        source_manifest_path.write_text(json.dumps({
            "run_id": "fixture-run", "case_ids": ["owner__repo-1"], "pr_numbers": [1]}))
        target = root / "classification"
        target.mkdir(parents=True)
        prompt = target / "16_03_05_visual_capability.system.md"
        schema = target / "16_03_06_visual_capability.schema.json"
        prompt.write_bytes(subject.PROMPT.read_bytes())
        schema.write_bytes(subject.SCHEMA.read_bytes())
        statement = candidate_problem_statement(
            source_packet, curator["assets"], source_archive_documents(source_packet))
        packet = {"task_id": "owner__repo-1", "problem_statement": statement,
                  "assets": [{"asset_id": asset_id, "attachment_index": 1,
                              "source_ids": [issue_body],
                              "model_input_representation": {
                                  "kind": "original_static_image",
                                  "source_sha256": asset_id}}]}
        packet_path = target / "16_03_07_packet_0001.json"
        packet_path.write_text(json.dumps(packet))
        annotation = {
            "schema_version": "visual-capability-classifier-v4",
            "task_id": "owner__repo-1",
            "visual_capabilities": [{
                "category": "spatial_layout_understanding",
                "importance": "core",
                "visual_evidence": "the border has directly visible spacing",
                "task_relation": "the requested repair must match that geometry",
            }],
        }
        attempt = target / "16_03_07_call_0001/semantic_attempt_01"
        attempt.mkdir(parents=True)
        request, raw, provider = (attempt / name for name in (
            "10_api_request.json", "09_model_raw.json", "10_provider_response_01.json"))
        system = (prompt.read_text() + '\nOutput JSON matching this schema:\n'
                  + schema.read_text())
        request.write_text(json.dumps({
            "model": "fixture-model", "temperature": 1.0, "top_p": 0.95,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": [
                    {"type": "text", "text": json.dumps(packet, ensure_ascii=False)},
                    {"type": "image_url", "image_url": {"url":
                        "data:image/png;base64," + base64.b64encode(
                            media.read_bytes()).decode("ascii")}},
                ]},
            ],
            "max_tokens": 16384, "stream": False,
        }))
        raw.write_text(json.dumps(annotation))
        provider.write_text(json.dumps({"model": "fixture-model", "choices": [{
            "finish_reason": "stop", "message": {"content": json.dumps(annotation)}}]}))
        trace = attempt / "10_api_invocation.json"
        profile = {"model": "fixture-model", "protocol": "chat",
                   "endpoint": "https://fixture.invalid"}
        trace.write_text(json.dumps({"backend": "fixture", "profile": profile,
            "attempt_limit": 1,
            "request_sha256": subject._sha(request), "prompt_sha256": subject._sha(prompt),
            "schema_sha256": subject._sha(schema)}))
        attempt_receipt = attempt / "10_attempt_01.json"
        attempt_receipt.write_text(json.dumps({
            "status": "received", "response_sha256": subject._sha(provider)}))
        runner_path = Path(__file__).resolve()
        contract = {"backend": "fixture", "requested_model": "fixture-model",
                    "accepted_response_models": ["fixture-model"],
                    "provider_profile": profile, "max_tokens": 16384,
                    "transport_attempt_limit": 1,
                    "credential_identity": {"source_kind": "test_fixture",
                                            "key_name": "FIXTURE_API_KEY",
                                            "fingerprint": "c" * 64},
                    "client_factory": {"kind": "api_engine_default",
                                       "name": "openai.AzureOpenAI"},
                    "runner": "fixture.Runner", "runner_path": str(runner_path),
                    "runner_sha256": subject._sha(runner_path)}
        invocation = {"backend": "fixture", "model": "fixture-model",
            "requested_model": "fixture-model", "attempts": 1,
            "evaluator_contract": contract, "prompt_sha256": subject._sha(prompt),
            "schema_sha256": subject._sha(schema), "packet_sha256": subject._sha(packet_path),
            "trace": str(trace), "trace_sha256": subject._sha(trace),
            "request": str(request), "request_sha256": subject._sha(request),
            "raw_response": str(raw), "raw_response_sha256": subject._sha(raw),
            "provider_response": str(provider), "provider_response_sha256": subject._sha(provider)}
        invocation.update({"attempt_receipt": str(attempt_receipt),
                           "attempt_receipt_sha256": subject._sha(attempt_receipt)})
        record = {"case_id": "owner__repo-1",
            "source_result_sha256": subject._sha(source_result_path),
            "source_packet_sha256": subject._sha(source_packet_path),
            "source_archive_sha256": subject._sha(archive_path),
            "change_scale": subject.classify_change_scale(archive["sections"]["files"]["items"]),
            "visual_capability": {"status": "complete", "annotation": annotation,
                                  "invocation": invocation},
            "packet": str(packet_path), "packet_sha256": subject._sha(packet_path)}
        manifest = {"schema_version": "pre-human-review-classification-run-v1",
            "created_at": "2026-09-02T00:00:00Z", "source_run": str(source_run.resolve()),
            "source_manifest_sha256": subject._sha(source_manifest_path),
            "source_run_id": "fixture-run", "model_invoked": False,
            "classification_runner_sha256": subject._sha(Path(subject.__file__).resolve()),
            "prompt_sha256": subject._sha(prompt), "schema_sha256": subject._sha(schema),
            "authorization_proposal": None, "run_authorization": None,
            "records": [record], "model_contracts": [contract], "human_review_ready": True}
        manifest_path = target / "16_03_08_pre_review_classifications.json"
        manifest_path.write_text(json.dumps(manifest))
        return source_run, manifest_path

    def test_animated_gif_is_represented_by_audited_contact_sheet(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "animated.gif"
            frames = [Image.new("RGB", (8, 6), color) for color in ("red", "green", "blue")]
            frames[0].save(source, save_all=True, append_images=frames[1:], duration=50, loop=0)
            prepared, representation = subject._prepare_model_image(source, root / "sheet.png")
            self.assertEqual(root / "sheet.png", prepared)
            self.assertEqual("animated_gif_contact_sheet", representation["kind"])
            self.assertEqual([0, 1, 2], representation["sampled_frame_indices"])
            self.assertTrue(prepared.exists())

    def test_unidentified_media_is_routed_to_manual_video_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "clip"
            source.write_bytes(b"\x00\x00\x00\x18ftypqt  not-an-image")
            prepared, representation = subject._prepare_model_image(source, root / "sheet.png")
            self.assertIsNone(prepared)
            self.assertEqual("unsupported_media", representation["kind"])

    def test_unidentified_video_uses_shared_contact_sheet_representation(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "clip"
            source.write_bytes(b"fixture-video")
            destination = root / "sheet.png"

            def prepare(_source, target):
                Image.new("RGB", (12, 8), "green").save(target)
                return target, {"kind": "video_contact_sheet",
                                "source_sha256": subject._sha(_source),
                                "derived_sha256": subject._sha(target)}

            with mock.patch.object(subject, "_prepare_video_contact_sheet", prepare):
                prepared, representation = subject._prepare_model_image(source, destination)
            self.assertEqual(destination, prepared)
            self.assertEqual("video_contact_sheet", representation["kind"])

    def test_video_duration_falls_back_to_decoded_timestamp(self):
        probe = subject.subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="Duration: N/A")
        decoded = subject.subprocess.CompletedProcess(
            args=[], returncode=0, stdout="",
            stderr="frame= 75 fps=30 time=00:00:02.50 bitrate=N/A")
        version = subject.subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ffmpeg version frozen\n", stderr="")
        with mock.patch.object(subject.subprocess, "run",
                               side_effect=[probe, decoded, version]):
            duration, observed_version, method = subject._video_duration(
                Path("/tmp/frozen-ffmpeg"), Path("/tmp/undurationed.webm"))
        self.assertEqual(2.5, duration)
        self.assertEqual("ffmpeg version frozen", observed_version)
        self.assertEqual("decoded_final_timestamp", method)

    def test_image_decode_budget_routes_asset_to_manual_review(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "large.png"
            Image.new("RGB", (20, 20), "red").save(source)
            with mock.patch.object(subject, "MAX_IMAGE_PIXELS", 100):
                prepared, representation = subject._prepare_model_image(
                    source, root / "sheet.png")
            self.assertIsNone(prepared)
            self.assertEqual("decode_budget_exceeded", representation["kind"])

    def test_frozen_schemas_are_valid_and_prompts_define_boundaries(self):
        extension_prompt = (subject.CODE_ROOT / "analysis/prompts/20_09_existing_tests_extension.system.md").read_text()
        extension_schema = json.loads((subject.CODE_ROOT / "analysis/prompts/20_10_existing_tests_extension.schema.json").read_text())
        visual_prompt = subject.PROMPT.read_text()
        visual_schema = json.loads(subject.SCHEMA.read_text())
        jsonschema.Draft202012Validator.check_schema(extension_schema)
        jsonschema.Draft202012Validator.check_schema(visual_schema)
        self.assertIn("candidate_f2p", extension_prompt)
        self.assertIn("must never output final `FAIL_TO_PASS`", extension_prompt)
        self.assertIn("rendering_appearance_understanding", visual_prompt)
        self.assertIn("Categories are multi-label", visual_prompt)
        self.assertNotIn("primary_visual_category", visual_prompt)

    def test_semantic_validator_rejects_duplicate_capabilities(self):
        packet = {"task_id": "x", "assets": []}
        annotation = {
            "schema_version": "visual-capability-classifier-v4", "task_id": "x",
            "visual_capabilities": [{
                "category": "spatial_layout_understanding", "importance": "core",
                "visual_evidence": "spacing", "task_relation": "layout repair",
            }, {
                "category": "spatial_layout_understanding", "importance": "supporting",
                "visual_evidence": "alignment", "task_relation": "layout repair",
            }],
        }
        with self.assertRaisesRegex(ValueError, "duplicated"):
            subject._validate_visual(annotation, packet, subject.SCHEMA)

    def test_semantic_validator_requires_a_core_capability(self):
        packet = {"task_id": "x", "assets": []}
        annotation = {
            "schema_version": "visual-capability-classifier-v4", "task_id": "x",
            "visual_capabilities": [{
                "category": "element_state_understanding", "importance": "supporting",
                "visual_evidence": "disabled state", "task_relation": "state repair",
            }],
        }
        with self.assertRaisesRegex(ValueError, "requires one core"):
            subject._validate_visual(annotation, packet, subject.SCHEMA)

    def test_semantic_validator_rejects_unknown_capability(self):
        with tempfile.TemporaryDirectory() as directory:
            _, manifest_path = self._copy_frozen_classification(Path(directory))
            value = json.loads(manifest_path.read_text())
            packet = json.loads(Path(value["records"][0]["packet"]).read_text())
            annotation = value["records"][0]["visual_capability"]["annotation"]
            annotation["visual_capabilities"][0]["category"] = "media_type_video"
            with self.assertRaises(jsonschema.ValidationError):
                subject._validate_visual(annotation, packet, subject.SCHEMA)

    def test_loader_rejects_problem_statement_source_packet_and_trace_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_run, manifest_path = self._copy_frozen_classification(root)
            value = json.loads(manifest_path.read_text())
            record = value["records"][0]
            packet_path = Path(record["packet"])
            packet = json.loads(packet_path.read_text())
            packet["problem_statement"] += " injected"
            packet_path.write_text(json.dumps(packet))
            record["packet_sha256"] = subject._sha(packet_path)
            manifest_path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "source materials"):
                subject.load_for_source(source_run, manifest_path)

            source_run, manifest_path = self._copy_frozen_classification(root / "packet")
            value = json.loads(manifest_path.read_text())
            record = value["records"][0]
            record["source_packet_sha256"] = "0" * 64
            manifest_path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "source packet binding"):
                subject.load_for_source(source_run, manifest_path)

            source_run, manifest_path = self._copy_frozen_classification(root / "trace")
            value = json.loads(manifest_path.read_text())
            invocation = value["records"][0]["visual_capability"]["invocation"]
            trace = Path(invocation["trace"])
            trace_value = json.loads(trace.read_text())
            trace_value["profile"]["model"] = "different-model"
            trace.write_text(json.dumps(trace_value))
            invocation["trace_sha256"] = subject._sha(trace)
            manifest_path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "trace metadata"):
                subject.load_for_source(source_run, manifest_path)

            source_run, manifest_path = self._copy_frozen_classification(
                root / "trace-endpoint")
            value = json.loads(manifest_path.read_text())
            invocation = value["records"][0]["visual_capability"]["invocation"]
            trace = Path(invocation["trace"])
            trace_value = json.loads(trace.read_text())
            trace_value["profile"]["endpoint"] = "https://other.invalid"
            trace.write_text(json.dumps(trace_value))
            invocation["trace_sha256"] = subject._sha(trace)
            manifest_path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "trace metadata"):
                subject.load_for_source(source_run, manifest_path)

            source_run, manifest_path = self._copy_frozen_classification(
                root / "request-budget")
            value = json.loads(manifest_path.read_text())
            invocation = value["records"][0]["visual_capability"]["invocation"]
            request = Path(invocation["request"])
            request_value = json.loads(request.read_text())
            request_value["max_tokens"] += 1
            request.write_text(json.dumps(request_value))
            invocation["request_sha256"] = subject._sha(request)
            trace = Path(invocation["trace"])
            trace_value = json.loads(trace.read_text())
            trace_value["request_sha256"] = subject._sha(request)
            trace.write_text(json.dumps(trace_value))
            invocation["trace_sha256"] = subject._sha(trace)
            manifest_path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "output budget changed"):
                subject.load_for_source(source_run, manifest_path)

    def test_loader_rejects_model_runner_and_raw_annotation_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_run, manifest_path = self._copy_frozen_classification(root)
            value = json.loads(manifest_path.read_text())
            contract = value["records"][0]["visual_capability"]["invocation"][
                "evaluator_contract"]
            contract["runner_sha256"] = "0" * 64
            value["model_contracts"][0]["runner_sha256"] = "0" * 64
            manifest_path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "runner changed"):
                subject.load_for_source(source_run, manifest_path)

            source_run, manifest_path = self._copy_frozen_classification(root / "raw")
            value = json.loads(manifest_path.read_text())
            invocation = value["records"][0]["visual_capability"]["invocation"]
            raw = Path(invocation["raw_response"])
            raw.write_text(json.dumps({"different": True}))
            invocation["raw_response_sha256"] = subject._sha(raw)
            manifest_path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "provider response or annotation"):
                subject.load_for_source(source_run, manifest_path)

            source_run, manifest_path = self._copy_frozen_classification(root / "provider")
            value = json.loads(manifest_path.read_text())
            invocation = value["records"][0]["visual_capability"]["invocation"]
            provider = Path(invocation["provider_response"])
            provider_value = json.loads(provider.read_text())
            provider_value["choices"][0]["finish_reason"] = "length"
            provider.write_text(json.dumps(provider_value))
            invocation["provider_response_sha256"] = subject._sha(provider)
            receipt = Path(invocation["attempt_receipt"])
            receipt_value = json.loads(receipt.read_text())
            receipt_value["response_sha256"] = subject._sha(provider)
            receipt.write_text(json.dumps(receipt_value))
            invocation["attempt_receipt_sha256"] = subject._sha(receipt)
            manifest_path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "provider response is incomplete"):
                subject.load_for_source(source_run, manifest_path)

    def test_loader_rejects_duplicate_cases_and_changed_prompt_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_run, manifest_path = self._copy_frozen_classification(root)
            value = json.loads(manifest_path.read_text())
            value["records"].append(value["records"][0])
            manifest_path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "duplicate classification case"):
                subject.load_for_source(source_run, manifest_path)

            source_run, manifest_path = self._copy_frozen_classification(root / "second")
            prompt = manifest_path.parent / "16_03_05_visual_capability.system.md"
            prompt.write_text(prompt.read_text() + "\nchanged\n")
            with self.assertRaisesRegex(ValueError, "prompt.*changed"):
                subject.load_for_source(source_run, manifest_path)

    def test_resume_rejects_changed_contract_and_rolls_back_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_run, manifest_path = self._copy_frozen_classification(root)
            prompt = manifest_path.parent / "16_03_05_visual_capability.system.md"
            prompt.write_text(prompt.read_text() + "\nchanged\n")
            source_manifest = json.loads((source_run / "16_03_run_manifest.json").read_text())
            output = root / "resumed"
            with mock.patch(
                    "analysis.scripts.step_16_04_export_human_review.load_rows",
                    return_value=(source_manifest, [])):
                with self.assertRaisesRegex(ValueError, "prompt.*changed"):
                    subject.run(source_run, output, resume_from=manifest_path)
            self.assertFalse(output.exists())

    def test_resume_reuses_a_deeply_bound_packet_and_invocation_trace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_run, manifest_path = self._copy_frozen_classification(root)
            previous = json.loads(manifest_path.read_text())
            record = previous["records"][0]
            source_result = json.loads((source_run / "16_03_result_0001.json").read_text())
            source_packet = json.loads(Path(source_result["packet"]).read_text())
            curator = json.loads(Path(source_result["curator_assets"]).read_text())
            row = {"case_id": record["case_id"],
                "result_sha256": record["source_result_sha256"],
                "packet_sha256": record["source_packet_sha256"],
                "packet": source_packet,
                "human_seed": {"problem_statement": json.loads(
                    Path(record["packet"]).read_text())["problem_statement"]},
                "assets": curator["assets"]}
            source_manifest = json.loads((source_run / "16_03_run_manifest.json").read_text())
            output = root / "resumed"
            with mock.patch(
                    "analysis.scripts.step_16_04_export_human_review.load_rows",
                    return_value=(source_manifest, [row])):
                subject.run(source_run, output, resume_from=manifest_path)
            resumed = output / "16_03_08_pre_review_classifications.json"
            value = subject.validate_classification_run(source_run, resumed)
            reused = value["records"][0]["visual_capability"]["reused_from"]
            self.assertEqual(subject._sha(manifest_path), reused["manifest_sha256"])

    def test_loader_rejects_packet_asset_or_annotation_tampering_even_when_rehashed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_run, manifest_path = self._copy_frozen_classification(root)
            value = json.loads(manifest_path.read_text())
            record = value["records"][0]
            packet = json.loads(Path(record["packet"]).read_text())
            packet["assets"][0]["asset_id"] = "0" * 64
            packet_path = root / "tampered_packet.json"
            packet_path.write_text(json.dumps(packet))
            record["packet"] = str(packet_path)
            record["packet_sha256"] = subject._sha(packet_path)
            manifest_path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "packet assets"):
                subject.load_for_source(source_run, manifest_path)

            source_run, manifest_path = self._copy_frozen_classification(root / "second")
            value = json.loads(manifest_path.read_text())
            record = value["records"][0]
            record["visual_capability"]["annotation"]["visual_capabilities"] = []
            manifest_path.write_text(json.dumps(value))
            with self.assertRaisesRegex(
                    ValueError, "provider response or annotation changed"):
                subject.load_for_source(source_run, manifest_path)

    def test_loader_reconstructs_request_text_images_and_prompt_even_when_rehashed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_run, manifest_path = self._copy_frozen_classification(root)
            value = json.loads(manifest_path.read_text())
            invocation = value["records"][0]["visual_capability"]["invocation"]
            request_path = Path(invocation["request"])
            request = json.loads(request_path.read_text())
            request["messages"][1]["content"][0]["text"] = json.dumps({
                "task_id": "owner__repo-1", "problem_statement": "attacker text",
                "assets": []})
            request_path.write_text(json.dumps(request))
            invocation["request_sha256"] = subject._sha(request_path)
            trace_path = Path(invocation["trace"])
            trace = json.loads(trace_path.read_text())
            trace["request_sha256"] = invocation["request_sha256"]
            trace_path.write_text(json.dumps(trace))
            invocation["trace_sha256"] = subject._sha(trace_path)
            manifest_path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "request packet text"):
                subject.load_for_source(source_run, manifest_path)

    def test_run_rejects_symlinked_archive_media_and_removes_failed_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_run = root / "source"
            source_run.mkdir()
            (source_run / "16_03_run_manifest.json").write_text(json.dumps({
                "run_id": "source", "case_ids": ["owner__repo-1"]}))
            archive_dir = root / "archive"
            media_dir = archive_dir / "11_http_archive/assets"
            media_dir.mkdir(parents=True)
            outside = root / "outside.png"
            outside.write_bytes(b"not-an-image")
            linked = media_dir / "asset"
            linked.symlink_to(outside)
            asset_id = subject._sha(outside)
            archive = archive_dir / "11_record.json"
            archive.write_text(json.dumps({"sections": {
                "files": {"items": [{"filename": "src/a.js", "additions": 1}]},
                "assets": {"items": [{"sha256": asset_id,
                    "local_path": "assets/asset", "status": "complete"}]},
            }}))
            row = {
                "case_id": "owner__repo-1", "result_sha256": "a" * 64,
                "packet_sha256": "b" * 64,
                "packet": {"provenance": {"source_archive": str(archive)}},
                "human_seed": {"problem_statement": "fix the visual defect"},
                "assets": [{"asset_id": asset_id, "status": "available",
                            "local_path": str(linked), "source_ids": ["owner/repo#2:body"]}],
            }
            output = root / "output"
            with mock.patch(
                    "analysis.scripts.step_16_04_export_human_review.load_rows",
                    return_value=({"run_id": "source"}, [row])):
                with self.assertRaisesRegex(ValueError, "symlink"):
                    subject.run(source_run, output)
            self.assertFalse(output.exists())

    def test_run_rejects_changed_archive_media_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_run = root / "source"
            source_run.mkdir()
            (source_run / "16_03_run_manifest.json").write_text(json.dumps({
                "run_id": "source", "case_ids": ["owner__repo-1"]}))
            archive_dir = root / "archive"
            media_dir = archive_dir / "11_http_archive/assets"
            media_dir.mkdir(parents=True)
            media = media_dir / "asset"
            media.write_bytes(b"original")
            asset_id = subject._sha(media)
            archive = archive_dir / "11_record.json"
            archive.write_text(json.dumps({"sections": {
                "files": {"items": [{"filename": "src/a.js", "additions": 1}]},
                "assets": {"items": [{"sha256": asset_id,
                    "local_path": "assets/asset", "status": "complete"}]},
            }}))
            media.write_bytes(b"changed")
            row = {
                "case_id": "owner__repo-1", "result_sha256": "a" * 64,
                "packet_sha256": "b" * 64,
                "packet": {"provenance": {"source_archive": str(archive)}},
                "human_seed": {"problem_statement": "fix the visual defect"},
                "assets": [{"asset_id": asset_id, "status": "available",
                            "local_path": str(media), "source_ids": ["owner/repo#2:body"]}],
            }
            output = root / "output"
            with mock.patch(
                    "analysis.scripts.step_16_04_export_human_review.load_rows",
                    return_value=({"run_id": "source"}, [row])):
                with self.assertRaisesRegex(ValueError, "hash changed"):
                    subject.run(source_run, output)
            self.assertFalse(output.exists())

    def test_model_packet_contains_no_absolute_media_paths(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_run = root / "source"
            source_run.mkdir()
            (source_run / "16_03_run_manifest.json").write_text(json.dumps({
                "run_id": "source", "case_ids": ["owner__repo-1"]}))
            archive_dir = root / "archive"
            media_dir = archive_dir / "11_http_archive/assets"
            media_dir.mkdir(parents=True)
            image = media_dir / "asset"
            Image.new("RGB", (8, 8), "blue").save(image, format="PNG")
            asset_id = subject._sha(image)
            archive = archive_dir / "11_record.json"
            archive.write_text(json.dumps({"sections": {
                "files": {"items": [{"filename": "src/a.js", "additions": 1}]},
                "assets": {"items": [{"sha256": asset_id,
                    "local_path": "assets/asset", "status": "complete"}]},
            }}))
            row = {
                "case_id": "owner__repo-1", "result_sha256": "a" * 64,
                "packet": {"provenance": {"source_archive": str(archive)}},
                "human_seed": {"problem_statement": "fix the visual defect"},
                "assets": [{"asset_id": asset_id, "status": "available",
                            "local_path": str(image), "source_ids": ["owner/repo#2:body"]}],
            }
            row["packet_sha256"] = "b" * 64
            captured = []
            def evaluator(**kwargs):
                captured.append(kwargs["packet"])
                packet = kwargs["packet"]
                annotation = {
                    "schema_version": "visual-capability-classifier-v4",
                    "task_id": packet["task_id"],
                    "visual_capabilities": [{
                        "category": "rendering_appearance_understanding",
                        "importance": "core",
                        "visual_evidence": "the square is blue",
                        "task_relation": "the requested appearance must preserve that color",
                    }],
                }
                work = Path(kwargs["workdir"])
                request = work / "10_api_request.json"
                raw = work / "09_model_raw.json"
                provider = work / "10_provider_response_01.json"
                from pr_crawler.api_engines import request_body
                profile = {"model": "fixture-model", "protocol": "chat",
                           "endpoint": "https://fixture.invalid"}
                system = (Path(kwargs["system_prompt"]).read_text()
                          + '\nOutput JSON matching this schema:\n'
                          + Path(kwargs["schema"]).read_text())
                request.write_text(json.dumps(request_body(
                    profile, packet, kwargs["image_paths"], system, 16384)))
                raw.write_text(json.dumps(annotation))
                provider.write_text(json.dumps({"model": "fixture-model", "choices": [{
                    "finish_reason": "stop", "message": {"content": json.dumps(annotation)}}]}))
                trace = work / "10_api_invocation.json"
                trace.write_text(json.dumps({
                    "backend": "fixture", "profile": profile,
                    "attempt_limit": 1,
                    "request_sha256": subject._sha(request),
                    "prompt_sha256": subject._sha(kwargs["system_prompt"]),
                    "schema_sha256": subject._sha(kwargs["schema"]),
                }))
                attempt = work / "10_attempt_01.json"
                attempt.write_text(json.dumps({
                    "status": "received", "response_sha256": subject._sha(provider)}))
                return annotation, {"backend": "fixture", "model": "fixture-model",
                    "requested_model": "fixture-model", "attempts": 1,
                    "request": str(request), "request_sha256": subject._sha(request),
                    "raw_response": str(raw), "raw_response_sha256": subject._sha(raw),
                    "provider_response": str(provider),
                    "provider_response_sha256": subject._sha(provider)}
            evaluator.backend = "fixture"
            evaluator.profile = {"model": "fixture-model", "protocol": "chat",
                                 "endpoint": "https://fixture.invalid"}
            evaluator.attempts = 1
            evaluator.max_tokens = 16384
            evaluator.client_factory = None
            evaluator.credential_identity = {"source_kind": "test_fixture",
                                             "key_name": "FIXTURE_API_KEY",
                                             "fingerprint": "c" * 64}
            canonical_output = subject.RUNS_ROOT / f"_classification_test_{root.name}" / "output"
            identity = {"run_id": "classification-test-run-001",
                        "nonce": f"classification_{root.name}_nonce",
                        "expires_at": "2099-01-01T00:00:00+00:00"}
            proposal = subject._authorization_proposal(
                source_run, [row], subject._evaluator_contract(evaluator), 1,
                subject._validate_authorization_identity(identity, canonical_output))
            self.assertEqual(1, proposal["expected_case_calls"])
            self.assertEqual(2, proposal["maximum_api_requests"])
            self.assertEqual("b" * 64,
                             proposal["case_bindings"][0]["source_packet_sha256"])
            self.assertEqual("fixture-model",
                             proposal["evaluator_contract"]["requested_model"])
            authorization = dict(proposal)
            authorization["schema_version"] = "classification-run-authorization-v1"
            authorization["authorized"] = True
            authorization_path = (subject.REPORT_ROOT / "evidence"
                                  / f"{identity['nonce']}.authorization.json")
            authorization_path.write_text(json.dumps(authorization))
            receipt = (subject.REPORT_ROOT / "evidence/classification_authorization_receipts"
                       / f"{identity['nonce']}.json")
            try:
                with mock.patch(
                        "analysis.scripts.step_16_04_export_human_review.load_rows",
                        return_value=({"run_id": "source"}, [row])):
                    subject.run(source_run, canonical_output, run_model=True, evaluator=evaluator,
                                authorization_path=authorization_path)
                serialized = json.dumps(captured[0])
                self.assertNotIn(str(root), serialized)
                stored = json.loads(
                    (canonical_output / "16_03_07_packet_0001.json").read_text())
                self.assertNotIn(str(root), json.dumps(stored))
                call_count = len(captured)
                shutil.rmtree(canonical_output)
                with mock.patch(
                        "analysis.scripts.step_16_04_export_human_review.load_rows",
                        return_value=({"run_id": "source"}, [row])):
                    with self.assertRaisesRegex(ValueError, "nonce_already_consumed"):
                        subject.run(source_run, canonical_output, run_model=True,
                                    evaluator=evaluator, authorization_path=authorization_path)
                self.assertEqual(call_count, len(captured))
            finally:
                shutil.rmtree(canonical_output.parent, ignore_errors=True)
                authorization_path.unlink(missing_ok=True)
                receipt.unlink(missing_ok=True)

    def test_model_run_rejects_missing_or_mismatched_authorization_before_evaluator(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_run = root / "source"
            source_run.mkdir()
            (source_run / "16_03_run_manifest.json").write_text(json.dumps({
                "run_id": "source", "case_ids": ["owner__repo-1"]}))
            archive = root / "archive.json"
            archive.write_text(json.dumps({"sections": {
                "files": {"items": [{"filename": "src/a.js", "additions": 1}]},
                "assets": {"items": []}}}))
            row = {"case_id": "owner__repo-1", "result_sha256": "a" * 64,
                "packet_sha256": "b" * 64,
                "packet": {"provenance": {"source_archive": str(archive)}},
                "human_seed": {"problem_statement": "visual defect"}, "assets": []}
            calls = []
            def evaluator(**kwargs):
                calls.append(kwargs)
                raise AssertionError("authorization failure must precede evaluator")
            evaluator.backend = "fixture"
            evaluator.profile = {"model": "fixture-model", "protocol": "chat",
                                 "endpoint": "https://fixture.invalid"}
            evaluator.attempts = 1
            evaluator.max_tokens = 16384
            evaluator.client_factory = None
            evaluator.credential_identity = {"source_kind": "test_fixture",
                                             "key_name": "FIXTURE_API_KEY",
                                             "fingerprint": "c" * 64}
            load = mock.patch(
                "analysis.scripts.step_16_04_export_human_review.load_rows",
                return_value=({"run_id": "source"}, [row]))
            with load, self.assertRaisesRegex(ValueError, "authorization_required"):
                subject.run(source_run, root / "missing", run_model=True,
                            evaluator=evaluator)
            self.assertFalse((root / "missing").exists())
            canonical_output = subject.RUNS_ROOT / f"_classification_wrong_{root.name}" / "output"
            identity = {"run_id": "classification-wrong-run-001",
                        "nonce": f"classification_wrong_{root.name}_nonce",
                        "expires_at": "2099-01-01T00:00:00+00:00"}
            bound_identity = subject._validate_authorization_identity(identity, canonical_output)
            proposal = subject._authorization_proposal(
                source_run, [row], subject._evaluator_contract(evaluator), 1,
                bound_identity)
            proposal["expected_case_calls"] = 999
            proposal["schema_version"] = "classification-run-authorization-v1"
            proposal["authorized"] = True
            authorization = (subject.REPORT_ROOT / "evidence"
                             / f"{identity['nonce']}.authorization.json")
            authorization.write_text(json.dumps(proposal))
            try:
                with mock.patch(
                        "analysis.scripts.step_16_04_export_human_review.load_rows",
                        return_value=({"run_id": "source"}, [row])):
                    with self.assertRaisesRegex(ValueError, "binding_mismatch"):
                        subject.run(source_run, canonical_output, run_model=True,
                                    evaluator=evaluator, authorization_path=authorization)
                self.assertFalse(canonical_output.exists())
                self.assertEqual([], calls)
                with mock.patch(
                        "analysis.scripts.step_16_04_export_human_review.load_rows",
                        return_value=({"run_id": "source"}, [row])):
                    proposal_run = subject.run(
                        source_run, root / "proposal", evaluator=evaluator,
                        authorization_identity=identity,
                        canonical_output=canonical_output)
                self.assertFalse(proposal_run["model_invoked"])
                self.assertEqual(
                    "classification-run-authorization-proposal-v1",
                    proposal_run["authorization_proposal"]["schema_version"])
                self.assertEqual([], calls)
            finally:
                shutil.rmtree(canonical_output.parent, ignore_errors=True)
                authorization.unlink(missing_ok=True)

    def test_all_reused_model_run_does_not_consume_authorization(self):
        from analysis.scripts.step_16_04_export_human_review import load_rows
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_run, prior_manifest = self._copy_frozen_classification(root)
            calls = []

            def evaluator(**kwargs):
                calls.append(kwargs)
                raise AssertionError("a fully reused run must not call the evaluator")

            evaluator.backend = "fixture"
            evaluator.profile = {"model": "fixture-model", "protocol": "chat",
                                 "endpoint": "https://fixture.invalid"}
            evaluator.attempts = 1
            evaluator.max_tokens = 16384
            evaluator.client_factory = None
            evaluator.credential_identity = {"source_kind": "test_fixture",
                                             "key_name": "FIXTURE_API_KEY",
                                             "fingerprint": "c" * 64}
            contract = subject._evaluator_contract(evaluator)
            prior = json.loads(prior_manifest.read_text())
            invocation = prior["records"][0]["visual_capability"]["invocation"]
            invocation["evaluator_contract"] = contract
            trace = Path(invocation["trace"])
            trace_value = json.loads(trace.read_text())
            trace_value["profile"] = contract["provider_profile"]
            trace_value["attempt_limit"] = contract["transport_attempt_limit"]
            trace.write_text(json.dumps(trace_value))
            invocation["trace_sha256"] = subject._sha(trace)
            prior["model_contracts"] = [contract]
            prior_manifest.write_text(json.dumps(prior))

            _, rows = load_rows(source_run)
            canonical_output = (subject.RUNS_ROOT
                                / f"_classification_reuse_{root.name}" / "output")
            identity = {"run_id": "classification-reuse-run-001",
                        "nonce": f"classification_reuse_{root.name}_nonce",
                        "expires_at": "2099-01-01T00:00:00+00:00"}
            bound_identity = subject._validate_authorization_identity(
                identity, canonical_output)
            proposal = subject._authorization_proposal(
                source_run, rows, contract, 1, bound_identity)
            authorization_value = dict(proposal)
            authorization_value.update({
                "schema_version": "classification-run-authorization-v1",
                "authorized": True,
            })
            authorization_path = (subject.REPORT_ROOT / "evidence"
                                  / f"{identity['nonce']}.authorization.json")
            authorization_path.write_text(json.dumps(authorization_value))
            receipt = (subject.REPORT_ROOT / "evidence/classification_authorization_receipts"
                       / f"{identity['nonce']}.json")
            try:
                result = subject.run(
                    source_run, canonical_output, run_model=True, evaluator=evaluator,
                    resume_from=prior_manifest, authorization_path=authorization_path)
                self.assertFalse(result["model_invoked"])
                self.assertIsNone(result["run_authorization"])
                self.assertEqual([], calls)
                self.assertFalse(receipt.exists())
            finally:
                shutil.rmtree(canonical_output.parent, ignore_errors=True)
                authorization_path.unlink(missing_ok=True)
                receipt.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
