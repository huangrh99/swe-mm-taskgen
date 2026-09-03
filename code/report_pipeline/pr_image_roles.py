"""Classify archived Issue/PR images by chronology, role, and leakage risk.

This curator-side stage may inspect PR prose, but its outputs never become
solver-visible without the existing human multimodal-necessity gate.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import shutil
import subprocess

from report_pipeline.atomic import write_json
from report_pipeline.paths import CODE_ROOT
from report_pipeline.pre_review_classification import (
    _evaluator_contract,
    _prepare_model_image,
    _prepare_video_contact_sheet,
    _validate_evaluator_contract,
)


PROMPT = CODE_ROOT / "analysis/prompts/08_04_pr_image_role_leakage.system.md"
SCHEMA = CODE_ROOT / "analysis/prompts/08_05_pr_image_role_leakage.schema.json"
RUNNER_VERSION = "pr-image-role-run-v1"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _identity_hash(value: str) -> str:
    return hashlib.sha256(("unavailable-image-v1\0" + value).encode()).hexdigest()


def _source_kind(source_id: str) -> str:
    lowered = source_id.lower()
    if lowered.startswith("issue:") or ("#" in source_id and lowered.endswith(":body")):
        return "issue"
    if lowered.startswith(("pr:", "comments:", "review", "pull:")):
        return "pr"
    return "unknown"


def _documents(archive: dict) -> list[dict]:
    """Keep curator prose, never patch/diff/test content, in stable source order."""
    media_sources = {
        occurrence.get("source_id")
        for item in archive.get("archival_view", {}).get("media", [])
        for occurrence in item.get("occurrences", [])
        if occurrence.get("source_id")
    }
    documents = []
    for item in archive.get("archival_view", {}).get("documents", []):
        source_id = item.get("source_id", "")
        kind = item.get("kind")
        if not (source_id in media_sources or kind in {"pr", "issue"}):
            continue
        source_kind = "issue" if kind == "issue" else "pr"
        documents.append({
            "source_id": source_id, "source_kind": source_kind,
            "field": item.get("field"), "url": item.get("url"),
            "text": item.get("text") or "", "text_sha256": item.get("text_sha256"),
        })
    return documents


def _download_path(archive_path: Path, item: dict) -> Path | None:
    if item.get("status") != "complete" or not item.get("local_path"):
        return None
    relative = Path(item["local_path"])
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("unsafe archived asset path")
    root = archive_path.parent / "11_http_archive"
    path = root / relative
    if root.is_symlink() or path.is_symlink():
        raise ValueError("archived asset path must not be a symlink")
    resolved = path.resolve(strict=True)
    if not resolved.is_file() or not resolved.is_relative_to(root.resolve(strict=True)):
        raise ValueError("archived asset escapes its run")
    if _sha(resolved) != item.get("sha256"):
        raise ValueError("archived asset hash changed")
    return resolved


def build_packet(archive_path: Path, model_input_dir: Path) -> tuple[dict, list[Path]]:
    archive_path = archive_path.resolve(strict=True)
    archive = json.loads(archive_path.read_text())
    if archive.get("schema_version") != 1 or not archive.get("instance_id"):
        raise ValueError("unsupported source archive")
    media = archive.get("archival_view", {}).get("media", [])
    by_url = {item.get("url"): item for item in media if item.get("url")}
    groups: dict[str, list[dict]] = defaultdict(list)
    for item in archive.get("sections", {}).get("assets", {}).get("items", []):
        key = item.get("sha256") if item.get("status") == "complete" else _identity_hash(item.get("url", ""))
        groups[key].append(item)

    assets, image_paths = [], []
    for position, (asset_id, items) in enumerate(groups.items(), 1):
        occurrences, urls, source_ids, origins = [], [], [], []
        for item in items:
            url = item.get("url")
            if url and url not in urls:
                urls.append(url)
            visual = by_url.get(url, {})
            for occurrence in visual.get("occurrences", []):
                occurrence = dict(occurrence)
                if occurrence not in occurrences:
                    occurrences.append(occurrence)
                source_id = occurrence.get("source_id", "")
                if source_id and source_id not in source_ids:
                    source_ids.append(source_id)
            for source_id in item.get("sources", []):
                if source_id not in source_ids:
                    source_ids.append(source_id)
        for source_id in source_ids:
            kind = _source_kind(source_id)
            if kind not in origins:
                origins.append(kind)

        complete = [item for item in items if item.get("status") == "complete"]
        path = _download_path(archive_path, complete[0]) if complete else None
        media_types = sorted({str(item.get("media_type")) for item in items if item.get("media_type")})
        representation = {"kind": "unavailable", "reason": "download_or_parse_pending"}
        attached_index = None
        if path is not None and any(value.startswith("image/") for value in media_types):
            prepared, representation = _prepare_model_image(
                path, model_input_dir / f"08_04_02_asset_{position:03d}.png")
            if prepared is not None:
                image_paths.append(prepared)
                attached_index = len(image_paths)
        elif path is not None and any(value.startswith("video/") for value in media_types):
            try:
                prepared, representation = _prepare_video_contact_sheet(
                    path, model_input_dir / f"08_04_02_video_{position:03d}.png")
                image_paths.append(prepared)
                attached_index = len(image_paths)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                representation = {
                    "kind": "video_requires_review", "source_sha256": asset_id,
                    "failure_class": type(exc).__name__, "reason": str(exc)[:400],
                }

        assets.append({
            "asset_id": asset_id, "content_sha256": asset_id if complete else None,
            "normalized_duplicate_count": len(items), "urls": urls,
            "source_ids": source_ids, "origin_kinds": origins,
            "occurrences": occurrences, "media_types": media_types,
            "download_statuses": sorted({str(item.get("status")) for item in items}),
            "attachment_index": attached_index,
            "model_input_representation": representation,
        })

    packet = {
        "schema_version": "pr-image-role-packet-v1",
        "case_id": archive["instance_id"], "repo": archive.get("repo"),
        "number": archive.get("number"),
        "source_archive": str(archive_path), "source_archive_sha256": _sha(archive_path),
        "source_archive_status": archive.get("status"),
        "source_documents": _documents(archive), "assets": assets,
        "boundary": {
            "curator_only": True, "contains_pr_solution_prose": True,
            "patch_diff_commits_tests_omitted": True,
            "verifier_cannot_grant_solver_visibility": True,
            "all_solver_visible_assets_require_human_gate": True,
        },
    }
    return packet, image_paths


def validate(annotation: dict, packet: dict, schema: Path = SCHEMA) -> None:
    import jsonschema
    jsonschema.validate(annotation, json.loads(schema.read_text()))
    if annotation.get("case_id") != packet.get("case_id"):
        raise ValueError("image-role case identity mismatch")
    ids = [asset["asset_id"] for asset in packet["assets"]]
    images = annotation.get("images", [])
    if [item.get("asset_id") for item in images] != ids or len(ids) != len(set(ids)):
        raise ValueError("image-role asset order/coverage mismatch")
    before, curator, crop, retry, video = [], [], [], [], []
    policy_errors = []
    packet_by_id = {item["asset_id"]: item for item in packet["assets"]}
    for item in images:
        source = packet_by_id[item["asset_id"]]
        recommendation = item["agent_visibility_recommendation"]
        attached = source.get("attachment_index") is not None
        if not attached:
            if item["observed"] or recommendation != "retry_or_video_review":
                policy_errors.append(f"{item['asset_id'][:12]}:unattached_requires_retry_or_video")
            representation = source.get("model_input_representation", {}).get("kind")
            (video if representation == "video_requires_review" else retry).append(item["asset_id"])
        elif not item["observed"]:
            if recommendation != "retry_or_video_review":
                policy_errors.append(f"{item['asset_id'][:12]}:unobserved_requires_retry")
            retry.append(item["asset_id"])
        if recommendation == "recommend_before_candidate":
            if (item["role"] not in {"before_only", "temporal_sequence"}
                    or item["shows_actual_bug"] != "yes"
                    or item["contains_fixed_after"] != "no"
                    or item["contains_solution_evidence"] != "no"
                    or item["task_relationship"] != "explicit"
                    or not item["requires_human_review"]):
                policy_errors.append(f"{item['asset_id'][:12]}:before_policy_requires_role_bug_no_leak_explicit_and_human_true")
            before.append(item["asset_id"])
        elif recommendation == "exclude":
            curator.append(item["asset_id"])
        elif recommendation == "crop_then_review":
            box = item["crop"]["normalized_box"]
            if (item["role"] != "before_after_composite" or not item["crop"]["needed"]
                    or item["crop"]["feasible"] != "yes" or box is None
                    or box[0] + box[2] > 1.000001 or box[1] + box[3] > 1.000001
                    or not item["requires_human_review"]):
                policy_errors.append(f"{item['asset_id'][:12]}:crop_policy_requires_separable_composite_and_human_true")
            crop.append(item["asset_id"])
        elif recommendation == "human_review" and not item["requires_human_review"]:
            policy_errors.append(f"{item['asset_id'][:12]}:human_review_requires_human_true")
    for field, expected in (("before_candidate_asset_ids", before),
                            ("curator_only_asset_ids", curator),
                            ("crop_review_asset_ids", crop),
                            ("retry_asset_ids", retry),
                            ("video_review_asset_ids", video)):
        if annotation[field] != expected:
            policy_errors.append(f"{field}:must_equal_{expected}")

    candidate_origin_sets = {
        asset_id: set(packet_by_id[asset_id].get("origin_kinds", [])) for asset_id in before}
    issue_candidates = {asset_id for asset_id, origins in candidate_origin_sets.items()
                        if "issue" in origins}
    pr_only_candidates = {asset_id for asset_id, origins in candidate_origin_sets.items()
                          if "pr" in origins and "issue" not in origins}
    path = annotation["source_path_recommendation"]
    if path == "issue_derived" and not issue_candidates:
        policy_errors.append("source_path:issue_derived_requires_issue_before")
    if path == "pr_derived" and not pr_only_candidates:
        policy_errors.append("source_path:pr_derived_requires_pr_before")
    if path == "both" and not (issue_candidates and pr_only_candidates):
        policy_errors.append("source_path:both_requires_distinct_issue_and_pr_before_candidates")
    if path == "no_candidate" and before:
        policy_errors.append("source_path:no_candidate_contradicts_before_candidates")
    expected_action = {
        "issue_derived": "use_issue_text", "pr_derived": "draft_pr_derived",
        "both": "human_review", "no_candidate": "unavailable",
    }[path]
    if annotation["problem_statement_action"] != expected_action:
        policy_errors.append(f"problem_statement_action:must_be_{expected_action}_for_{path}")
    if policy_errors:
        raise ValueError("; ".join(policy_errors))


def _canonicalize_routing_lists(annotation: dict, packet: dict) -> dict:
    """Derive aggregate asset queues from the model's per-image decisions."""
    annotation = json.loads(json.dumps(annotation))
    packet_by_id = {item["asset_id"]: item for item in packet["assets"]}
    queues = {
        "before_candidate_asset_ids": [], "curator_only_asset_ids": [],
        "crop_review_asset_ids": [], "retry_asset_ids": [],
        "video_review_asset_ids": [],
    }
    for item in annotation.get("images", []):
        asset_id = item.get("asset_id")
        source = packet_by_id.get(asset_id)
        if source is None:
            continue
        recommendation = item.get("agent_visibility_recommendation")
        attached = source.get("attachment_index") is not None
        if not attached:
            representation = source.get("model_input_representation", {}).get("kind")
            field = ("video_review_asset_ids" if representation == "video_requires_review"
                     else "retry_asset_ids")
            queues[field].append(asset_id)
        elif not item.get("observed"):
            queues["retry_asset_ids"].append(asset_id)
        if recommendation == "recommend_before_candidate":
            queues["before_candidate_asset_ids"].append(asset_id)
        elif recommendation == "exclude":
            queues["curator_only_asset_ids"].append(asset_id)
        elif recommendation == "crop_then_review":
            queues["crop_review_asset_ids"].append(asset_id)
    annotation.update(queues)
    return annotation


def _unobservable_annotation(packet: dict) -> dict:
    images, retry_ids, video_ids = [], [], []
    for asset in packet["assets"]:
        representation = asset.get("model_input_representation", {}).get("kind")
        is_video = representation == "video_requires_review"
        (video_ids if is_video else retry_ids).append(asset["asset_id"])
        images.append({
            "asset_id": asset["asset_id"], "observed": False, "role": "unclear",
            "role_evidence": "No solver-usable pixels were available to this stage.",
            "shows_actual_bug": "unknown", "contains_fixed_after": "unknown",
            "contains_solution_evidence": "unknown", "task_relationship": "unknown",
            "agent_visibility_recommendation": "retry_or_video_review",
            "crop": {"needed": False, "feasible": "unknown", "normalized_box": None,
                     "reason": "Pixels are unavailable or require video review."},
            "requires_human_review": True,
            "reason": "Technical retry or curator video review is required.",
            "confidence": "low",
        })
    return {
        "schema_version": "pr-image-role-leakage-v1", "case_id": packet["case_id"],
        "images": images, "source_path_recommendation": "no_candidate",
        "before_candidate_asset_ids": [], "curator_only_asset_ids": [],
        "crop_review_asset_ids": [], "retry_asset_ids": retry_ids,
        "video_review_asset_ids": video_ids,
        "problem_statement_action": "unavailable",
        "leakage_summary": "No image was exposed to a model or solver.",
        "limitations": ["No attached still image; this is a technical routing result, not a semantic rejection."],
    }


