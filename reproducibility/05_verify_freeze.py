#!/usr/bin/env python3
"""Validate freeze structure, evidence hashes, and secret/path boundaries."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

import jsonschema

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "02_freeze_manifest.json"
SCHEMA = HERE / "01_freeze_manifest.schema.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fail(message: str) -> None:
    raise ValueError(message)


FORBIDDEN = [
    r"(?i)(api[_-]?key|access[_-]?token|secret)[\"']?\s*[:=]\s*[\"'][^\"']+",
    r"sk-[A-Za-z0-9_-]{16,}",
    r"AIza[0-9A-Za-z_-]{20,}",
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
]


def validate_authorization_values(record: dict, task: dict, agent: dict) -> None:
    expected = {
        "authorized": True,
        "authorization_id": agent["authorization_record"]["authorization_id"],
        "task_directory_checksum": task["task_directory_checksum"],
        "task_inventory_sha256": task["task_inventory"]["sha256"],
        "model_id": agent["model_id"], "agent": agent["agent"],
        "agent_version": agent["agent_version"],
        "instruction_sha256": agent["instruction_sha256"],
        "sampling": agent["sampling"], "budget": agent["budget"],
        "tool_policy": agent["tool_policy"], "timeout_sec": agent["timeout_sec"],
        "trial_count": agent["trial_count"], "expected_external_calls": agent["expected_external_calls"],
        "agent_command": task["agent_command"], "verifier_command": task["verifier_command"],
    }
    mismatched = [key for key, value in expected.items() if record.get(key) != value]
    if mismatched:
        fail(f"authorization record differs from frozen run: {mismatched}")


def _required_complete(data: dict, *, verify_entities: bool) -> None:
    hex40 = re.compile(r"^[0-9a-f]{40}$")
    hex64 = re.compile(r"^[0-9a-f]{64}$")
    digest = re.compile(r"^sha256:[0-9a-f]{64}$")
    def require_command(value, label):
        if not isinstance(value, list) or not value or not all(isinstance(x, str) and x.strip() for x in value):
            fail(f"complete freeze requires structured command list for {label}")
    def bound_file(record, label):
        if not isinstance(record, dict) or not hex64.fullmatch(record.get("sha256", "")):
            fail(f"complete freeze requires path/hash evidence for {label}")
        relative = record.get("path", "")
        if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
            fail(f"complete freeze has unsafe evidence path for {label}")
        path = ROOT / relative
        if not verify_entities:
            fail("complete freeze cannot be certified without entity verification")
        if not path.is_file() or sha256(path) != record["sha256"]:
            fail(f"complete freeze evidence missing or mismatched for {label}")
        return path
    if data.get("open_items"):
        fail("complete freeze cannot retain open_items")
    deps = data["dependencies"]["formal_pipeline"]
    if deps.get("state") != "realtime_verified" or not deps.get("hash_locked_resolution_present"):
        fail("complete freeze requires hash-locked formal dependencies")
    bound_file(deps.get("lock_evidence"), "formal dependency lock")
    installed = data["harbor"]["installed_package"]
    if not installed.get("source_commit_independently_bound"):
        fail("complete freeze requires installed Harbor source binding")
    if not hex40.fullmatch(data["harbor"]["source_revision"].get("commit", "")):
        fail("complete freeze requires a 40-hex Harbor source commit")
    task = data["harbor"]["selected_visual_task"]
    for key in ("task_directory_checksum", "agent_command", "verifier_command"):
        if not task.get(key):
            fail(f"complete freeze missing selected task {key}")
    if not hex64.fullmatch(task["task_directory_checksum"]):
        fail("complete freeze requires a SHA-256 task checksum")
    require_command(task["agent_command"], "agent_command")
    require_command(task["verifier_command"], "verifier_command")
    inventory_path = bound_file(task.get("task_inventory"), "selected task inventory")
    inventory = json.loads(inventory_path.read_text())
    task_root_value, files = inventory.get("task_root", ""), inventory.get("files")
    if (not task_root_value or Path(task_root_value).is_absolute() or ".." in Path(task_root_value).parts
            or not isinstance(files, list) or not files):
        fail("selected task inventory lacks safe task_root/material files")
    canonical = []
    for entry in files:
        relative, expected_sha = entry.get("path", ""), entry.get("sha256", "")
        if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts or not hex64.fullmatch(expected_sha):
            fail("selected task inventory has unsafe material entry")
        material = ROOT / task_root_value / relative
        if not material.is_file() or sha256(material) != expected_sha:
            fail(f"selected task material missing or mismatched: {relative}")
        canonical.append({"path": relative, "sha256": expected_sha})
    calculated = hashlib.sha256(json.dumps(sorted(canonical, key=lambda x: x["path"]), separators=(",", ":")).encode()).hexdigest()
    if inventory.get("task_directory_checksum") != calculated or calculated != task["task_directory_checksum"]:
        fail("selected task inventory checksum differs")
    if task.get("state") != "realtime_verified" or not task.get("task_schema_compatibility_verified"):
        fail("complete freeze requires verified selected Harbor task")
    if data["docker"]["daemon"].get("state") != "realtime_verified":
        fail("complete freeze requires realtime Docker daemon evidence")
    image = data["docker"]["selected_task_image"]
    for key in ("base_digest", "produced_image_id", "produced_repo_digest", "architecture", "build_command", "offline_archive_sha256"):
        if not image.get(key):
            fail(f"complete freeze missing selected image {key}")
    for key in ("base_digest", "produced_image_id", "produced_repo_digest"):
        if not digest.fullmatch(image[key]):
            fail(f"complete freeze requires sha256 digest for selected image {key}")
    if not hex64.fullmatch(image["offline_archive_sha256"]):
        fail("complete freeze requires SHA-256 offline image archive")
    require_command(image["build_command"], "build_command")
    archive_path = bound_file(image.get("offline_archive"), "offline image archive")
    if image["offline_archive"]["sha256"] != image["offline_archive_sha256"]:
        fail("offline image archive hash differs")
    inspection_path = bound_file(image.get("inspection_evidence"), "image inspection")
    inspection = json.loads(inspection_path.read_text())
    for key in ("produced_image_id", "produced_repo_digest", "architecture"):
        if inspection.get(key) != image[key]:
            fail(f"image inspection differs for {key}")
    if image.get("state") != "realtime_verified":
        fail("complete freeze requires realtime selected image evidence")
    agent = data["models"]["coding_agent"]
    for key in ("provider", "model_id", "agent", "agent_version", "instruction_path",
                "instruction_sha256", "sampling", "tool_policy", "budget", "timeout_sec",
                "expected_external_calls", "authorization_record"):
        if agent.get(key) in (None, "", {}, []):
            fail(f"complete freeze missing coding agent {key}")
    if not hex64.fullmatch(agent["instruction_sha256"]):
        fail("complete freeze requires coding-agent instruction SHA-256")
    bound_file({"path": agent["instruction_path"], "sha256": agent["instruction_sha256"]},
               "coding-agent instruction")
    if not isinstance(agent["sampling"], dict) or not isinstance(agent["budget"], dict):
        fail("complete freeze requires structured sampling and budget")
    if not isinstance(agent["timeout_sec"], int) or agent["timeout_sec"] <= 0:
        fail("complete freeze requires positive coding-agent timeout")
    if not isinstance(agent["expected_external_calls"], int) or agent["expected_external_calls"] < 5:
        fail("complete freeze requires at least five expected external calls")
    authorization = agent["authorization_record"]
    if not isinstance(authorization, dict) or not authorization.get("authorization_id"):
        fail("complete freeze requires hashed bound authorization record")
    authorization_path = bound_file(authorization, "external-run authorization")
    authorization_data = json.loads(authorization_path.read_text())
    validate_authorization_values(authorization_data, task, agent)
    if agent.get("state") != "realtime_verified" or agent.get("trial_count") != 5:
        fail("complete freeze requires realtime five-trial coding-agent binding")
    if data["runtime_policy"]["selected_task_limits"].get("state") != "realtime_verified":
        fail("complete freeze requires selected task limits")


def validate_data(data: dict, *, verify_files: bool = True) -> int:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    try:
        jsonschema.Draft202012Validator(schema).validate(data)
    except jsonschema.ValidationError as error:
        fail(f"schema violation: {error.message}")

    entries = list(data.get("schemas_and_prompts", []))
    entries.extend(data.get("dependencies", {}).get("files", []))
    entries.extend(data.get("harbor", {}).get("evidence_files", []))
    selected_task = data.get("harbor", {}).get("selected_visual_task", {})
    for key in ("task_inventory", "control_evidence", "negative_control_evidence",
                "git_history_evidence"):
        selected_evidence = selected_task.get(key)
        if isinstance(selected_evidence, dict) and selected_evidence.get("path"):
            entries.append(selected_evidence)
    model_evidence = data.get("models", {}).get("screening_verifier", {}).get("result_evidence")
    if model_evidence:
        entries.append(model_evidence)
    for image_key in ("functional_base_image", "selected_task_image"):
        image = data.get("docker", {}).get(image_key, {})
        for evidence_key in ("inspection_evidence", "offline_archive"):
            image_evidence = image.get(evidence_key)
            if image_evidence:
                entries.append(image_evidence)
    for entry in entries:
        relative = entry.get("path", "")
        if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
            fail(f"unsafe evidence path: {relative!r}")
        if verify_files:
            evidence = ROOT / relative
            if not evidence.is_file():
                fail(f"missing evidence file: {relative}")
            actual = sha256(evidence)
            if actual != entry.get("sha256"):
                fail(f"hash mismatch for {relative}: {actual}")

    negative_record = selected_task.get("negative_control_evidence")
    if verify_files and isinstance(negative_record, dict) and negative_record.get("path"):
        summary = json.loads((ROOT / negative_record["path"]).read_text())
        current_checksum = selected_task.get("task_directory_checksum")
        current_valid = summary.get("status") == "all_controls_passed"
        historical = summary.get("status") == "historical_task_checksum_rerun_required"
        if current_valid:
            if summary.get("task_directory_sha256") != current_checksum:
                fail("negative-control summary does not bind the selected task")
        elif historical:
            if (summary.get("current_task_directory_sha256") != current_checksum
                    or summary.get("current_task_validated") is not False
                    or negative_record.get("state") != "historical_checksum_rerun_required"):
                fail("historical negative-control summary does not bind the current pending task")
        else:
            fail("negative-control summary has an unsupported state")
        valid = summary.get("valid_run", {})
        invalid = summary.get("invalid_attempt", {})
        for label, record in (("valid negative-control run", valid),
                              ("invalid negative-control attempt", invalid)):
            relative = record.get("path", "")
            if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
                fail(f"unsafe evidence path for {label}: {relative!r}")
            raw_path = ROOT / relative
            if not raw_path.is_file() or sha256(raw_path) != record.get("sha256"):
                fail(f"missing or mismatched evidence for {label}")
        raw_valid = json.loads((ROOT / valid["path"]).read_text())
        controls = raw_valid.get("controls", {})
        expected_raw_checksum = (current_checksum if current_valid
                                 else summary.get("task_directory_sha256"))
        if (raw_valid.get("status") != "all_controls_passed"
                or raw_valid.get("canonical_task_material_sha256") != expected_raw_checksum
                or len(controls) != valid.get("control_count")
                or sum(item.get("control_passed") is True for item in controls.values()) != valid.get("passed_count")
                or not all(item.get("control_passed") is True for item in controls.values())):
            fail("raw negative-control run does not prove the compact summary")
        raw_invalid = json.loads((ROOT / invalid["path"]).read_text())
        if (invalid.get("classification") != "infrastructure_invalid"
                or any(item.get("verifier_reached") for item in raw_invalid.get("controls", {}).values())):
            fail("invalid negative-control attempt is not purely infrastructure-invalid")

    serialized = json.dumps(data, ensure_ascii=False)
    for pattern in FORBIDDEN:
        if re.search(pattern, serialized):
            fail(f"possible secret material matched pattern {pattern!r}")

    if verify_files:
        scan_paths = [path for path in (ROOT / "report").rglob("*") if path.is_file()
                      and path.name not in {"05_verify_freeze.py", "test_verify_freeze.py"}]
        scan_paths.extend(ROOT / entry["path"] for entry in entries)
        for path in scan_paths:
            if path.stat().st_size > 10 * 1024 * 1024:
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            is_test = "tests" in path.parts or path.name.startswith("test_")
            if is_test:
                for fixture in ("fixture-secret", "not-a-real-value", "sk-abcdefghijklmnop", "AIzaabcdefghijklmnopqrst"):
                    content = content.replace(fixture, "<allowed-test-fixture>")
            for pattern in (FORBIDDEN[1:] if is_test else FORBIDDEN):
                if re.search(pattern, content):
                    fail(f"possible secret material in {path.relative_to(ROOT)}")

    if data["freeze_status"] == "complete":
        _required_complete(data, verify_entities=verify_files)
    return len(entries)


def main() -> int:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    count = validate_data(data)
    print(f"freeze-ok status={data['freeze_status']} evidence_files={count}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"freeze-invalid: {error}", file=sys.stderr)
        raise SystemExit(1)
