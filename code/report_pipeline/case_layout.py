"""Keep each named case self-contained while exposing a Harbor task projection."""

from __future__ import annotations

import json
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

from report_pipeline.atomic import write_json


CASE_ID = re.compile(r"^[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-[1-9][0-9]*$")
RUNTIME_ENTRIES = {"environment", "instruction.md", "solution", "task.toml", "tests"}
CURATOR_ENTRIES = {"meta", "outputs"}
ALLOWED_ENTRIES = RUNTIME_ENTRIES | CURATOR_ENTRIES
REQUIRED_FILES = {
    "environment/Dockerfile", "environment/base_image.json", "instruction.md",
    "solution/solve.sh", "solution/gold.patch", "task.toml", "tests/config.json",
    "tests/sweb_grade.py", "tests/test.patch", "tests/test.sh",
}


def status(case_root: Path) -> dict:
    observed = {item.name for item in case_root.iterdir()}
    unexpected = sorted(observed - ALLOWED_ENTRIES)
    missing = sorted(path for path in REQUIRED_FILES if not (case_root / path).is_file())
    return {
        "instance_id": case_root.name,
        "submit_ready_layout": not unexpected and not missing,
        "missing_required_files": missing,
        "unexpected_root_entries": unexpected,
        "evidence_contract": (
            "Curator metadata and runtime outputs remain in the case directory; "
            "Harbor runs use a projection containing only runtime entries."
        ),
    }


def _rewrite_case_config_paths(case_root: Path, meta: Path) -> list[str]:
    changed = []
    for path in meta.rglob("*.json"):
        try:
            value = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        rendered = json.dumps(value, ensure_ascii=False)
        old_task = f"report/cases/{case_root.name}/05_harbor/task"
        old_jobs = f"report/cases/{case_root.name}/05_harbor/jobs"
        if old_task not in rendered and old_jobs not in rendered:
            continue
        rendered = rendered.replace(old_task, f"report/cases/{case_root.name}")
        rendered = rendered.replace(old_jobs, f"report/cases/{case_root.name}/meta/05_harbor/jobs")
        path.write_text(json.dumps(json.loads(rendered), ensure_ascii=False, indent=2) + "\n")
        changed.append(path.relative_to(case_root).as_posix())
    return changed


def _rebind_manifest(case_root: Path, meta: Path) -> list[str]:
    """Bind the mutable archive index to relocated bytes without touching source files."""
    manifest_path = meta / "00_case_manifest.json"
    if not manifest_path.is_file():
        return []
    manifest = json.loads(manifest_path.read_text())
    changed = []
    sections = manifest.get("sections") or {}
    for items in sections.values():
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict) or item.get("storage") == "external_bound":
                continue
            relative = str(item.get("path", ""))
            if relative.startswith("05_harbor/task/"):
                relative = "@task/" + relative.removeprefix("05_harbor/task/")
                item["path"] = relative
                changed.append(relative)
            target = ((case_root / relative.removeprefix("@task/")) if relative.startswith("@task/")
              else (meta / relative))
            if target.is_file():
                item["size_bytes"] = target.stat().st_size
                item["sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
    harbor = sections.get("harbor")
    if isinstance(harbor, list):
        # The old export was replaced by the standardized runtime tree. Preserve the
        # old export evidence in layout_migration.json and bind the current bytes here.
        harbor[:] = [item for item in harbor
                     if not (isinstance(item, dict)
                             and str(item.get("path", "")).startswith("@task/"))]
        for target in sorted(case_root.rglob("*")):
            if not target.is_file():
                continue
            relative = target.relative_to(case_root).as_posix()
            harbor.append({
                "path": "@task/" + relative,
                "storage": "generated",
                "size_bytes": target.stat().st_size,
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            })
    manifest["layout"] = {
        "submission_root": ".",
        "metadata_root": str(meta),
        "harbor_projection_entries": sorted(RUNTIME_ENTRIES),
        "metadata_excluded_from_harbor_projection": True,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return changed


def migrate_case(case_root: Path, evidence_root: Path) -> dict:
    case_root = case_root.resolve()
    evidence_root = evidence_root.resolve()
    if not case_root.is_dir() or not CASE_ID.fullmatch(case_root.name):
        raise ValueError("invalid_case_root")
    # ``evidence_root`` is retained for CLI compatibility only.  The required
    # submission layout keeps meta/ and outputs/ beside the Harbor task files.
    meta = case_root / "meta"
    nested = case_root / "05_harbor" / "task"
    promoted = []
    if nested.is_dir():
        for source in sorted(nested.iterdir()):
            destination = case_root / source.name
            if destination.exists():
                raise FileExistsError(f"submission_entry_collision:{destination}")
            source.rename(destination)
            promoted.append(source.name)
        nested.rmdir()
    meta.mkdir(exist_ok=True)
    (case_root / "outputs").mkdir(exist_ok=True)
    moved = []
    for source in sorted(case_root.iterdir()):
        if source.name in ALLOWED_ENTRIES:
            continue
        destination = meta / source.name
        if destination.exists():
            raise FileExistsError(f"metadata_entry_collision:{destination}")
        source.rename(destination)
        moved.append(source.name)
    rewritten = _rewrite_case_config_paths(case_root, meta)
    rebound = _rebind_manifest(case_root, meta)
    result = {
        "schema_version": "case-layout-migration-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "case_root": str(case_root),
        "evidence_root": str(case_root),
        "promoted_runtime_entries": promoted,
        "moved_to_meta": moved,
        "rewritten_metadata_files": rewritten,
        "rebound_manifest_artifacts": rebound,
        **status(case_root),
    }
    write_json(meta / "layout_migration.json", result)
    return result


def migrate_all(cases_root: Path, report_meta_root: Path) -> dict:
    cases_root, report_meta_root = cases_root.resolve(), report_meta_root.resolve()
    report_meta_root.mkdir(parents=True, exist_ok=True)
    batch_root = report_meta_root / "case_batch_audits"
    batch_root.mkdir(exist_ok=True)
    moved_batch = []
    for source in sorted(cases_root.glob("_batch_audit*")):
        destination = batch_root / source.name
        if destination.exists():
            raise FileExistsError(f"batch_audit_collision:{destination}")
        source.rename(destination)
        moved_batch.append(source.name)
    cases = [migrate_case(path, report_meta_root / path.name)
             for path in sorted(cases_root.iterdir())
             if path.is_dir() and CASE_ID.fullmatch(path.name)]
    return {"schema_version": "case-layout-batch-v1", "cases": cases,
            "moved_batch_audits": moved_batch}