def _archive_paths(paths: list[Path], manifests: list[Path],
                   orchestrations: list[Path] = ()) -> list[Path]:
    resolved = [path.resolve(strict=True) for path in paths]
    expanded_manifests = list(manifests)
    for orchestration_path in orchestrations:
        orchestration_path = orchestration_path.resolve(strict=True)
        orchestration = json.loads(orchestration_path.read_text())
        if orchestration.get("schema_version") != "selected-candidate-stage11-waves-v1":
            raise ValueError("unsupported Stage-11 orchestration manifest")
        quality = orchestration.get("quality_audit") or {}
        quality_path = Path(quality.get("path", ""))
        if (not quality_path.is_file() or _sha(quality_path) != quality.get("sha256")):
            raise ValueError("Stage-11 quality audit binding changed")
        entries = [
            {"path": str(Path(item["path"]) / "11_manifest.json"),
             "sha256": item["manifest_sha256"]}
            for item in orchestration.get("previous_runs", [])
        ] + [
            {"path": str(Path(item["run"]) / "11_manifest.json"),
             "sha256": item["run_manifest_sha256"]}
            for item in orchestration.get("waves", [])
        ]
        for item in entries:
            manifest_path = Path(item["path"]).resolve(strict=True)
            if _sha(manifest_path) != item["sha256"]:
                raise ValueError("Stage-11 orchestration archive binding changed")
            expanded_manifests.append(manifest_path)
    for manifest_path in expanded_manifests:
        manifest_path = manifest_path.resolve(strict=True)
        manifest = json.loads(manifest_path.read_text())
        files = manifest.get("files")
        if not isinstance(files, dict):
            raise ValueError("source archive manifest lacks files")
        for name, expected in files.items():
            path = manifest_path.parent / name
            if _sha(path) != expected:
                raise ValueError("source archive manifest binding changed")
            resolved.append(path.resolve())
    if len(resolved) != len(set(resolved)):
        raise ValueError("duplicate source archive input")
    identities = [json.loads(path.read_text()).get("instance_id") for path in resolved]
    if any(not isinstance(identity, str) or not identity for identity in identities):
        raise ValueError("source archive identity is missing")
    if len(identities) != len(set(identities)):
        raise ValueError("duplicate PR across source archives")
    return resolved


