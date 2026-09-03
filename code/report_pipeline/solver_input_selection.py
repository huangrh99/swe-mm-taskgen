"""Build the audited Stage-08.05 solver-input proposal and human queues."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from report_pipeline.pr_image_roles import validate_run


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))


def _partial_archive_is_safe(record: dict, archive: dict) -> tuple[bool, list[str]]:
    """Allow only irrelevant asset gaps after all semantic source sections passed."""
    sections = archive.get("sections")
    if not isinstance(sections, dict):
        return False, []
    incomplete = sorted(
        name for name, value in sections.items()
        if not isinstance(value, dict) or value.get("status") != "complete"
    )
    if incomplete != ["assets"]:
        return False, incomplete

    packet_path = Path(record.get("packet", "")).resolve()
    if not packet_path.is_file() or _sha(packet_path) != record.get("packet_sha256"):
        return False, incomplete
    packet = json.loads(packet_path.read_text())
    assets = {asset.get("asset_id"): asset for asset in packet.get("assets", [])}
    selected = record["annotation"]["before_candidate_asset_ids"]
    for asset_id in selected:
        asset = assets.get(asset_id)
        if not asset:
            return False, incomplete
        if not set(asset.get("origin_kinds", [])) & {"issue", "pr"}:
            return False, incomplete
        if "complete" not in asset.get("download_statuses", []):
            return False, incomplete
        if not isinstance(asset.get("attachment_index"), int):
            return False, incomplete
    return True, incomplete


def _issue_only_asset_ids(record: dict, candidate_ids: list[str]) -> list[str]:
    """Return candidate assets that are bound only to archived Issue sources."""
    packet_path = Path(record.get("packet", "")).resolve()
    if not packet_path.is_file() or _sha(packet_path) != record.get("packet_sha256"):
        return []
    packet = json.loads(packet_path.read_text())
    assets = {asset.get("asset_id"): asset for asset in packet.get("assets", [])}
    issue_ids = []
    for asset_id in candidate_ids:
        asset = assets.get(asset_id)
        if not asset:
            continue
        occurrences = asset.get("occurrences", [])
        source_ids = [item.get("source_id", "") for item in occurrences]
        if ("issue" in asset.get("origin_kinds", [])
                and source_ids
                and all("#" in source_id and not source_id.startswith("pr:")
                        for source_id in source_ids)):
            issue_ids.append(asset_id)
    return issue_ids


def _issue_expected_design_asset_ids(record: dict) -> list[str]:
    annotation = record["annotation"]
    expected_ids = [
        image["asset_id"] for image in annotation.get("images", [])
        if image.get("observed") is True
        and image.get("role") == "expected_design"
        and image.get("contains_fixed_after") == "no"
        and image.get("contains_solution_evidence") == "no"
        and image.get("task_relationship") == "explicit"
        and image.get("confidence") == "high"
        and image.get("agent_visibility_recommendation") == "human_review"
    ]
    return _issue_only_asset_ids(record, expected_ids)


def _classify(record: dict) -> tuple[str, dict]:
    base = {
        "case_id": record["case_id"],
        "source_archive": record["source_archive"],
        "source_archive_sha256": record["source_archive_sha256"],
    }
    if record["status"] != "complete":
        return "followup", base | {"reason": "image_role_semantic_validation_failed"}
    archive_path = Path(record["source_archive"]).resolve()
    archive = json.loads(archive_path.read_text())
    if _sha(archive_path) != record["source_archive_sha256"]:
        raise ValueError(f"source archive changed: {record['case_id']}")
    annotation = record["annotation"]
    route = annotation["source_path_recommendation"]
    selected_asset_ids = list(annotation["before_candidate_asset_ids"])
    original_route = route
    issue_first_reduction = False
    expected_design_recall = False
    if route == "both":
        issue_ids = _issue_only_asset_ids(record, selected_asset_ids)
        ambiguous = set(annotation["crop_review_asset_ids"]) | set(
            annotation["video_review_asset_ids"])
        if issue_ids and not ambiguous.intersection(issue_ids):
            route = "issue_derived"
            selected_asset_ids = issue_ids
            issue_first_reduction = True
    if route != "issue_derived" or not selected_asset_ids:
        expected_design_ids = _issue_expected_design_asset_ids(record)
        if expected_design_ids:
            route = "issue_derived"
            selected_asset_ids = expected_design_ids
            expected_design_recall = True
    row = base | {
        "repo": archive["repo"],
        "number": archive["number"],
        "pr_id": f"{archive['repo']}#{archive['number']}",
        "source_route": route,
        "before_candidate_asset_ids": selected_asset_ids,
        "crop_review_asset_ids": annotation["crop_review_asset_ids"],
        "video_review_asset_ids": annotation["video_review_asset_ids"],
        "retry_asset_ids": annotation["retry_asset_ids"],
        "problem_statement_action": annotation["problem_statement_action"],
        "human_visual_gate": "pending",
        "formal_admission": False,
    }
    if issue_first_reduction:
        row.update({
            "original_source_route": original_route,
            "issue_first_reduction": True,
            "excluded_pr_candidate_asset_ids": sorted(
                set(annotation["before_candidate_asset_ids"]) - set(selected_asset_ids)),
            "problem_statement_action": "use_issue_text",
        })
    if expected_design_recall:
        row.update({
            "original_source_route": original_route,
            "candidate_basis": "historical_issue_expected_design",
            "expected_design_human_confirmation": "pending",
            "problem_statement_action": "use_issue_text",
        })
    if archive.get("status") != "complete":
        safe_partial, incomplete = _partial_archive_is_safe(record, archive)
        if not safe_partial:
            return "followup", row | {
                "reason": "source_archive_not_complete",
                "incomplete_archive_sections": incomplete,
            }
        row["archive_warnings"] = ["unselected_asset_download_gaps_ignored"]
        row["incomplete_archive_sections"] = incomplete
    if route == "issue_derived" and selected_asset_ids:
        return "selected", row
    reason = {
        "pr_derived": "human_authored_pr_derived_statement_required",
        "both": "human_route_and_asset_allowlist_required",
        "no_candidate": "no_solver_visible_image_candidate",
    }[route]
    return "followup", row | {"reason": reason}


def run(image_role_runs: list[Path], output: Path,
        retry_runs: list[Path] | None = None,
        case_ids: list[str] | None = None) -> dict:
    if not image_role_runs:
        raise ValueError("at least one image-role run is required")
    output = output.resolve()
    if output.exists():
        raise ValueError(f"output already exists: {output}")
    records, sources = [], []
    for run_path in map(Path.resolve, image_role_runs):
        value = validate_run(run_path)
        sources.append({
            "path": str(run_path / "08_04_03_results.json"),
            "sha256": _sha(run_path / "08_04_03_results.json"),
            "status": value["status"],
        })
        records.extend(value["records"])
    identities = [record["case_id"] for record in records]
    if len(identities) != len(set(identities)):
        raise ValueError("duplicate PR across image-role runs")
    order = list(identities)
    active = {record["case_id"]: record for record in records}
    for run_path in map(Path.resolve, retry_runs or []):
        value = validate_run(run_path)
        sources.append({
            "path": str(run_path / "08_04_03_results.json"),
            "sha256": _sha(run_path / "08_04_03_results.json"),
            "status": value["status"], "role": "retry_override",
        })
        for record in value["records"]:
            previous = active.get(record["case_id"])
            if previous is None:
                raise ValueError("retry record is not present in the base image-role runs")
            if previous["status"] != "failed":
                raise ValueError("retry can only replace a failed image-role record")
            if (previous["source_archive"] != record["source_archive"]
                    or previous["source_archive_sha256"] != record["source_archive_sha256"]):
                raise ValueError("retry record source archive binding changed")
            active[record["case_id"]] = record
    records = [active[identity] for identity in order]
    if case_ids:
        requested = set(case_ids)
        if len(requested) != len(case_ids):
            raise ValueError("duplicate requested case ID")
        missing = sorted(requested - set(active))
        if missing:
            raise ValueError("requested case ID not present: " + ", ".join(missing))
        records = [record for record in records if record["case_id"] in requested]

    selected, followup = [], []
    for record in records:
        destination, row = _classify(record)
        (selected if destination == "selected" else followup).append(row)
    selected.sort(key=lambda row: (row["repo"], row["number"]))
    followup.sort(key=lambda row: row["case_id"])

    output.mkdir(parents=True)
    selected_path = output / "08_05_01_issue_derived_selected.jsonl"
    followup_path = output / "08_05_02_human_followup.jsonl"
    _write_jsonl(selected_path, selected)
    _write_jsonl(followup_path, followup)
    reasons: dict[str, int] = {}
    for row in followup:
        reasons[row["reason"]] = reasons.get(row["reason"], 0) + 1
    manifest = {
        "schema_version": "solver-input-selection-v3",
        "status": "complete",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_image_role_runs": sources,
        "source_record_count": len(records),
        "requested_case_ids": sorted(case_ids or []),
        "retry_run_count": len(retry_runs or []),
        "selected_count": len(selected),
        "human_followup_count": len(followup),
        "followup_reason_counts": dict(sorted(reasons.items())),
        "selected_file": selected_path.name,
        "selected_sha256": _sha(selected_path),
        "human_followup_file": followup_path.name,
        "human_followup_sha256": _sha(followup_path),
        "policy": {
            "automatic_proposal_route": (
                "issue_derived with before candidates; archive must be complete, or only the "
                "assets section may be partial while every selected Issue/PR asset is complete"
            ),
            "partial_archive_boundary": (
                "linked issues, closing issues, consistency, and every non-asset section must "
                "be complete; only unselected asset download gaps may be ignored"
            ),
            "pr_derived_and_both": "human followup required",
            "historical_issue_expected_design": (
                "high-confidence, explicit, non-after and non-solution Issue assets may enter "
                "the review candidate pool with expected-design confirmation still pending"
            ),
            "human_visual_gate": "pending for every selected row",
            "formal_admission": False,
        },
    }
    (output / "08_05_03_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest
