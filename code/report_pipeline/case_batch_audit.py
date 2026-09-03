"""Validate and render the curator archive for a requested case batch."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path

from report_pipeline.atomic import write_bytes, write_json


SCHEMA_VERSION = "report-case-archive-v1"
SECTION_NAMES = (
    "visual_review", "source_archive", "test_construction", "measurements", "harbor"
)
STATUS_NAMES = (
    "source_archived", "test_construction", "base_gold_measurement",
    "harbor_empty", "harbor_oracle",
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact(case_root: Path, value: dict, task_root: Path | None = None) -> tuple[dict, list[str]]:
    errors: list[str] = []
    storage = value.get("storage")
    if storage not in {"copied", "hardlinked", "generated", "external_bound"}:
        return value, ["invalid_storage"]
    raw_path = value.get("source_path") if storage == "external_bound" else value.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        return value, ["missing_artifact_path"]
    path = Path(raw_path)
    if storage != "external_bound":
        if path.is_absolute() or ".." in path.parts:
            return value, ["unsafe_case_relative_path"]
        if raw_path.startswith("@task/") and task_root is not None:
            path = task_root / raw_path.removeprefix("@task/")
        else:
            path = case_root / path
    path = path.resolve()
    if not path.is_file():
        return value, ["artifact_missing"]
    actual_size = path.stat().st_size
    actual_sha = _sha(path)
    if value.get("size_bytes") != actual_size:
        errors.append("size_mismatch")
    if value.get("sha256") != actual_sha:
        errors.append("sha256_mismatch")
    checked = dict(value)
    checked["resolved_path"] = str(path)
    checked["valid"] = not errors
    return checked, errors


def audit_case(case_root: Path) -> dict:
    case_root = case_root.resolve()
    metadata_root = case_root / "meta"
    if not (metadata_root / "00_case_manifest.json").is_file():
        metadata_root = case_root  # read-only compatibility for pre-migration archives
    manifest_path = metadata_root / "00_case_manifest.json"
    errors: list[str] = []
    if not manifest_path.is_file():
        return {"case_id": case_root.name, "valid": False,
                "errors": ["manifest_missing"], "sections": {}, "pipeline_status": {}}
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"case_id": case_root.name, "valid": False,
                "errors": ["manifest_invalid_json"], "sections": {}, "pipeline_status": {}}
    if manifest.get("schema_version") != SCHEMA_VERSION:
        errors.append("schema_version_mismatch")
    if manifest.get("case_id") != case_root.name:
        errors.append("case_id_directory_mismatch")
    if manifest.get("state") not in {"candidate", "provisional", "frozen"}:
        errors.append("invalid_state")
    sections: dict[str, list[dict]] = {}
    raw_sections = manifest.get("sections")
    if not isinstance(raw_sections, dict):
        raw_sections = {}
        errors.append("sections_missing")
    for section in SECTION_NAMES:
        items = raw_sections.get(section, [])
        if not isinstance(items, list):
            errors.append(f"{section}:not_a_list")
            items = []
        checked_items = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                errors.append(f"{section}:{index}:not_an_object")
                continue
            checked, artifact_errors = _artifact(metadata_root, item, case_root)
            checked_items.append(checked)
            errors.extend(f"{section}:{index}:{code}" for code in artifact_errors)
        sections[section] = checked_items
    verifier_runs = []
    for item in sections.get("test_construction", []):
        if not str(item.get("path", "")).endswith("20_11_06_result.json"):
            continue
        path = Path(str(item.get("resolved_path", "")))
        if not item.get("valid") or not path.is_file():
            continue
        try:
            value = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            errors.append("verifier_result_invalid_json")
            continue
        annotation = value.get("annotation") or {}
        verifier_runs.append({
            "status": value.get("status"),
            "verdict": annotation.get("status"),
            "bundle_count": len(annotation.get("test_bundles") or []),
            "human_review_required": annotation.get("human_review_required"),
            "path": str(path),
        })
    pipeline = manifest.get("pipeline_status")
    if not isinstance(pipeline, dict):
        pipeline = {}
        errors.append("pipeline_status_missing")
    for name in STATUS_NAMES:
        if pipeline.get(name) not in {
            "complete", "prepared", "blocked", "not_started", "not_applicable"
        }:
            errors.append(f"pipeline_status:{name}:invalid")
    return {
        "case_id": manifest.get("case_id", case_root.name),
        "repository": manifest.get("repository"),
        "pr_number": manifest.get("pr_number"),
        "state": manifest.get("state"),
        "valid": not errors,
        "errors": errors,
        "sections": sections,
        "pipeline_status": pipeline,
        "blockers": manifest.get("blockers", []),
        "verifier_runs": verifier_runs,
        "manifest": str(manifest_path),
        "manifest_sha256": _sha(manifest_path),
    }


def _render(audit: dict) -> str:
    columns = STATUS_NAMES
    rows = []
    for case in audit["cases"]:
        status = case["pipeline_status"]
        cells = "".join(
            f'<td><span class="status {html.escape(str(status.get(name, "missing")))}">'
            f'{html.escape(str(status.get(name, "missing")))}</span></td>'
            for name in columns
        )
        blockers = "<br>".join(
            html.escape(f'{item.get("stage", "?")}: {item.get("code", "?")} — {item.get("detail", "")}')
            for item in case.get("blockers", []) if isinstance(item, dict)
        ) or "—"
        errors = "<br>".join(map(html.escape, case["errors"])) or "—"
        manifest_link = Path(case["manifest"]).as_uri() if case.get("manifest") else "#"
        verifier = case.get("verifier_runs", [])
        if verifier:
            latest = verifier[-1]
            verifier_text = (f'{latest.get("status")} / {latest.get("verdict")} · '
                             f'{latest.get("bundle_count")} bundle')
        else:
            verifier_text = "not_run"
        rows.append(
            f'<tr><td><a href="{manifest_link}">{html.escape(case["case_id"])}</a></td>'
            f'<td>{"valid" if case["valid"] else "invalid"}</td>'
            f'<td>{html.escape(verifier_text)}</td>{cells}'
            f'<td>{blockers}</td><td>{errors}</td></tr>'
        )
    headers = "".join(f"<th>{html.escape(name)}</th>" for name in columns)
    return f"""<!doctype html><meta charset="utf-8"><title>Case batch audit</title>