def _render(output: Path, records: list[dict]) -> Path:
    cards = []
    for record in records:
        packet_path = record.get("packet")
        if not packet_path:
            cards.append(
                f'<section><h2>{html.escape(record["case_id"])} '
                f'<span>{html.escape(record["status"])}</span></h2>'
                '<p><b>技术路由：</b>模型输入准备失败；本题未调用 Verifier，'
                '也未被解释为语义不合格。</p>'
                f'<pre>{html.escape(str(record.get("reason", "")))}</pre></section>')
            continue
        packet = json.loads(Path(packet_path).read_text())
        annotation = record.get("annotation") or {}
        decisions = {item["asset_id"]: item for item in annotation.get("images", [])}
        images = []
        archive_root = Path(packet["source_archive"]).parent / "11_http_archive"
        archive = json.loads(Path(packet["source_archive"]).read_text())
        download_by_sha = {item.get("sha256"): item for item in archive["sections"]["assets"]["items"]
                           if item.get("status") == "complete"}
        for asset in packet["assets"]:
            decision = decisions.get(asset["asset_id"], {})
            downloaded = download_by_sha.get(asset.get("content_sha256"))
            preview = ""
            if downloaded:
                preview_path = (archive_root / downloaded["local_path"]).resolve()
                preview = f'<img loading="lazy" src="{html.escape(preview_path.as_uri())}">'
            images.append(
                '<div class="asset">' + preview
                + f'<div><code>{html.escape(asset["asset_id"][:12])}</code> · '
                + html.escape("/".join(asset.get("origin_kinds", []))) + '</div>'
                + f'<b>{html.escape(str(decision.get("role", record["status"])))}</b> · '
                + html.escape(str(decision.get("agent_visibility_recommendation", "")))
                + f'<p>{html.escape(str(decision.get("reason", record.get("reason", ""))))}</p></div>')
        pr_url = next((doc.get("url") for doc in packet["source_documents"]
                       if doc["source_id"] == "pr:body"), "")
        documents = "".join(
            f'<details><summary>{html.escape(doc["source_id"])}</summary><pre>{html.escape(doc["text"])}</pre></details>'
            for doc in packet["source_documents"])
        cards.append(
            f'<section><h2><a href="{html.escape(pr_url or "#")}">{html.escape(packet["case_id"])}</a> '
            f'<span>{html.escape(record["status"])}</span></h2>'
            f'<p><b>来源建议：</b>{html.escape(str(annotation.get("source_path_recommendation", "未判定")))} · '
            f'<b>题面动作：</b>{html.escape(str(annotation.get("problem_statement_action", "未判定")))}</p>'
            + '<div class="assets">' + "".join(images) + '</div>' + documents
            + f'<details><summary>完整 Verifier 输出</summary><pre>{html.escape(json.dumps(annotation, ensure_ascii=False, indent=2))}</pre></details></section>')
    page = """<!doctype html><meta charset=utf-8><title>08_04 PR image role audit</title>
<style>body{font:13px system-ui;margin:18px;background:#f5f6f8;color:#202124}section{background:white;border:1px solid #ddd;border-radius:8px;padding:12px;margin:10px 0}h2{font-size:16px;margin:0 0 8px}h2 span{font-size:11px;background:#eee;padding:3px 6px;border-radius:9px}.assets{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:8px}.asset{border:1px solid #ddd;border-radius:6px;padding:7px}.asset img{width:100%;height:145px;object-fit:contain;background:#fafafa}.asset p{margin:4px 0}pre{white-space:pre-wrap;max-height:360px;overflow:auto;background:#f7f7f7;padding:8px}</style>""" + "".join(cards)
    path = output / "08_04_04_audit.html"
    path.write_text(page)
    return path


