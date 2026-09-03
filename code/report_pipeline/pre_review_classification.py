"""Freeze change-scale evidence and optionally run the visual-capability VLM.

This stage runs after the text-only verifier and before human review.  The
change scale is deterministic; the visual capability result is model evidence
and remains subject to human calibration.
"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from report_pipeline.atomic import write_json
from report_pipeline.paths import REPORT_ROOT, RUNS_ROOT


CODE_ROOT = Path(__file__).resolve().parents[1]
PROMPT = CODE_ROOT / "analysis/prompts/20_09_visual_capability_classifier_v4.system.md"
SCHEMA = CODE_ROOT / "analysis/prompts/20_10_visual_capability_classifier_v4.schema.json"

SOURCE_SUFFIXES = {
    ".c", ".cc", ".cpp", ".css", ".cs", ".go", ".h", ".hpp", ".html",
    ".java", ".js", ".jsx", ".kt", ".kts", ".less", ".mjs", ".php",
    ".py", ".rb", ".rs", ".sass", ".scss", ".svelte", ".swift", ".ts",
    ".tsx", ".vue",
}
BINARY_SUFFIXES = {
    ".bmp", ".gif", ".ico", ".jpeg", ".jpg", ".mov", ".mp3", ".mp4",
    ".pdf", ".png", ".ttf", ".wav", ".webm", ".webp", ".woff", ".woff2",
}
LOCK_NAMES = {
    "bun.lockb", "cargo.lock", "composer.lock", "gemfile.lock", "go.sum",
    "package-lock.json", "pnpm-lock.yaml", "poetry.lock", "uv.lock", "yarn.lock",
}
MAX_MEDIA_BYTES = 32 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_ANIMATION_FRAMES = 300
VIDEO_FFMPEG_ENV = "SWE_VISUAL_FFMPEG"
VIDEO_SAMPLE_COUNT = 6
RESUMABLE_FINAL_CAPABILITY_STATUSES = frozenset({"complete", "ineligible"})


class _AuthorizationConsumptionError(ValueError):
    """Authorization receipts are run-level failures, never per-case evidence."""


class _SourceMediaRecordError(ValueError):
    """One case has unresolved archive membership, without implying file tamper."""


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, value: dict) -> None:
    write_json(path, value)


def _evaluator_contract(evaluator) -> dict:
    """Freeze the provider/model and the exact adapter implementation used."""
    backend = getattr(evaluator, "backend", None)
    profile = getattr(evaluator, "profile", None)
    requested_model = profile.get("model") if isinstance(profile, dict) else None
    runner_object = evaluator if inspect.isfunction(evaluator) else type(evaluator)
    runner_path_value = inspect.getsourcefile(runner_object)
    if not backend or not requested_model or not runner_path_value:
        raise ValueError("evaluator must expose backend, profile.model, and source runner")
    logical_runner_path = Path(runner_path_value)
    if logical_runner_path.is_symlink():
        raise ValueError("evaluator runner must be a real source file")
    runner_path = logical_runner_path.resolve(strict=True)
    if not runner_path.is_file():
        raise ValueError("evaluator runner must be a real source file")
    provider_profile = dict(profile)
    if (provider_profile.get("protocol") not in {"chat", "responses"}
            or not provider_profile.get("endpoint")):
        raise ValueError("evaluator provider profile is incomplete")
    accepted_response_models = getattr(
        evaluator, "accepted_response_models", [requested_model])
    if (not isinstance(accepted_response_models, list)
            or not accepted_response_models
            or any(not isinstance(item, str) or not item
                   for item in accepted_response_models)
            or len(set(accepted_response_models)) != len(accepted_response_models)):
        raise ValueError("evaluator response-model allowlist is invalid")
    max_tokens = getattr(evaluator, "max_tokens", None)
    attempts = getattr(evaluator, "attempts", None)
    if (not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0
            or not isinstance(attempts, int) or not 1 <= attempts <= 3):
        raise ValueError("evaluator token or transport-attempt budget is invalid")
    credential = getattr(evaluator, "credential_identity", None)
    if credential is None:
        key_name = provider_profile.get("key_name")
        if not key_name:
            raise ValueError("evaluator credential identity is unavailable")
        from pr_crawler.api_engines import load_key
        key = load_key(provider_profile, getattr(evaluator, "key_file", None))
        source_kind = "environment" if os.environ.get(key_name) else "key_file"
        credential = {
            "source_kind": source_kind,
            "key_name": key_name,
            "fingerprint": hashlib.sha256(
                ("visual-classifier-credential-v1\0" + backend + "\0" + key).encode()
            ).hexdigest(),
        }
    if (not isinstance(credential, dict)
            or credential.get("source_kind") not in {"environment", "key_file", "test_fixture"}
            or not credential.get("key_name")
            or not re.fullmatch(r"[0-9a-f]{64}", str(credential.get("fingerprint", "")))):
        raise ValueError("evaluator credential identity is invalid")
    factory = getattr(evaluator, "client_factory", None)
    if factory is None:
        factory_contract = {"kind": "api_engine_default",
                            "name": ("openai.OpenAI" if provider_profile["protocol"] == "responses"
                                     else "openai.AzureOpenAI")}
    else:
        factory_object = factory if inspect.isfunction(factory) or inspect.isclass(factory) else type(factory)
        factory_path_value = inspect.getsourcefile(factory_object)
        if not factory_path_value:
            raise ValueError("custom evaluator client factory source is unavailable")
        factory_path = Path(factory_path_value).resolve(strict=True)
        factory_contract = {"kind": "custom",
                            "name": f"{factory_object.__module__}.{factory_object.__qualname__}",
                            "path": str(factory_path), "sha256": _sha(factory_path)}
    return {
        "backend": backend,
        "requested_model": requested_model,
        "accepted_response_models": accepted_response_models,
        "provider_profile": provider_profile,
        "max_tokens": max_tokens,
        "transport_attempt_limit": attempts,
        "credential_identity": credential,
        "client_factory": factory_contract,
        "runner": f"{runner_object.__module__}.{runner_object.__qualname__}",
        "runner_path": str(runner_path),
        "runner_sha256": _sha(runner_path),
    }


def _validate_evaluator_contract(contract: object) -> dict:
    if not isinstance(contract, dict):
        raise ValueError("classification evaluator contract is missing")
    profile = contract.get("provider_profile")
    factory = contract.get("client_factory")
    credential = contract.get("credential_identity")
    accepted_response_models = contract.get("accepted_response_models")
    if (not contract.get("backend") or not contract.get("requested_model")
            or not contract.get("runner") or not contract.get("runner_path")
            or not contract.get("runner_sha256") or not isinstance(profile, dict)
            or profile.get("model") != contract.get("requested_model")
            or not isinstance(accepted_response_models, list)
            or contract.get("requested_model") not in accepted_response_models
            or any(not isinstance(item, str) or not item for item in accepted_response_models)
            or len(set(accepted_response_models)) != len(accepted_response_models)
            or profile.get("protocol") not in {"chat", "responses"}
            or not profile.get("endpoint")
            or not isinstance(contract.get("max_tokens"), int)
            or contract["max_tokens"] <= 0
            or not isinstance(contract.get("transport_attempt_limit"), int)
            or not 1 <= contract["transport_attempt_limit"] <= 3
            or not isinstance(factory, dict)
            or factory.get("kind") not in {"api_engine_default", "custom"}
            or not factory.get("name")
            or not isinstance(credential, dict)
            or credential.get("source_kind") not in {"environment", "key_file", "test_fixture"}
            or not credential.get("key_name")
            or not re.fullmatch(r"[0-9a-f]{64}", str(credential.get("fingerprint", "")))):
        raise ValueError("classification evaluator contract is incomplete")
    runner_path = Path(contract["runner_path"])
    if (runner_path.is_symlink() or not runner_path.is_file()
            or _sha(runner_path) != contract["runner_sha256"]):
        raise ValueError("classification evaluator runner changed")
    if factory["kind"] == "custom":
        factory_path = Path(factory.get("path", ""))
        if (factory_path.is_symlink() or not factory_path.is_file()
                or _sha(factory_path) != factory.get("sha256")):
            raise ValueError("classification evaluator client factory changed")
    return contract


def _bound_trace_file(path_value: str, expected_sha256: str,
                      root: Path, label: str) -> Path:
    path = Path(path_value)
    if (not path.is_absolute() or path.is_symlink() or not path.is_file()
            or not path.resolve().is_relative_to(root.resolve())
            or _sha(path.resolve()) != expected_sha256):
        raise ValueError(f"classification invocation {label} trace changed")
    return path.resolve()


def _data_url_sha256(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("classification request image is not a data URL")
    match = re.fullmatch(
        r"data:image/(?:png|jpeg|webp);base64,([A-Za-z0-9+/]*={0,2})", value)
    if not match:
        raise ValueError("classification request image data URL is invalid")
    try:
        payload = base64.b64decode(match.group(1), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("classification request image base64 is invalid") from exc
    return hashlib.sha256(payload).hexdigest()


def _validate_request_semantics(request: dict, packet: dict, prompt: Path,
                                schema: Path, protocol: str,
                                requested_model: str,
                                invocation: dict, expected_max_tokens: int,
                                *, allow_legacy_video_probe: bool = False) -> None:
    """Reconstruct the solver-visible request instead of trusting file hashes."""
    if protocol not in {"chat", "responses"} or not isinstance(request, dict):
        raise ValueError("classification request protocol is unsupported")
    expected_packet = json.loads(json.dumps(packet))
    semantic_attempts = invocation.get("semantic_validation_attempts", 1)
    prior_failures = invocation.get("prior_validation_failures", [])
    if (not isinstance(semantic_attempts, int) or semantic_attempts not in {1, 2}
            or not isinstance(prior_failures, list)
            or len(prior_failures) != semantic_attempts - 1
            or any(not isinstance(item, str) for item in prior_failures)):
        raise ValueError("classification semantic retry evidence changed")
    if prior_failures:
        expected_packet["previous_output_validation_error"] = prior_failures[-1]
    expected_text = json.dumps(expected_packet, ensure_ascii=False)
    expected_system = (prompt.read_text()
                       + "\nOutput JSON matching this schema:\n"
                       + schema.read_text())
    expected_image_hashes = []
    for asset in packet.get("assets", []):
        representation = asset.get("model_input_representation")
        if not isinstance(representation, dict):
            raise ValueError("classification packet lacks model image representation")
        kind = representation.get("kind")
        digest = (representation.get("source_sha256")
                  if kind == "original_static_image"
                  else representation.get("derived_sha256")
                  if kind in {"animated_gif_contact_sheet", "video_contact_sheet"}
                  else None)
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("classification model image representation is invalid")
        if kind == "video_contact_sheet":
            timestamps = representation.get("sampled_timestamps_seconds")
            duration = representation.get("duration_seconds")
            if (representation.get("source_sha256") != asset.get("asset_id")
                    or not isinstance(duration, (int, float)) or duration <= 0
                    or not isinstance(timestamps, list) or len(timestamps) != VIDEO_SAMPLE_COUNT
                    or timestamps != sorted(timestamps)
                    or any(not isinstance(value, (int, float))
                           or value < 0 or value >= duration for value in timestamps)
                    or representation.get("layout") != {
                        "order": "left_to_right_top_to_bottom", "columns": 3, "rows": 2}
                    or representation.get("duration_probe_method") not in (
                        {"container_metadata", "decoded_final_timestamp", None}
                        if allow_legacy_video_probe else
                        {"container_metadata", "decoded_final_timestamp"})
                    or not re.fullmatch(r"[0-9a-f]{64}",
                                        str(representation.get("ffmpeg_sha256")))
                    or not isinstance(representation.get("ffmpeg_version"), str)):
                raise ValueError("classification video contact-sheet contract is invalid")
        expected_image_hashes.append(digest)

    common = {"model", "temperature", "top_p"}
    if (request.get("model") != requested_model
            or request.get("temperature") != 1.0
            or request.get("top_p") != 0.95):
        raise ValueError("classification request model parameters changed")
    if protocol == "responses":
        if set(request) != common | {"instructions", "input", "max_output_tokens"}:
            raise ValueError("classification responses request shape changed")
        if request.get("instructions") != expected_system:
            raise ValueError("classification request system prompt changed")
        inputs = request.get("input")
        if (not isinstance(inputs, list) or len(inputs) != 1
                or inputs[0].get("role") != "user"
                or set(inputs[0]) != {"role", "content"}):
            raise ValueError("classification responses input shape changed")
        content = inputs[0].get("content")
        if (not isinstance(content, list) or not content
                or content[0] != {"type": "input_text", "text": expected_text}
                or any(not isinstance(item, dict) or set(item) != {"type", "image_url"}
                       or item.get("type") != "input_image" for item in content[1:])):
            raise ValueError("classification request packet text or image order changed")
        observed_image_hashes = [_data_url_sha256(item["image_url"])
                                 for item in content[1:]]
        token_budget = request.get("max_output_tokens")
    else:
        if set(request) != common | {"messages", "max_tokens", "stream"}:
            raise ValueError("classification chat request shape changed")
        messages = request.get("messages")
        if (not isinstance(messages, list) or len(messages) != 2
                or messages[0] != {"role": "system", "content": expected_system}
                or not isinstance(messages[1], dict)
                or set(messages[1]) != {"role", "content"}
                or messages[1].get("role") != "user"):
            raise ValueError("classification chat messages changed")
        content = messages[1].get("content")
        if (not isinstance(content, list) or not content
                or content[0] != {"type": "text", "text": expected_text}
                or any(not isinstance(item, dict) or set(item) != {"type", "image_url"}
                       or item.get("type") != "image_url"
                       or not isinstance(item.get("image_url"), dict)
                       or set(item["image_url"]) != {"url"} for item in content[1:])
                or request.get("stream") is not False):
            raise ValueError("classification request packet text or image order changed")
        observed_image_hashes = [_data_url_sha256(item["image_url"]["url"])
                                 for item in content[1:]]
        token_budget = request.get("max_tokens")
    if observed_image_hashes != expected_image_hashes:
        raise ValueError("classification request image bytes or order changed")
    if token_budget != expected_max_tokens:
        raise ValueError("classification request output budget changed")


def _case_authorization_bindings(rows: list[dict]) -> list[dict]:
    bindings = []
    for row in rows:
        archive_path = Path(row["packet"]["provenance"]["source_archive"]).resolve(
            strict=True)
        archive = json.loads(archive_path.read_text())
        available_ids = [asset["asset_id"] for asset in row["assets"]
                         if asset.get("status") == "available"]
        _, media_error = _resolve_available_media(row["assets"], archive_path, archive)
        binding = {
            "case_id": row["case_id"],
            "source_result_sha256": row["result_sha256"],
            "source_packet_sha256": row["packet_sha256"],
            "source_archive_sha256": _sha(archive_path),
            "problem_statement_sha256": hashlib.sha256(
                row["human_seed"]["problem_statement"].encode()).hexdigest(),
            "available_asset_ids": available_ids,
            "source_media_binding_status": "invalid" if media_error else "valid",
            "source_media_binding_error": media_error,
            "eligible_for_model_call": bool(
                row["human_seed"]["problem_statement"].strip()
                and available_ids and media_error is None),
        }
        encoded = json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()
        binding["case_binding_sha256"] = hashlib.sha256(encoded).hexdigest()
        bindings.append(binding)
    return bindings


def _validate_authorization_identity(identity: dict, canonical_output: Path) -> dict:
    run_id, nonce, expires_at = (identity.get(name) for name in (
        "run_id", "nonce", "expires_at"))
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{8,128}", run_id):
        raise ValueError("classification_run_authorization_run_id_invalid")
    if not isinstance(nonce, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", nonce):
        raise ValueError("classification_run_authorization_nonce_invalid")
    try:
        expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("classification_run_authorization_expiry_invalid") from None
    if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
        raise ValueError("classification_run_authorization_expired")
    canonical_output = canonical_output.resolve()
    runs_root = RUNS_ROOT.resolve()
    if canonical_output != runs_root and not canonical_output.is_relative_to(runs_root):
        raise ValueError("classification_run_authorization_output_outside_runs")
    return {"run_id": run_id, "nonce": nonce, "expires_at": expires_at,
            "canonical_output": str(canonical_output)}


def _authorization_proposal(source_run: Path, rows: list[dict],
                            evaluator_contract: dict, attempts: int,
                            identity: dict) -> dict:
    source_run = source_run.resolve()
    cases = _case_authorization_bindings(rows)
    expected = sum(item["eligible_for_model_call"] for item in cases)
    return {
        "schema_version": "classification-run-authorization-proposal-v1",
        "source_run": str(source_run),
        "source_manifest_sha256": _sha(source_run / "16_03_run_manifest.json"),
        "prompt_sha256": _sha(PROMPT),
        "schema_sha256": _sha(SCHEMA),
        "classification_runner_sha256": _sha(Path(__file__).resolve()),
        **identity,
        "evaluator_contract": evaluator_contract,
        "case_bindings": cases,
        "expected_case_calls": expected,
        "semantic_attempt_limit_per_case": 2,
        "transport_attempt_limit_per_semantic_call": attempts,
        "maximum_api_requests": expected * 2 * attempts,
    }


def _read_authorization_identity(path: Path | None, canonical_output: Path) -> tuple[Path, dict]:
    if path is None:
        raise ValueError("classification_run_authorization_required")
    if path.is_symlink():
        raise ValueError("classification_run_authorization_must_be_a_real_file")
    path = path.resolve(strict=True)
    if not path.is_file():
        raise ValueError("classification_run_authorization_must_be_a_real_file")
    authorization = json.loads(path.read_text())
    evidence_root = (REPORT_ROOT / "evidence").resolve()
    if path.parent != evidence_root and not path.is_relative_to(evidence_root):
        raise ValueError("classification_run_authorization_outside_formal_evidence")
    identity = _validate_authorization_identity(authorization, canonical_output)
    if authorization.get("canonical_output") != identity["canonical_output"]:
        raise ValueError("classification_run_authorization_output_mismatch")
    return path, identity


def _validate_authorization(path: Path, proposal: dict) -> dict:
    authorization = json.loads(path.read_text())
    expected = dict(proposal)
    expected["schema_version"] = "classification-run-authorization-v1"
    expected["authorized"] = True
    if authorization != expected:
        raise ValueError("classification_run_authorization_binding_mismatch")
    return {"path": str(path), "sha256": _sha(path),
            "run_id": authorization["run_id"], "nonce": authorization["nonce"],
            "expires_at": authorization["expires_at"],
            "canonical_output": authorization["canonical_output"]}


def _consume_authorization(authorization: dict) -> dict:
    registry = REPORT_ROOT / "evidence/classification_authorization_receipts"
    registry.mkdir(parents=True, exist_ok=True)
    receipt_path = registry / f"{authorization['nonce']}.json"
    receipt = {
        "schema_version": "classification-authorization-receipt-v1",
        "status": "consumed",
        "run_id": authorization["run_id"],
        "nonce": authorization["nonce"],
        "expires_at": authorization["expires_at"],
        "canonical_output": authorization["canonical_output"],
        "authorization": {"path": authorization["path"],
                          "sha256": authorization["sha256"]},
        "consumed_at": datetime.now(timezone.utc).isoformat(),
    }
    payload = (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode()
    try:
        descriptor = os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ValueError("classification_run_authorization_nonce_already_consumed") from None
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        receipt_path.unlink(missing_ok=True)
        raise
    return {"path": str(receipt_path.resolve()), "sha256": _sha(receipt_path)}


def _whitespace_only_patch(patch: str | None) -> bool:
    """Recognize the auditable subset of format-only hunks with equal tokens."""
    if not patch:
        return False
    removed = [re.sub(r"\s+", "", line[1:]) for line in patch.splitlines()
               if line.startswith("-") and not line.startswith("---")]
    added = [re.sub(r"\s+", "", line[1:]) for line in patch.splitlines()
             if line.startswith("+") and not line.startswith("+++")]
    return bool(removed) and removed == added


def exclusion_reason(filename: str, patch: str | None = None) -> str | None:
    """Return a conservative reason for excluding a non-production file."""
    path = filename.replace("\\", "/")
    lower = path.lower()
    parts = [part for part in lower.split("/") if part]
    name = parts[-1] if parts else lower
    suffix = Path(name).suffix.lower()
    stem = Path(name).stem.lower()
    if name in LOCK_NAMES or name.endswith(".lock"):
        return "lockfile"
    if suffix in BINARY_SUFFIXES:
        return "binary_or_media"
    if ("__snapshots__" in parts or suffix == ".snap" or name.endswith(".snapshot")):
        return "snapshot"
    if any(part in {"test", "tests", "spec", "specs", "__tests__", "cypress", "e2e"}
           for part in parts[:-1]) or re.search(r"(?:^|[._-])(test|tests|spec|specs)(?:[._-]|$)", stem):
        return "test_code"
    if any(part in {"dist", "build", "coverage", "vendor", "generated"}
           for part in parts[:-1]) or name.endswith((".min.js", ".min.css", ".generated.js", ".generated.ts")):
        return "generated_or_build_output"
    if suffix not in SOURCE_SUFFIXES:
        return "non_source_file"
    if _whitespace_only_patch(patch):
        return "detected_whitespace_only_change"
    return None


def classify_change_scale(files: list[dict]) -> dict:
    """Classify the reference patch using the frozen line/file thresholds."""
    raw_files = []
    production_files = []
    excluded_files = []
    for item in files:
        row = {
            "filename": item["filename"],
            "status": item.get("status"),
            "additions": int(item.get("additions") or 0),
            "deletions": int(item.get("deletions") or 0),
        }
        row["changed_lines"] = row["additions"] + row["deletions"]
        raw_files.append(row)
        reason = exclusion_reason(row["filename"], item.get("patch"))
        if reason:
            excluded_files.append({**row, "exclusion_reason": reason})
        else:
            production_files.append(row)
    file_count = len(production_files)
    changed_lines = sum(item["changed_lines"] for item in production_files)
    if file_count == 0:
        label = "无法分类"
        review = True
        reason = "清洗后没有可识别的生产源代码文件"
    elif file_count == 1 and changed_lines <= 100:
        label, review = "小规模修改", False
        reason = "1 个生产源代码文件，清洗后增删行数不超过 100"
    elif 2 <= file_count <= 4 and changed_lines <= 100:
        label, review = "中规模修改", False
        reason = "2–4 个生产源代码文件，清洗后增删行数不超过 100"
    else:
        label, review = "大规模修改", False
        reason = "清洗后增删行数超过 100，或涉及至少 5 个生产源代码文件"
    return {
        "schema_version": "reference-change-scale-v1",
        "label": label,
        "reason": reason,
        "cleaned_source_file_count": file_count,
        "cleaned_changed_lines": changed_lines,
        "raw_changed_file_count": len(raw_files),
        "raw_changed_lines": sum(item["changed_lines"] for item in raw_files),
        "production_files": production_files,
        "excluded_files": excluded_files,
        "human_review_required": review,
        "limitations": [
            "GitHub changed-file metadata cannot reliably identify pure formatting hunks; retain patch text for human audit.",
            "The source/test/generated-file rules are frozen heuristics and their per-file decisions are preserved above.",
        ],
    }


V4_CAPABILITIES = frozenset({
    "rendering_appearance_understanding",
    "spatial_layout_understanding",
    "element_state_understanding",
    "interaction_temporal_understanding",
})


def _validate_visual(annotation: dict, packet: dict, schema_path: Path) -> None:
    import jsonschema
    jsonschema.validate(annotation, json.loads(schema_path.read_text()))
    if annotation["task_id"] != packet["task_id"]:
        raise ValueError("visual classification task_id differs from packet")
    if annotation.get("schema_version") == "visual-capability-classifier-v4":
        capabilities = annotation["visual_capabilities"]
        categories = [item["category"] for item in capabilities]
        if len(categories) != len(set(categories)):
            raise ValueError("visual capability categories are duplicated")
        if not set(categories) <= V4_CAPABILITIES:
            raise ValueError("visual capability category is unsupported")
        if not any(item["importance"] == "core" for item in capabilities):
            raise ValueError("visual capability classification requires one core capability")
        return

    # Frozen V3 runs remain independently revalidatable as migration inputs.
    # New executions use V4 and never emit the fields below.
    if annotation.get("schema_version") != "visual-capability-classifier-v3":
        raise ValueError("unsupported visual capability annotation version")
    expected = [asset["asset_id"] for asset in packet["assets"]]
    observed = [asset["asset_id"] for asset in annotation["assets"]]
    if observed != expected:
        raise ValueError("visual classification asset order differs from packet")
    reasons = annotation["human_review_reasons"]
    if annotation["human_review_required"] != bool(reasons):
        raise ValueError("human review flag and reasons differ")
    constraints = annotation["atomic_visual_constraints"]
    constraint_ids = [item["constraint_id"] for item in constraints]
    if constraint_ids != [f"constraint_{index:03d}"
                          for index in range(1, len(constraints) + 1)]:
        raise ValueError("visual constraint IDs are not sequential")
    expected_set = set(expected)
    if any(not set(item["evidence_asset_ids"]) <= expected_set for item in constraints):
        raise ValueError("visual constraint references an unknown asset")

    strict = annotation["strict_multimodal_admission"] == "非文字视觉信息候选不可替代"
    primary = annotation["primary_visual_category"]
    purity = annotation["category_purity"]
    contributing = annotation["contributing_visual_categories"]
    if not strict:
        if constraints or primary is not None or purity is not None or contributing:
            raise ValueError("non-strict classification must not assign visual constraints")
        return
    if not constraints:
        raise ValueError("strict classification requires visual constraints")
    critical = []
    relevant = []
    unresolved = False
    for item in constraints:
        category = item["visual_category"]
        if category not in relevant:
            relevant.append(category)
        if item["decision_critical"] == "是" and category not in critical:
            critical.append(category)
        if item["decision_critical"] == "当前输入不足，无法判断":
            unresolved = True
    if unresolved:
        if (primary is not None or purity is not None or contributing
                or not annotation["human_review_required"]):
            raise ValueError("unresolved strict classification requires human review and null category")
        return
    if not critical:
        raise ValueError("strict classification requires a decision-critical constraint")
    if contributing != critical:
        raise ValueError("contributing visual categories differ from critical constraints")
    if len(critical) >= 2:
        expected_primary, expected_purity = "混合视觉能力", "混合能力题"
    elif len(relevant) == 1:
        expected_primary, expected_purity = critical[0], "单一能力题"
    else:
        expected_primary, expected_purity = critical[0], "主导能力题"
    if primary != expected_primary or purity != expected_purity:
        raise ValueError("primary visual category or purity differs from constraints")


def _resume_contract_compatible(prior: dict, *, run_model: bool,
                                evaluator_contract: dict | None) -> bool:
    """Never reuse a completed result under another provider authorization."""
    capability = prior.get("visual_capability") or {}
    if capability.get("status") != "complete" or not run_model:
        return True
    invocation = capability.get("invocation") or {}
    return invocation.get("evaluator_contract") == evaluator_contract


def validate_classification_run(source_run: Path, manifest_path: Path,
                                _seen: set[Path] | None = None) -> dict:
    """Validate one complete classification ledger and all of its source bindings."""
    source_run, manifest_path = source_run.resolve(), manifest_path.resolve()
    seen = set() if _seen is None else _seen
    if manifest_path in seen:
        raise ValueError("classification resume provenance contains a cycle")
    seen.add(manifest_path)
    value = json.loads(manifest_path.read_text())
    if value.get("schema_version") != "pre-human-review-classification-run-v1":
        raise ValueError("unsupported pre-review classification schema")
    contract_root = manifest_path.parent
    frozen_runner = contract_root / "16_03_04_classifier_runner.py"
    if frozen_runner.is_file():
        if value.get("classification_runner_sha256") != _sha(frozen_runner):
            raise ValueError("classification runner contract changed")
    else:
        # Legacy runs predate the frozen-runner copy. Preserve their opaque
        # runner identity only when the independently constructed authorization
        # proposal binds the same hash; all payload, prompt, schema, provider,
        # source and result hashes are still revalidated below.
        proposal = value.get("authorization_proposal") or {}
        runner_sha = value.get("classification_runner_sha256")
        bound_by_proposal = proposal.get("classification_runner_sha256") == runner_sha
        matches_current = runner_sha == _sha(Path(__file__).resolve())
        if (not re.fullmatch(r"[0-9a-f]{64}", str(runner_sha))
                or not (bound_by_proposal or matches_current)):
            raise ValueError("legacy classification runner identity is unbound")
    source_manifest_path = source_run / "16_03_run_manifest.json"
    source_manifest = json.loads(source_manifest_path.read_text())
    if (Path(value.get("source_run", "")).resolve() != source_run
            or value.get("source_manifest_sha256") != _sha(source_manifest_path)
            or value.get("source_run_id") != source_manifest.get("run_id")):
        raise ValueError("pre-review classifications belong to another source run")
    prompt = contract_root / "16_03_05_visual_capability.system.md"
    schema = contract_root / "16_03_06_visual_capability.schema.json"
    if not prompt.is_file() or value.get("prompt_sha256") != _sha(prompt):
        raise ValueError("classification prompt contract changed")
    if not schema.is_file() or value.get("schema_sha256") != _sha(schema):
        raise ValueError("classification schema contract changed")

    records = value.get("records")
    if not isinstance(records, list):
        raise ValueError("classification records are missing")
    case_ids = [record.get("case_id") for record in records]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("duplicate classification case")
    if case_ids != source_manifest.get("case_ids"):
        raise ValueError("classification case identity/order differs from source run")
    observed_model_contracts = []
    expected_case_bindings = []
    from analysis.scripts.step_16_04_export_human_review import (
        candidate_problem_statement, source_archive_documents)
    for index, record in enumerate(records, 1):
        source_result_path = source_run / f"16_03_result_{index:04d}.json"
        if record.get("source_result_sha256") != _sha(source_result_path):
            raise ValueError("classification source result changed")
        source_result = json.loads(source_result_path.read_text())
        if source_result.get("case_id") != record["case_id"]:
            raise ValueError("classification case differs from source result")
        source_packet_path = Path(source_result["packet"])
        curator_path = Path(source_result["curator_assets"])
        if (source_result.get("packet_sha256") != _sha(source_packet_path)
                or source_result.get("curator_assets_sha256") != _sha(curator_path)):
            raise ValueError("classification source packet or curator assets changed")
        source_packet = json.loads(source_packet_path.read_text())
        if record.get("source_packet_sha256") != source_result.get("packet_sha256"):
            raise ValueError("classification source packet binding changed")
        archive_path = Path(source_packet["provenance"]["source_archive"]).resolve()
        if (record.get("source_archive_sha256") != _sha(archive_path)
                or source_packet["provenance"].get("source_archive_sha256")
                != _sha(archive_path)):
            raise ValueError("classification source archive changed")
        archive = json.loads(archive_path.read_text())
        if record.get("change_scale") != classify_change_scale(
                archive["sections"]["files"]["items"]):
            raise ValueError("classification change scale changed")
        packet_path = Path(record.get("packet", ""))
        if (not packet_path.is_file()
                or record.get("packet_sha256") != _sha(packet_path)):
            raise ValueError("classification packet changed")
        packet = json.loads(packet_path.read_text())
        curator = json.loads(curator_path.read_text())
        curator_assets = [dict(item, display_index=index)
                          for index, item in enumerate(curator["assets"], 1)]
        available = [item for item in curator_assets if item.get("status") == "available"]
        expected_assets = [
            {"asset_id": item["asset_id"], "attachment_index": position,
             "source_ids": item.get("source_ids", [])}
            for position, item in enumerate(available, 1)
        ]
        observed_assets = [
            {key: item.get(key) for key in ("asset_id", "attachment_index", "source_ids")}
            for item in packet.get("assets", [])
        ]
        expected_problem_statement = candidate_problem_statement(
            source_packet, curator_assets, source_archive_documents(source_packet))
        expected_case_bindings.extend(_case_authorization_bindings([{
            "case_id": record["case_id"],
            "result_sha256": record["source_result_sha256"],
            "packet_sha256": record["source_packet_sha256"],
            "packet": source_packet,
            "human_seed": {"problem_statement": expected_problem_statement},
            "assets": curator_assets,
        }]))
        if (packet.get("task_id") != record["case_id"]
                or packet.get("problem_statement") != expected_problem_statement
                or observed_assets != expected_assets):
            raise ValueError("classification packet assets differ from source materials")
        capability = record.get("visual_capability") or {}
        status, annotation = capability.get("status"), capability.get("annotation")
        if status == "complete":
            invocation = capability.get("invocation")
            if not isinstance(invocation, dict):
                raise ValueError("complete classification invocation is missing")
            contract = _validate_evaluator_contract(invocation.get("evaluator_contract"))
            if (invocation.get("backend") != contract["backend"]
                    or invocation.get("requested_model") != contract["requested_model"]
                    or invocation.get("prompt_sha256") != value["prompt_sha256"]
                    or invocation.get("schema_sha256") != value["schema_sha256"]
                    or invocation.get("packet_sha256") != record["packet_sha256"]):
                raise ValueError("classification model or invocation contract changed")
            reused = capability.get("reused_from")
            if reused is not None:
                if not isinstance(reused, dict):
                    raise ValueError("classification reuse provenance is incomplete")
                reused_manifest = Path(reused.get("manifest", "")).resolve()
                if (not reused_manifest.is_file()
                        or reused.get("manifest_sha256") != _sha(reused_manifest)):
                    raise ValueError("classification reused manifest changed")
                validate_classification_run(source_run, reused_manifest, seen)
                trace_root = reused_manifest.parent.resolve()
            else:
                trace_root = manifest_path.parent.resolve()
            trace = _bound_trace_file(
                invocation.get("trace", ""), invocation.get("trace_sha256", ""),
                trace_root, "configuration")
            trace_value = json.loads(trace.read_text())
            if (trace_value.get("backend") != contract["backend"]
                    or trace_value.get("profile") != contract["provider_profile"]
                    or trace_value.get("attempt_limit")
                    != contract["transport_attempt_limit"]
                    or trace_value.get("prompt_sha256") != value["prompt_sha256"]
                    or trace_value.get("schema_sha256") != value["schema_sha256"]
                    or trace_value.get("request_sha256") != invocation.get("request_sha256")):
                raise ValueError("classification invocation trace metadata changed")
            bound_trace_files = {}
            for name in ("request", "raw_response", "provider_response"):
                bound_trace_files[name] = _bound_trace_file(
                    invocation.get(name, ""), invocation.get(f"{name}_sha256", ""),
                    trace_root, name)
            request_value = json.loads(bound_trace_files["request"].read_text())
            _validate_request_semantics(
                request_value, packet, prompt, schema,
                (trace_value.get("profile") or {}).get("protocol"),
                contract["requested_model"], invocation, contract["max_tokens"])
            raw_annotation = json.loads(bound_trace_files["raw_response"].read_text())
            provider_response = json.loads(
                bound_trace_files["provider_response"].read_text())
            attempt_receipt = _bound_trace_file(
                invocation.get("attempt_receipt", ""),
                invocation.get("attempt_receipt_sha256", ""),
                trace_root, "attempt receipt")
            attempt_value = json.loads(attempt_receipt.read_text())
            attempt_number = invocation.get("attempts")
            protocol = (trace_value.get("profile") or {}).get("protocol")
            from pr_crawler.api_engines import extract_annotation
            try:
                extracted = extract_annotation(provider_response, protocol)
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("classification provider response is incomplete") from exc
            if (protocol not in {"chat", "responses"}
                    or provider_response.get("model") != invocation.get("model")
                    or invocation.get("model") not in contract["accepted_response_models"]
                    or not isinstance(attempt_number, int)
                    or not 1 <= attempt_number <= contract["transport_attempt_limit"]
                    or attempt_receipt.name != f"10_attempt_{attempt_number:02d}.json"
                    or attempt_value.get("status") != "received"
                    or attempt_value.get("response_sha256")
                    != invocation.get("provider_response_sha256")
                    or raw_annotation != annotation or extracted != annotation):
                raise ValueError("classification provider response or annotation changed")
            if contract not in observed_model_contracts:
                observed_model_contracts.append(contract)
            _validate_visual(annotation, packet, schema)
        elif status in {"prepared", "ineligible", "requires_video_review", "failed"}:
            if annotation is not None:
                raise ValueError("non-complete classification contains an annotation")
        else:
            raise ValueError("unsupported visual capability status")
    expected_ready = bool(records) and all(
        record["change_scale"]["label"] != "无法分类"
        and record["visual_capability"]["status"] in {"complete", "ineligible"}
        for record in records)
    if value.get("human_review_ready") != expected_ready:
        raise ValueError("classification human-review readiness changed")
    if value.get("model_contracts") != observed_model_contracts:
        raise ValueError("classification model contracts changed")
    proposal = value.get("authorization_proposal")
    if proposal is not None:
        proposal_contract = _validate_evaluator_contract(
            proposal.get("evaluator_contract"))
        expected_proposal = {
            "schema_version": "classification-run-authorization-proposal-v1",
            "source_run": str(source_run),
            "source_manifest_sha256": value["source_manifest_sha256"],
            "prompt_sha256": value["prompt_sha256"],
            "schema_sha256": value["schema_sha256"],
            "classification_runner_sha256": value["classification_runner_sha256"],
            "run_id": proposal.get("run_id"),
            "nonce": proposal.get("nonce"),
            "expires_at": proposal.get("expires_at"),
            "canonical_output": proposal.get("canonical_output"),
            "evaluator_contract": proposal_contract,
            "case_bindings": expected_case_bindings,
            "expected_case_calls": sum(
                item["eligible_for_model_call"] for item in expected_case_bindings),
            "semantic_attempt_limit_per_case": 2,
            "transport_attempt_limit_per_semantic_call": proposal_contract[
                "transport_attempt_limit"],
            "maximum_api_requests": sum(
                item["eligible_for_model_call"] for item in expected_case_bindings)
                * 2 * proposal.get("transport_attempt_limit_per_semantic_call", 0),
        }
        if proposal != expected_proposal:
            schema_id = json.loads(schema.read_text()).get("$id")
            legacy_v3 = schema_id == "visual-capability-classifier-v3"
            actual_bindings = proposal.get("case_bindings") or []
            expected_bindings = expected_proposal["case_bindings"]
            actual_by_case = {item.get("case_id"): item for item in actual_bindings}
            expected_by_case = {item.get("case_id"): item for item in expected_bindings}
            complete_cases = {
                item["case_id"] for item in records
                if (item.get("visual_capability") or {}).get("status") == "complete"
            }
            completed_bindings_unchanged = all(
                actual_by_case.get(case_id) == expected_by_case.get(case_id)
                and (actual_by_case.get(case_id) or {}).get("eligible_for_model_call") is True
                for case_id in complete_cases
            )
            # V3 proposals may differ today only for cases that were never
            # called: duplicate-media normalization was deliberately relaxed
            # after those runs. Preserve the originally authorized proposal,
            # while requiring every completed/counted call binding to be
            # byte-for-byte reconstructable under the current validator.
            if (not legacy_v3 or not completed_bindings_unchanged
                    or [item.get("case_id") for item in actual_bindings] != case_ids):
                differing = sorted(key for key in set(proposal) | set(expected_proposal)
                                   if proposal.get(key) != expected_proposal.get(key))
                raise ValueError("classification authorization proposal changed: "
                                 + ",".join(differing))
    authorization = value.get("run_authorization")
    if value.get("model_invoked"):
        if not isinstance(proposal, dict) or not isinstance(authorization, dict):
            raise ValueError("classification run authorization evidence is missing")
        authorization_path = Path(authorization.get("path", ""))
        if (authorization_path.is_symlink() or not authorization_path.is_file()
                or authorization.get("sha256") != _sha(authorization_path)):
            raise ValueError("classification run authorization changed")
        expected_authorization = dict(proposal)
        expected_authorization.update({
            "schema_version": "classification-run-authorization-v1", "authorized": True})
        if json.loads(authorization_path.read_text()) != expected_authorization:
            raise ValueError("classification run authorization binding changed")
        receipt_binding = authorization.get("receipt") or {}
        receipt_path = Path(receipt_binding.get("path", ""))
        receipt_root = (REPORT_ROOT / "evidence/classification_authorization_receipts").resolve()
        if (receipt_path.is_symlink() or not receipt_path.is_file()
                or receipt_path.resolve().parent != receipt_root
                or receipt_binding.get("sha256") != _sha(receipt_path)):
            raise ValueError("classification authorization receipt changed")
        receipt = json.loads(receipt_path.read_text())
        if (receipt.get("schema_version") != "classification-authorization-receipt-v1"
                or receipt.get("status") != "consumed"
                or any(receipt.get(key) != proposal.get(key)
                       for key in ("run_id", "nonce", "expires_at", "canonical_output"))
                or receipt.get("authorization") != {
                    "path": str(authorization_path.resolve()),
                    "sha256": _sha(authorization_path)}):
            raise ValueError("classification authorization receipt semantics changed")
    elif authorization is not None:
        raise ValueError("dry classification run must not contain authorization")
    seen.remove(manifest_path)
    return value


def _video_duration(ffmpeg: Path, source: Path) -> tuple[float, str, str]:
    result = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-i", str(source)],
        capture_output=True, text=True, timeout=30, check=False,
    )
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
    method = "container_metadata"
    if not match:
        # MediaRecorder WebM files commonly omit container duration. Decode the
        # video stream to a null sink and use ffmpeg's final progress timestamp;
        # this remains bounded and uses the same frozen binary as frame sampling.
        decoded = subprocess.run(
            [str(ffmpeg), "-hide_banner", "-i", str(source), "-map", "0:v:0",
             "-an", "-f", "null", "-"],
            capture_output=True, text=True, timeout=120, check=False,
        )
        progress = re.findall(
            r"time=\s*(\d+):(\d+):(\d+(?:\.\d+)?)", decoded.stderr)
        if not progress:
            raise ValueError("video duration is unavailable")
        match_groups = progress[-1]
        method = "decoded_final_timestamp"
    else:
        match_groups = match.groups()
    hours, minutes, seconds = match_groups
    duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    if not 0 < duration <= 1800:
        raise ValueError("video duration is outside the 30-minute safety bound")
    version = subprocess.run(
        [str(ffmpeg), "-version"], capture_output=True, text=True,
        timeout=10, check=True,
    ).stdout.splitlines()[0]
    return duration, version, method


def _prepare_video_contact_sheet(source: Path, target: Path) -> tuple[Path, dict]:
    """Create a deterministic, curator-only six-frame temporal representation."""
    from PIL import Image, ImageOps

    configured = os.environ.get(VIDEO_FFMPEG_ENV)
    executable = Path(configured).expanduser() if configured else None
    if executable is None:
        discovered = shutil.which("ffmpeg")
        executable = Path(discovered) if discovered else None
    if executable is None:
        raise FileNotFoundError(
            f"ffmpeg is unavailable; set {VIDEO_FFMPEG_ENV} to a frozen executable")
    executable = executable.resolve(strict=True)
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError("configured ffmpeg is not an executable file")

    duration, version, duration_probe_method = _video_duration(executable, source)
    timestamps = [duration * index / VIDEO_SAMPLE_COUNT
                  for index in range(VIDEO_SAMPLE_COUNT)]
    frame_root = target.parent / (target.stem + "_frames")
    frame_root.mkdir(parents=True, exist_ok=False)
    frames = []
    try:
        for index, timestamp in enumerate(timestamps, 1):
            frame_path = frame_root / f"frame_{index:02d}.png"
            subprocess.run([
                str(executable), "-loglevel", "error", "-ss", f"{timestamp:.6f}",
                "-i", str(source), "-frames:v", "1", "-vf",
                "scale=640:640:force_original_aspect_ratio=decrease",
                "-y", str(frame_path),
            ], capture_output=True, timeout=60, check=True)
            with Image.open(frame_path) as image:
                frames.append(ImageOps.exif_transpose(image).convert("RGB"))
        cell_width = max(image.width for image in frames)
        cell_height = max(image.height for image in frames)
        margin, gap, columns = 8, 8, 3
        rows = (len(frames) + columns - 1) // columns
        sheet = Image.new(
            "RGB",
            (2 * margin + columns * cell_width + (columns - 1) * gap,
             2 * margin + rows * cell_height + (rows - 1) * gap),
            "white",
        )
        for index, frame in enumerate(frames):
            x = margin + (index % columns) * (cell_width + gap)
            y = margin + (index // columns) * (cell_height + gap)
            sheet.paste(frame, (x, y))
        target.parent.mkdir(parents=True, exist_ok=True)
        sheet.save(target, format="PNG", optimize=False)
    finally:
        shutil.rmtree(frame_root, ignore_errors=True)
    return target, {
        "kind": "video_contact_sheet",
        "source_sha256": _sha(source),
        "derived_sha256": _sha(target),
        "duration_seconds": round(duration, 6),
        "sampled_timestamps_seconds": [round(value, 6) for value in timestamps],
        "layout": {"order": "left_to_right_top_to_bottom", "columns": 3, "rows": 2},
        "ffmpeg_sha256": _sha(executable),
        "ffmpeg_version": version,
        "duration_probe_method": duration_probe_method,
    }


def _prepare_model_image(path: Path, destination: Path) -> tuple[Path | None, dict]:
    """Return one static model image per original asset, preserving its derivation."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    path = path.resolve()
    if path.stat().st_size > MAX_MEDIA_BYTES:
        return None, {"kind": "decode_budget_exceeded",
                      "source_size_bytes": path.stat().st_size,
                      "budget": "encoded_bytes"}
    source_sha256 = _sha(path)
    try:
        with Image.open(path) as image:
            frame_count = int(getattr(image, "n_frames", 1))
            if (image.width * image.height > MAX_IMAGE_PIXELS
                    or frame_count > MAX_ANIMATION_FRAMES):
                return None, {"kind": "decode_budget_exceeded",
                              "source_sha256": source_sha256,
                              "budget": "pixels_or_frames",
                              "width": image.width, "height": image.height,
                              "frame_count": frame_count}
            if image.format in {"PNG", "JPEG", "WEBP"} and frame_count == 1:
                image.verify()
                return path, {"kind": "original_static_image",
                              "source_sha256": source_sha256}
            if image.format != "GIF":
                return None, {"kind": "unsupported_media",
                              "source_sha256": source_sha256,
                              "detected_format": image.format}
            indices = sorted({0, frame_count // 2, frame_count - 1})
            frames = []
            for index in indices:
                image.seek(index)
                frame = ImageOps.exif_transpose(image.convert("RGBA"))
                background = Image.new("RGB", frame.size, "white")
                background.paste(frame, mask=frame.getchannel("A"))
                background.thumbnail((768, 768))
                frames.append(background)
    except Image.DecompressionBombError:
        return None, {"kind": "decode_budget_exceeded",
                      "source_sha256": source_sha256, "budget": "pillow_bomb_limit"}
    except UnidentifiedImageError:
        try:
            return _prepare_video_contact_sheet(path, destination)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return None, {"kind": "unsupported_media", "source_sha256": source_sha256,
                          "detected_format": None,
                          "video_derivation_error": type(exc).__name__}

    width = sum(frame.width for frame in frames) + 8 * (len(frames) - 1)
    height = max(frame.height for frame in frames)
    sheet = Image.new("RGB", (width, height), "white")
    x = 0
    for frame in frames:
        sheet.paste(frame, (x, 0))
        x += frame.width + 8
    destination.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(destination, format="PNG", optimize=True)
    return destination, {
        "kind": "animated_gif_contact_sheet",
        "source_sha256": source_sha256,
        "frame_count": frame_count,
        "sampled_frame_indices": indices,
        "derived_sha256": _sha(destination),
    }


def _bound_media_path(asset: dict, archive_path: Path, archive: dict) -> Path:
    """Resolve one available media asset only through its archived hash binding."""
    archive_items = archive["sections"]["assets"]["items"]
    matches = [item for item in archive_items if item.get("sha256") == asset.get("asset_id")]
    if not matches:
        raise _SourceMediaRecordError(
            "media asset is missing from source archive: "
            f"asset_id={asset.get('asset_id')} matches=0")
    logical_root = archive_path.parent / "11_http_archive"
    if logical_root.is_symlink():
        raise ValueError("archive media root must not be a symlink")
    resolved_root = logical_root.resolve(strict=True)
    supplied = Path(asset.get("local_path", ""))
    if supplied.is_symlink():
        raise ValueError("available media path contains a symlink")
    if not supplied.is_absolute():
        raise ValueError("available media path differs from archived media")
    supplied_resolved = supplied.resolve(strict=True)
    resolved_matches = []
    for archived in matches:
        if archived.get("status") != "complete":
            continue
        relative = Path(archived.get("local_path", ""))
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise ValueError("unsafe archived media path")
        logical = logical_root / relative
        current = logical_root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("archive media path contains a symlink")
        resolved = logical.resolve(strict=True)
        if not resolved.is_relative_to(resolved_root) or not resolved.is_file():
            raise ValueError("archive media path escapes its archive root or is not a file")
        if resolved == supplied_resolved:
            resolved_matches.append(resolved)
    # The same bytes may be referenced by multiple Issue/PR occurrences.  The
    # solver-input asset already binds one concrete archived path, so repeated
    # SHA records are normalized by selecting that exact path.
    if not resolved_matches:
        raise ValueError("available media path differs from archived media")
    resolved = resolved_matches[0]
    if resolved.stat().st_size > MAX_MEDIA_BYTES:
        raise ValueError("archived media exceeds encoded-byte budget")
    if _sha(resolved) != asset.get("asset_id"):
        raise ValueError("archived media hash changed")
    return resolved


def _resolve_available_media(assets: list[dict], archive_path: Path,
                             archive: dict) -> tuple[list[Path], str | None]:
    """Resolve all model-visible media, returning an auditable per-case error."""
    available = [asset for asset in assets if asset.get("status") == "available"]
    try:
        return ([_bound_media_path(asset, archive_path, archive)
                 for asset in available], None)
    except _SourceMediaRecordError as exc:
        return [], f"{type(exc).__name__}: {str(exc)[:1200]}"


def run(source_run: Path, output: Path, *, run_model: bool = False,
        evaluator=None, timeout: int = 480, resume_from: Path | None = None,
        authorization_path: Path | None = None,
        authorization_identity: dict | None = None,
        canonical_output: Path | None = None) -> dict:
    """Create a bound pre-review classification record for every source case."""
    from analysis.scripts.step_16_04_export_human_review import load_rows

    source_run, output = source_run.resolve(), output.resolve()
    if output.exists():
        raise ValueError(f"output already exists: {output}")
    if run_model and evaluator is None:
        raise ValueError("run_model requires an evaluator")
    evaluator_contract = _evaluator_contract(evaluator) if evaluator is not None else None
    if authorization_path is not None and not run_model:
        raise ValueError("classification authorization is only valid with run_model")
    if run_model:
        resolved_authorization, bound_identity = _read_authorization_identity(
            authorization_path, output)
    elif evaluator_contract is not None:
        if authorization_identity is None or canonical_output is None:
            raise ValueError("classification proposal requires authorization identity and output")
        resolved_authorization = None
        bound_identity = _validate_authorization_identity(
            authorization_identity, canonical_output)
    else:
        resolved_authorization = None
        bound_identity = None
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    output.mkdir(parents=True)
    try:
        return _run_created(source_run, output, run_model=run_model,
                            evaluator=evaluator, timeout=timeout,
                            resume_from=resume_from, load_rows=load_rows,
                            evaluator_contract=evaluator_contract,
                            authorization_path=resolved_authorization,
                            authorization_identity=bound_identity)
    except BaseException:
        shutil.rmtree(output, ignore_errors=True)
        raise


def _run_created(source_run: Path, output: Path, *, run_model: bool, evaluator,
                 timeout: int, resume_from: Path | None, load_rows,
                 evaluator_contract: dict | None,
                 authorization_path: Path | None,
                 authorization_identity: dict | None) -> dict:
    """Populate an already-created output directory; ``run`` owns rollback."""
    shutil.copyfile(Path(__file__), output / "16_03_04_classifier_runner.py")
    shutil.copyfile(PROMPT, output / "16_03_05_visual_capability.system.md")
    shutil.copyfile(SCHEMA, output / "16_03_06_visual_capability.schema.json")
    source_manifest, rows = load_rows(source_run)
    attempts = int(getattr(evaluator, "attempts", 1)) if evaluator is not None else 1
    authorization_proposal = (_authorization_proposal(
        source_run, rows, evaluator_contract, attempts, authorization_identity)
        if evaluator_contract is not None else None)
    authorization = (_validate_authorization(authorization_path, authorization_proposal)
                     if run_model else None)
    model_invoked = False
    previous = {}
    if resume_from is not None:
        resume_from = resume_from.resolve()
        previous_manifest = validate_classification_run(source_run, resume_from)
        previous = {record["case_id"]: record for record in previous_manifest["records"]}
    records = []
    for index, row in enumerate(rows, 1):
        archive_path = Path(row["packet"]["provenance"]["source_archive"]).resolve(
            strict=True)
        archive = json.loads(archive_path.read_text())
        change_scale = classify_change_scale(archive["sections"]["files"]["items"])
        available = [asset for asset in row["assets"] if asset.get("status") == "available"]
        media_paths, media_error = _resolve_available_media(
            row["assets"], archive_path, archive)
        packet = {
            "task_id": row["case_id"],
            "problem_statement": row["human_seed"]["problem_statement"],
            "assets": [
                {"asset_id": asset["asset_id"], "attachment_index": position,
                 "source_ids": asset.get("source_ids", [])}
                for position, asset in enumerate(available, 1)
            ],
        }
        packet_path = output / f"16_03_07_packet_{index:04d}.json"
        _write(packet_path, packet)
        record = {
            "case_id": row["case_id"],
            "source_result_sha256": row["result_sha256"],
            "source_packet_sha256": row["packet_sha256"],
            "source_archive_sha256": _sha(archive_path),
            "change_scale": change_scale,
            "visual_capability": {"status": "prepared", "annotation": None, "invocation": None},
            "packet": str(packet_path),
            "packet_sha256": _sha(packet_path),
        }
        prior = previous.get(row["case_id"])
        if (prior and prior.get("source_result_sha256") == record["source_result_sha256"]
                and prior.get("source_packet_sha256") == record["source_packet_sha256"]
                and prior.get("source_archive_sha256") == record["source_archive_sha256"]
                and prior.get("visual_capability", {}).get("status")
                in RESUMABLE_FINAL_CAPABILITY_STATUSES
                and _resume_contract_compatible(
                    prior, run_model=run_model,
                    evaluator_contract=evaluator_contract)):
            prior_packet = json.loads(Path(prior["packet"]).read_text())
            comparable_prior = json.loads(json.dumps(prior_packet))
            for asset in comparable_prior.get("assets", []):
                asset.pop("model_input_representation", None)
            if comparable_prior != packet:
                raise ValueError("reused classification packet differs from current source")
            shutil.copyfile(Path(prior["packet"]), packet_path)
            record["packet_sha256"] = _sha(packet_path)
            record["visual_capability"] = json.loads(json.dumps(prior["visual_capability"]))
            record["visual_capability"]["reused_from"] = {
                "manifest": str(resume_from.resolve()),
                "manifest_sha256": _sha(resume_from.resolve()),
            }
            records.append(record)
            continue
        if media_error is not None:
            record["visual_capability"] = {
                "status": "failed",
                "annotation": None,
                "invocation": None,
                "reason": "source_media_binding_error: " + media_error,
            }
        elif not packet["problem_statement"].strip() or not available:
            record["visual_capability"] = {
                "status": "ineligible",
                "annotation": None,
                "invocation": None,
                "reason": "missing_problem_statement_or_available_solver_visible_image",
            }
        elif run_model:
            work = output / f"16_03_07_call_{index:04d}"
            work.mkdir()
            try:
                model_images, representations = [], []
                for position, (asset, media_path) in enumerate(
                        zip(available, media_paths), 1):
                    prepared, representation = _prepare_model_image(
                        media_path,
                        work / "16_03_07_model_inputs" / f"asset_{position:02d}.png")
                    representations.append(representation)
                    if prepared is not None:
                        model_images.append(prepared)
                for asset, representation in zip(packet["assets"], representations):
                    asset["model_input_representation"] = representation
                _write(packet_path, packet)
                record["packet_sha256"] = _sha(packet_path)
                if len(model_images) != len(available):
                    record["visual_capability"] = {
                        "status": "requires_video_review", "annotation": None, "invocation": None,
                        "reason": "one_or_more_assets_cannot_be_represented_as_audited_static_images",
                    }
                    records.append(record)
                    continue
                validation_failures = []
                for semantic_attempt in (1, 2):
                    attempt_packet = dict(packet)
                    if validation_failures:
                        attempt_packet["previous_output_validation_error"] = validation_failures[-1]
                    attempt_work = work / f"semantic_attempt_{semantic_attempt:02d}"
                    attempt_work.mkdir()
                    if not model_invoked:
                        if authorization is None:
                            raise ValueError("classification model invocation lacks authorization")
                        try:
                            authorization["receipt"] = _consume_authorization(authorization)
                        except ValueError as exc:
                            raise _AuthorizationConsumptionError(str(exc)) from exc
                        model_invoked = True
                    annotation, invocation = evaluator(
                        packet=attempt_packet,
                        image_paths=model_images,
                        system_prompt=output / "16_03_05_visual_capability.system.md",
                        schema=output / "16_03_06_visual_capability.schema.json",
                        workdir=attempt_work,
                        timeout=timeout,
                    )
                    try:
                        _validate_visual(annotation, packet, output / "16_03_06_visual_capability.schema.json")
                        break
                    except Exception as exc:
                        validation_failures.append(f"{type(exc).__name__}: {str(exc)[:1200]}")
                        if semantic_attempt == 2:
                            raise
                invocation["semantic_validation_attempts"] = semantic_attempt
                invocation["prior_validation_failures"] = validation_failures
                trace = attempt_work / "10_api_invocation.json"
                if not trace.is_file():
                    raise ValueError("evaluator did not retain its invocation trace")
                attempt_number = invocation.get("attempts")
                if not isinstance(attempt_number, int) or attempt_number < 1:
                    raise ValueError("evaluator did not retain its successful attempt receipt")
                attempt_receipt = attempt_work / f"10_attempt_{attempt_number:02d}.json"
                if not attempt_receipt.is_file():
                    raise ValueError("evaluator did not retain its successful attempt receipt")
                invocation.update({
                    "evaluator_contract": evaluator_contract,
                    "prompt_sha256": _sha(output / "16_03_05_visual_capability.system.md"),
                    "schema_sha256": _sha(output / "16_03_06_visual_capability.schema.json"),
                    "packet_sha256": record["packet_sha256"],
                    "trace": str(trace.resolve()),
                    "trace_sha256": _sha(trace),
                    "attempt_receipt": str(attempt_receipt.resolve()),
                    "attempt_receipt_sha256": _sha(attempt_receipt),
                })
                record["visual_capability"] = {
                    "status": "complete", "annotation": annotation, "invocation": invocation}
            except _AuthorizationConsumptionError:
                raise
            except Exception as exc:
                record["visual_capability"] = {
                    "status": "failed", "annotation": None, "invocation": None,
                    "reason": f"{type(exc).__name__}: {str(exc)[:1200]}",
                }
        records.append(record)
    manifest = {
        "schema_version": "pre-human-review-classification-run-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_run": str(source_run),
        "source_manifest_sha256": _sha(source_run / "16_03_run_manifest.json"),
        "source_run_id": source_manifest["run_id"],
        "model_invoked": model_invoked,
        "authorization_proposal": authorization_proposal,
        "run_authorization": authorization if model_invoked else None,
        "classification_runner_sha256": _sha(output / "16_03_04_classifier_runner.py"),
        "prompt_sha256": _sha(output / "16_03_05_visual_capability.system.md"),
        "schema_sha256": _sha(output / "16_03_06_visual_capability.schema.json"),
        "records": records,
        "model_contracts": [],
        "human_review_ready": all(
            record["change_scale"]["label"] != "无法分类"
            and record["visual_capability"]["status"] in {"complete", "ineligible"}
            for record in records
        ),
    }
    encoded_contracts = []
    for record in records:
        invocation = record["visual_capability"].get("invocation") or {}
        contract = invocation.get("evaluator_contract")
        encoded = json.dumps(contract, sort_keys=True) if contract else None
        if encoded and encoded not in encoded_contracts:
            encoded_contracts.append(encoded)
            manifest["model_contracts"].append(contract)
    _write(output / "16_03_08_pre_review_classifications.json", manifest)
    return manifest


def load_for_source(source_run: Path, path: Path | None = None) -> dict[str, dict]:
    """Load and verify a classification run bound to ``source_run``."""
    candidate = path or source_run / "16_03_08_pre_review_classifications.json"
    if not candidate.exists():
        return {}
    value = validate_classification_run(source_run, candidate)
    return {record["case_id"]: record for record in value["records"]}