<style>
body{{font:13px system-ui;margin:20px;color:#172033}} h1{{margin:0 0 6px}} p{{color:#526070}}
table{{border-collapse:collapse;width:100%}} th,td{{border:1px solid #dbe1ea;padding:7px;vertical-align:top}}
th{{background:#f4f6f9;position:sticky;top:0}} .status{{padding:2px 6px;border-radius:10px;background:#eef2f7}}
.complete{{background:#dff6e8;color:#176436}} .blocked{{background:#ffe7df;color:#913c20}}
.prepared{{background:#fff2c7;color:#795c00}} a{{color:#2457c5}}
</style><h1>七题归档、测试与 Harbor 审计</h1>
<p>生成时间：{html.escape(audit["generated_at"])}。complete 只表示对应阶段有绑定证据；candidate/provisional 不等于正式 frozen。</p>
<table><thead><tr><th>题目</th><th>归档校验</th><th>Verifier / 新测试</th>{headers}<th>阻塞</th><th>完整性错误</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>"""


def run(cases_root: Path, expected_case_ids: list[str], output: Path) -> dict:
    cases_root = cases_root.resolve(strict=True)
    if len(expected_case_ids) != len(set(expected_case_ids)):
        raise ValueError("duplicate expected case id")
    cases = [audit_case(cases_root / case_id) for case_id in expected_case_ids]
    audit = {
        "schema_version": "report-case-batch-audit-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cases_root": str(cases_root),
        "expected_case_ids": expected_case_ids,
        "case_count": len(cases),
        "valid_archive_count": sum(case["valid"] for case in cases),
        "cases": cases,
    }
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "00_batch_audit.json", audit)
    write_bytes(output / "00_batch_audit.html", _render(audit).encode())
    return audit