def _attempt_binding(work: Path, semantic_attempt: int) -> dict:
    files = {}
    for name in ("10_api_invocation.json", "10_api_request.json",
                 "09_model_raw.json"):
        path = work / name
        if path.is_file():
            files[name] = {"path": str(path.resolve()), "sha256": _sha(path)}
    provider = sorted(work.glob("10_provider_response_*.json"))
    receipts = sorted(work.glob("10_attempt_*.json"))
    for path in (*provider, *receipts):
        files[path.name] = {"path": str(path.resolve()), "sha256": _sha(path)}
    return {"semantic_attempt": semantic_attempt, "workdir": str(work.resolve()),
            "files": files}


def validate_run(run_directory: Path) -> dict:
    run_directory = run_directory.resolve(strict=True)
    result_path = run_directory / "08_04_03_results.json"
    value = json.loads(result_path.read_text())
    if value.get("schema_version") != RUNNER_VERSION:
        raise ValueError("unsupported PR image-role run")
    runner = run_directory / "08_04_00_runner.py"
    prompt = run_directory / PROMPT.name
    schema = run_directory / SCHEMA.name
    if (not runner.is_file() or value.get("runner_sha256") != _sha(runner)
            or value.get("prompt_sha256") != _sha(prompt)
            or value.get("schema_sha256") != _sha(schema)):
        raise ValueError("PR image-role code/prompt/schema binding changed")
    records = value.get("records")
    if not isinstance(records, list):
        raise ValueError("PR image-role records are missing")
    if len({item.get("case_id") for item in records}) != len(records):
        raise ValueError("duplicate PR image-role case")
    contract = value.get("evaluator_contract")
    if value.get("model_invoked"):
        _validate_evaluator_contract(contract)
    elif contract is not None:
        raise ValueError("prepare-only image-role run contains an evaluator contract")
    for record in records:
        archive = Path(record.get("source_archive", ""))
        if not archive.is_file() or _sha(archive) != record.get("source_archive_sha256"):
            raise ValueError("PR image-role source binding changed")
        if json.loads(archive.read_text()).get("instance_id") != record.get("case_id"):
            raise ValueError("PR image-role source identity changed")
        if record.get("failure_class") == "input_preparation":
            if (record.get("status") != "failed" or record.get("packet") is not None
                    or record.get("packet_sha256") is not None
                    or record.get("annotation") is not None
                    or record.get("invocation") is not None
                    or record.get("decision_method") != "technical_failure"
                    or not record.get("reason")):
                raise ValueError("invalid PR image-role input-preparation failure")
            continue
        packet_path = Path(record.get("packet", ""))
        if (not packet_path.is_file() or not packet_path.resolve().is_relative_to(run_directory)
                or _sha(packet_path) != record.get("packet_sha256")):
            raise ValueError("PR image-role packet binding changed")
        packet = json.loads(packet_path.read_text())
        if (packet.get("case_id") != record.get("case_id")
                or packet.get("source_archive") != str(archive.resolve())
                or packet.get("source_archive_sha256") != _sha(archive)):
            raise ValueError("PR image-role packet identity changed")
        if record.get("status") == "complete":
            validate(record.get("annotation"), packet, schema)
            if record.get("decision_method") == "deterministic_unobservable":
                if record.get("invocation") is not None or any(
                        asset.get("attachment_index") is not None for asset in packet["assets"]):
                    raise ValueError("invalid deterministic unobservable record")
                continue
            invocation = record.get("invocation") or {}
            if invocation.get("evaluator_contract") != contract:
                raise ValueError("PR image-role evaluator contract changed")
            attempt_records = invocation.get("semantic_attempt_records")
            if (not isinstance(attempt_records, list) or not attempt_records
                    or invocation.get("semantic_validation_attempts") != len(attempt_records)):
                raise ValueError("PR image-role semantic attempt ledger changed")
            for attempt in attempt_records:
                if attempt.get("semantic_attempt") < 1:
                    raise ValueError("invalid semantic attempt identity")
                work = Path(attempt.get("workdir", ""))
                if not work.resolve().is_relative_to(run_directory):
                    raise ValueError("semantic attempt escapes run directory")
                for binding in attempt.get("files", {}).values():
                    path = Path(binding.get("path", ""))
                    if (not path.is_file() or not path.resolve().is_relative_to(work.resolve())
                            or _sha(path) != binding.get("sha256")):
                        raise ValueError("semantic attempt file binding changed")
            raw_binding = attempt_records[-1]["files"].get("09_model_raw.json")
            if not raw_binding or json.loads(Path(raw_binding["path"]).read_text()) != record["annotation"]:
                raise ValueError("successful raw annotation changed")
        elif record.get("status") not in {"prepared", "failed"}:
            raise ValueError("unsupported PR image-role record status")
        elif record.get("annotation") is not None:
            raise ValueError("non-complete PR image-role record contains an accepted annotation")
        elif record.get("status") == "prepared" and record.get("invocation") is not None:
            raise ValueError("prepare-only PR image-role record contains model invocation")
        elif record.get("status") == "failed" and record.get("invocation") is not None:
            invocation = record["invocation"]
            if (invocation.get("evaluator_contract") != contract
                    or record.get("failure_class") not in {
                        "semantic_validation", "provider_or_infrastructure"}):
                raise ValueError("failed PR image-role invocation classification changed")
            attempt_records = invocation.get("semantic_attempt_records")
            if (not isinstance(attempt_records, list) or not attempt_records
                    or invocation.get("semantic_validation_attempts") != len(attempt_records)):
                raise ValueError("failed PR image-role attempt ledger changed")
            for attempt in attempt_records:
                work = Path(attempt.get("workdir", ""))
                if not work.resolve().is_relative_to(run_directory):
                    raise ValueError("failed semantic attempt escapes run directory")
                for binding in attempt.get("files", {}).values():
                    path = Path(binding.get("path", ""))
                    if (not path.is_file() or not path.resolve().is_relative_to(work.resolve())
                            or _sha(path) != binding.get("sha256")):
                        raise ValueError("failed semantic attempt file binding changed")
    expected_counts = {status: sum(record["status"] == status for record in records)
                       for status in ("complete", "prepared", "failed")}
    if value.get("counts") != expected_counts or value.get("source_archive_count") != len(records):
        raise ValueError("PR image-role counts changed")
    checkpoints = value.get("checkpoints")
    if checkpoints is not None:
        if set(checkpoints) != {record["case_id"] for record in records}:
            raise ValueError("PR image-role checkpoint inventory changed")
        by_case = {record["case_id"]: record for record in records}
        for case_id, binding in checkpoints.items():
            path = Path(binding.get("path", ""))
            if (not path.is_file() or not path.resolve().is_relative_to(run_directory)
                    or _sha(path) != binding.get("sha256")
                    or json.loads(path.read_text()) != by_case[case_id]):
                raise ValueError("PR image-role checkpoint binding changed")
    audit = run_directory / "08_04_04_audit.html"
    if (not audit.is_file() or value.get("audit_html") != str(audit)
            or value.get("audit_html_sha256") != _sha(audit)):
        raise ValueError("PR image-role HTML binding changed")
    return value


def run(*, archives: list[Path], archive_manifests: list[Path], output: Path,
        archive_orchestrations: list[Path] | None = None,
        evaluator=None, timeout: int = 480) -> dict:
    output = output.resolve()
    if output.exists():
        raise ValueError(f"output already exists: {output}")
    archive_paths = _archive_paths(archives, archive_manifests,
                                   archive_orchestrations or [])
    output.mkdir(parents=True)
    try:
        shutil.copyfile(PROMPT, output / PROMPT.name)
        shutil.copyfile(SCHEMA, output / SCHEMA.name)
        shutil.copyfile(Path(__file__), output / "08_04_00_runner.py")
        records = []
        checkpoints = {}
        evaluator_contract = _evaluator_contract(evaluator) if evaluator is not None else None
        for index, archive_path in enumerate(archive_paths, 1):
            packet_dir = output / "08_04_01_packets" / f"case_{index:04d}"
            packet_dir.mkdir(parents=True)
            archive_identity = json.loads(archive_path.read_text()).get("instance_id")
            record = {
                "case_id": archive_identity, "status": "prepared",
                "source_archive": str(archive_path), "source_archive_sha256": _sha(archive_path),
                "packet": None, "packet_sha256": None,
                "annotation": None, "invocation": None, "decision_method": None,
            }
            try:
                packet, image_paths = build_packet(
                    archive_path, output / "08_04_02_model_inputs" / f"case_{index:04d}")
            except Exception as exc:
                record.update(
                    status="failed", failure_class="input_preparation",
                    decision_method="technical_failure",
                    reason=f"{type(exc).__name__}: {str(exc)[:1200]}")
                records.append(record)
                checkpoint = output / "08_04_03_checkpoints" / f"case_{index:04d}.json"
                checkpoint.parent.mkdir(parents=True, exist_ok=True)
                write_json(checkpoint, record)
                checkpoints[record["case_id"]] = {
                    "path": str(checkpoint), "sha256": _sha(checkpoint)}
                continue
            packet_path = packet_dir / "08_04_01_packet.json"
            write_json(packet_path, packet)
            record.update(case_id=packet["case_id"], packet=str(packet_path),
                          packet_sha256=_sha(packet_path))
            if evaluator is not None:
                if not image_paths:
                    annotation = _unobservable_annotation(packet)
                    validate(annotation, packet, output / SCHEMA.name)
                    record.update(status="complete", annotation=annotation,
                                  decision_method="deterministic_unobservable")
                    records.append(record)
                    checkpoint = (output / "08_04_03_checkpoints"
                                  / f"case_{index:04d}.json")
                    checkpoint.parent.mkdir(parents=True, exist_ok=True)
                    write_json(checkpoint, record)
                    checkpoints[record["case_id"]] = {
                        "path": str(checkpoint), "sha256": _sha(checkpoint)}
                    continue
                validation_failures = []
                provider_failures = []
                semantic_attempt_records = []
                failure_class = "provider_or_infrastructure"
                try:
                    for semantic_attempt in (1, 2, 3):
                        work = (output / "08_04_03_model_runs" / f"case_{index:04d}"
                                / f"semantic_{semantic_attempt:02d}")
                        work.mkdir(parents=True)
                        attempt_packet = json.loads(json.dumps(packet))
                        if validation_failures:
                            attempt_packet["previous_output_validation_error"] = validation_failures[-1]
                        failure_class = "provider_or_infrastructure"
                        try:
                            annotation, invocation = evaluator(
                                packet=attempt_packet, image_paths=image_paths,
                                system_prompt=output / PROMPT.name, schema=output / SCHEMA.name,
                                workdir=work, timeout=timeout)
                        except Exception as exc:
                            semantic_attempt_records.append(
                                _attempt_binding(work, semantic_attempt))
                            provider_failures.append({
                                "attempt": semantic_attempt,
                                "error_type": type(exc).__name__,
                                "status_code": getattr(exc, "status_code", None),
                            })
                            if semantic_attempt == 3:
                                raise
                            continue
                        semantic_attempt_records.append(
                            _attempt_binding(work, semantic_attempt))
                        try:
                            annotation = _canonicalize_routing_lists(annotation, packet)
                            validate(annotation, packet, output / SCHEMA.name)
                            break
                        except Exception as exc:
                            failure_class = "semantic_validation"
                            validation_failures.append(
                                f"{type(exc).__name__}: {str(exc)[:1200]}")
                            if semantic_attempt == 3:
                                raise
                    invocation["semantic_validation_attempts"] = semantic_attempt
                    invocation["prior_validation_failures"] = validation_failures
                    invocation["prior_provider_failures"] = provider_failures
                    invocation["semantic_attempt_records"] = semantic_attempt_records
                    record.update(status="complete", annotation=annotation,
                                  invocation={**invocation, "evaluator_contract": evaluator_contract},
                                  decision_method="vlm")
                except Exception as exc:
                    record.update(status="failed",
                                  reason=f"{type(exc).__name__}: {str(exc)[:1200]}",
                                  failure_class=failure_class,
                                  invocation={
                                      "evaluator_contract": evaluator_contract,
                                      "semantic_validation_attempts": len(semantic_attempt_records),
                                      "prior_validation_failures": validation_failures,
                                      "prior_provider_failures": provider_failures,
                                      "semantic_attempt_records": semantic_attempt_records,
                                  })
            records.append(record)
            checkpoint = output / "08_04_03_checkpoints" / f"case_{index:04d}.json"
            checkpoint.parent.mkdir(parents=True, exist_ok=True)
            write_json(checkpoint, record)
            checkpoints[record["case_id"]] = {
                "path": str(checkpoint), "sha256": _sha(checkpoint)}
        result_path = output / "08_04_03_results.json"
        result = {
            "schema_version": RUNNER_VERSION, "status": "complete",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "runner_sha256": _sha(output / "08_04_00_runner.py"),
            "prompt_sha256": _sha(output / PROMPT.name),
            "schema_sha256": _sha(output / SCHEMA.name),
            "model_invoked": evaluator is not None,
            "evaluator_contract": evaluator_contract,
            "source_archive_count": len(archive_paths),
            "records": records,
            "checkpoints": checkpoints,
            "counts": {status: sum(record["status"] == status for record in records)
                       for status in ("complete", "prepared", "failed")},
            "boundary": {
                "verifier_is_curator_side_only": True,
                "verifier_cannot_approve_formal_task": True,
                "pr_derived_requires_human_problem_statement_and_leakage_review": True,
                "all_solver_visible_assets_require_existing_human_visual_gate": True,
                "api_or_decode_failure_is_not_semantic_rejection": True,
            },
        }
        audit = _render(output, records)
        result["audit_html"] = str(audit)
        result["audit_html_sha256"] = _sha(audit)
        write_json(result_path, result)
        result["result"] = str(result_path)
        return result
    except BaseException as exc:
        # Preserve completed per-case checkpoints. An interrupted batch can be
        # diagnosed or resumed without silently discarding already-paid calls.
        if output.exists():
            write_json(output / "08_04_99_interrupted.json", {
                "schema_version": "pr-image-role-interruption-v1",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "error_type": type(exc).__name__, "reason": str(exc)[:1200],
                "completed_checkpoint_count": len(locals().get("checkpoints", {})),
            })
        raise
