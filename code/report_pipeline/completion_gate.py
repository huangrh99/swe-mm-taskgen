"""Machine-executable completion consistency gate for the full exam pipeline.

The gate fails closed on missing, stale, or internally inconsistent evidence.
It operates inside the honest-curator local-workspace trust boundary; it is not
a cryptographic CI or reviewer-identity attestation against a workspace owner
who can rewrite every local file.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

from report_pipeline.atomic import assert_no_symlink_chain, write_json
from report_pipeline.category_audit import (
    CATEGORIES, _load_exclusions, _qualification_from_source, summarize,
)
from report_pipeline.paths import REPORT_ROOT, WORKSPACE_ROOT
from report_pipeline.pre_review_classification import validate_classification_run
from report_pipeline.submission_contract import validate as validate_submission
from report_pipeline.workflow import (
    IMAGE_ID, _audit_pass5_summary, _bound_file, _json, _require_formal_freeze_ready,
    _require_formal_pass5_config,
    _task_inventory, _validate_frozen_task_tree, _validate_pipeline_freeze,
    _validate_formal_job_config,
    _replay_promotion_evidence, _validate_promotion_chain,
    _validate_promotion_commit, _validate_schema,
)


EVIDENCE_ROOT = REPORT_ROOT / "evidence"
EXPECTED_REVIEW_FOCI = {"correctness", "design", "security"}
EXPECTED_TEST_COMMAND = [
    "PYTHONDONTWRITEBYTECODE=1", "PYTHONPATH=code",
    ".runtime/venv/bin/python", "test.py",
    "--evidence",
]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _error(errors: list[dict], code: str, **details: object) -> None:
    errors.append({"code": code, **details})


def _formal_json(binding: object, label: str, errors: list[dict],
                 required_root: Path = EVIDENCE_ROOT) -> tuple[dict | None, Path | None]:
    try:
        path = _bound_file(binding if isinstance(binding, dict) else {}, label)
        if not path.is_relative_to(required_root.resolve()):
            raise ValueError(f"{label}_outside_formal_evidence")
        return _json(path), path
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        _error(errors, "formal_evidence_invalid", evidence=label, reason=str(exc))
        return None, None


def _inventory_sha(manifest: dict) -> str:
    entries = [
        {"section": section, "path": item["path"], "sha256": item["sha256"]}
        for section in ("code", "schemas") for item in manifest.get(section, [])
    ]
    return hashlib.sha256(json.dumps(entries, separators=(",", ":")).encode()).hexdigest()


def _recompute_category_gate(value: dict, errors: list[dict]) -> bool:
    try:
        bindings = value.get("classifications")
        if not isinstance(bindings, list) or not bindings:
            raise ValueError("category_classifications_missing")
        records = []
        qualifications = {}
        seen = set()
        for run_index, binding in enumerate(bindings, 1):
            classification_path = _bound_file(binding, f"category_classification_{run_index}")
            classification = _json(classification_path)
            source_run = Path(classification["source_run"]).resolve()
            if binding.get("source_run") != source_run.relative_to(WORKSPACE_ROOT).as_posix():
                raise ValueError("category_source_run_binding_changed")
            validate_classification_run(source_run, classification_path)
            for index, item in enumerate(classification["records"], 1):
                if item["case_id"] in seen:
                    raise ValueError("category_case_duplicated_across_runs")
                seen.add(item["case_id"])
                qualification = _qualification_from_source(
                    item, source_run / f"16_03_result_{index:04d}.json")
                qualification["classification"] = classification_path.relative_to(
                    WORKSPACE_ROOT).as_posix()
                qualification["classification_sha256"] = _sha(classification_path)
                qualifications[item["case_id"]] = qualification
                records.append(item)
        exclusions, expected_exclusion_binding = _load_exclusions(
            _bound_file(value["exclusions"], "category_exclusions")
            if value.get("exclusions") else None)
        if value.get("exclusions") != expected_exclusion_binding:
            raise ValueError("category_exclusion_binding_changed")
        rebuilt = summarize(records, exclusions, qualifications)
        for field in ("distribution", "qualified_count", "capability_membership_count",
                      "multi_label_count", "gate_passed", "rows"):
            if value.get(field) != rebuilt.get(field):
                raise ValueError(f"category_gate_recompute_mismatch:{field}")
        if (value.get("schema_version") != "visual-capability-distribution-v4"
                or not rebuilt["gate_passed"]):
            raise ValueError("category_gate_not_passed")
        return True
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        _error(errors, "category_gate_invalid", reason=str(exc))
        return False


def _check_full_test_run(value: dict, path: Path, freeze: dict, freeze_sha: str,
                         errors: list[dict]) -> bool:
    import unittest

    def flatten(suite):
        for item in suite:
            if isinstance(item, unittest.TestSuite):
                yield from flatten(item)
            else:
                yield item

    current_head = subprocess.check_output(
        ["git", "-C", str(WORKSPACE_ROOT), "rev-parse", "HEAD"], text=True).strip()
    discovered = sorted(test.id() for test in flatten(
        unittest.defaultTestLoader.discover(str(REPORT_ROOT / "code/tests"))))
    runtime = value.get("runtime") if isinstance(value.get("runtime"), dict) else {}
    expected_executable = REPORT_ROOT / ".runtime/venv/bin/python"
    log_binding = value.get("raw_log") if isinstance(value.get("raw_log"), dict) else {}
    try:
        log_path = _bound_file(log_binding, "full_test_raw_log")
        log_valid = (log_path == (EVIDENCE_ROOT / "final_full_test_run.log").resolve()
                     and f"Ran {len(discovered)} tests" in log_path.read_text()
                     and "\nOK\n" in log_path.read_text())
    except (OSError, TypeError, ValueError):
        log_valid = False
    valid = (
        path == (EVIDENCE_ROOT / "final_full_test_run.json").resolve()
        and value.get("schema_version") == "formal-test-run-v1"
        and value.get("status") == "passed"
        and value.get("failed") == 0 and value.get("errors") == 0
        and value.get("tests_run") == len(discovered)
        and value.get("passed") == len(discovered)
        and value.get("skipped") == 0
        and value.get("test_ids") == discovered
        and log_valid
        and runtime.get("executable")
        == expected_executable.relative_to(WORKSPACE_ROOT).as_posix()
        and runtime.get("resolved_executable_sha256") == _sha(expected_executable.resolve())
        and runtime.get("python_version") == sys.version
        and runtime.get("pythonpath") == "code"
        and runtime.get("dont_write_bytecode") == "1"
        and value.get("command") == EXPECTED_TEST_COMMAND
        and value.get("git_head") == current_head
        and value.get("test_runner_sha256") == _sha(REPORT_ROOT / "test.py")
        and value.get("pipeline_freeze_sha256") == freeze_sha
        and value.get("formal_inventory_sha256") == _inventory_sha(freeze)
    )
    if not valid:
        _error(errors, "full_test_run_not_bound_to_final_tree")
    return valid


def _check_review_gate(value: dict, freeze_sha: str, test_sha: str,
                       errors: list[dict]) -> bool:
    bindings = value.get("reviews")
    if value.get("schema_version") != "independent-review-gate-v1" or not isinstance(bindings, list):
        _error(errors, "review_gate_schema_invalid")
        return False
    records = []
    for index, binding in enumerate(bindings, 1):
        record, _ = _formal_json(binding, f"independent_review_{index}", errors,
                                 EVIDENCE_ROOT / "reviews")
        if record is not None:
            records.append(record)
    identities = {item.get("reviewer_id") for item in records}
    foci = {item.get("focus") for item in records}
    valid = (
        len(records) == 3 and len(identities) == 3 and None not in identities
        and foci == EXPECTED_REVIEW_FOCI
        and all(item.get("schema_version") == "independent-readonly-review-v1"
                and item.get("read_only") is True and item.get("status") == "passed"
                and item.get("unresolved_p0") == 0 and item.get("unresolved_p1") == 0
                and item.get("pipeline_freeze_sha256") == freeze_sha
                and item.get("full_test_run_sha256") == test_sha
                for item in records)
    )
    if not valid:
        _error(errors, "three_clean_bound_reviews_required")
    return valid


def _validate_formal_task(task: dict, global_freeze_path: Path, global_freeze_sha: str,
                          errors: list[dict]) -> tuple[str | None, bool]:
    instance_id = task.get("instance_id") if isinstance(task, dict) else None
    if not isinstance(instance_id, str):
        _error(errors, "invalid_iid_task_record")
        return None, False
    ledger, ledger_path = _formal_json(task.get("state_ledger"), f"{instance_id}:state_ledger", errors)
    frozen, frozen_path = _formal_json(task.get("frozen_manifest"), f"{instance_id}:frozen_manifest", errors)
    if ledger is None or frozen is None or ledger_path is None or frozen_path is None:
        return instance_id, False
    try:
        _validate_schema(frozen, "frozen_harbor_task_v1.schema.json", "frozen_manifest_schema_invalid")
        if (frozen.get("schema_version") != "frozen-harbor-task-v1"
                or frozen.get("state") != "frozen" or frozen.get("mode") != "real"
                or frozen.get("instance_id") != instance_id):
            raise ValueError("frozen_manifest_not_formal")
        task_path = WORKSPACE_ROOT / frozen["task"]["path"]
        _validate_frozen_task_tree(task_path, instance_id)
        checksum, files = _task_inventory(task_path)
        if checksum != frozen["task"].get("sha256") or files != frozen["task"].get("files"):
            raise ValueError("frozen_task_binding_changed")
        config_path = _bound_file(frozen.get("pass5_config", {}), "pass5_config")
        config = _json(config_path)
        _validate_schema(config, "frozen_pass5_config_v1.schema.json", "pass5_config_invalid")
        _require_formal_pass5_config(config)
        _validate_formal_job_config(config, frozen)
        if (config.get("valid_trials") != 5
                or any(config.get(field) != frozen["pass5_config"].get(field)
                       for field in ("model_id", "agent", "agent_version"))):
            raise ValueError("frozen_pass5_config_changed")
        freeze_path, freeze = _validate_pipeline_freeze(frozen.get("pipeline_freeze", {}))
        _require_formal_freeze_ready(freeze)
        if freeze_path != global_freeze_path or _sha(freeze_path) != global_freeze_sha:
            raise ValueError("task_pipeline_freeze_mismatch")
        bound_ledger_path = _bound_file(frozen.get("promotion_ledger", {}), "promotion_ledger")
        if bound_ledger_path != ledger_path or ledger.get("schema_version") != "pipeline-state-ledger-v1":
            raise ValueError("promotion_ledger_binding_mismatch")
        if (ledger.get("current_state") != "frozen" or ledger.get("status") != "completed"
                or ledger.get("mode") != "real"):
            raise ValueError("promotion_ledger_not_formal")
        _validate_promotion_chain(ledger, frozen)
        _validate_promotion_commit(task_path, ledger_path, frozen_path, instance_id)
        _replay_promotion_evidence(ledger, frozen)
        for index in (0, 1, 2, 3):
            _bound_file(ledger["events"][index].get("evidence", {}),
                        f"promotion_event_{index}_evidence")
        if (ledger["events"][0]["evidence"].get("source") != "human"
                or ledger["events"][2]["evidence"].get("source") != "human"):
            raise ValueError("human_gate_source_invalid")
        if not IMAGE_ID.fullmatch(str(frozen.get("image", {}).get("image_id", ""))):
            raise ValueError("frozen_image_identity_invalid")
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        _error(errors, "formal_task_invalid", instance_id=instance_id, reason=str(exc))
        return instance_id, False

    summary_binding, audit_binding = task.get("pass5_summary"), task.get("pass5_audit")
    if summary_binding is None and audit_binding is None:
        return instance_id, False
    try:
        summary_path = _bound_file(summary_binding or {}, f"{instance_id}:pass5_summary")
        audit_path = _bound_file(audit_binding or {}, f"{instance_id}:pass5_audit")
        summary, stored_audit = _json(summary_path), _json(audit_path)
        _validate_schema(summary, "frozen_pass5_summary_v1.schema.json", "pass5_summary_schema_invalid")
        if (summary.get("mode") != "real" or summary.get("state") != "pass5_completed"
                or summary.get("instance_id") != instance_id
                or summary.get("valid_trial_count") != 5
                or summary.get("frozen_manifest", {}).get("sha256") != _sha(frozen_path)):
            raise ValueError("real_pass5_summary_binding_invalid")
        recomputed = _audit_pass5_summary(summary, frozen, config)
        comparable = dict(stored_audit)
        summary_audit_binding = comparable.pop("summary", None)
        if (comparable != recomputed or (summary_audit_binding or {}).get("sha256") != _sha(summary_path)):
            raise ValueError("pass5_audit_recompute_mismatch")
        completion_ledger_path = _bound_file(summary.get("state_ledger", {}), "completion_ledger")
        completion_ledger = _json(completion_ledger_path)
        events = completion_ledger.get("events", [])
        promotion_identity = {key: value for key, value in ledger.items()
                              if key not in {"current_state", "status", "events"}}
        completion_identity = {key: value for key, value in completion_ledger.items()
                               if key not in {"current_state", "status", "events"}}
        if (completion_ledger.get("current_state") != "pass5_completed"
                or completion_ledger.get("status") != "completed"
                or completion_ledger.get("mode") != "real" or len(events) != 6
                or events[:5] != ledger.get("events")
                or completion_identity != promotion_identity
                or events[-1].get("from") != "frozen"
                or events[-1].get("to") != "pass5_completed"
                or events[-1].get("status") != "accepted"
                or events[-1].get("code") != "five_valid_trials_completed"
                or events[-1].get("evidence") != {
                    "valid_trial_count": summary["valid_trial_count"],
                    "infrastructure_invalid_count": summary["infrastructure_invalid_count"],
                    "answer_leakage_invalid_count": summary["answer_leakage_invalid_count"],
                    "success_count": summary["success_count"],
                }):
            raise ValueError("pass5_completion_ledger_invalid")
        return instance_id, True
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        _error(errors, "real_pass5_invalid", instance_id=instance_id, reason=str(exc))
        return instance_id, False


def validate(packet_path: Path) -> dict:
    packet_path = packet_path.absolute()
    errors: list[dict] = []
    expected_packet = (EVIDENCE_ROOT / "completion_packet.json").absolute()
    if packet_path != expected_packet or packet_path.is_symlink():
        return {
            "schema_version": "visual-exam-completion-audit-v1",
            "status": "not_complete",
            "packet": {"path": str(packet_path), "sha256": None},
            "observed": {"formal_iid_tasks": 0, "real_pass5_tasks": 0,
                         "category_gate_passed": False,
                         "full_test_run_passed": False,
                         "static_submission_status": "not_checked"},
            "errors": [{"code": "completion_packet_not_in_formal_evidence"}],
        }
    assert_no_symlink_chain(packet_path.parent)
    packet = json.loads(packet_path.read_text())
    if packet.get("schema_version") != "visual-exam-completion-packet-v1" or packet.get("report_root") != "report":
        _error(errors, "completion_packet_schema_invalid")

    static = validate_submission(REPORT_ROOT, minimum_tasks=5)
    if static.get("status") != "static_layout_complete_not_exam_ready":
        _error(errors, "static_submission_contract_failed",
               findings=static.get("errors", []))
    static_ids = {item["instance_id"] for item in static.get("tasks", [])
                  if item.get("status") == "valid_static_contract"}

    try:
        freeze_path, freeze = _validate_pipeline_freeze(packet.get("pipeline_freeze", {}))
        _require_formal_freeze_ready(freeze)
        freeze_sha = _sha(freeze_path)
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        _error(errors, "pipeline_freeze_not_ready", reason=str(exc))
        freeze_path, freeze, freeze_sha = Path(), {}, ""

    category, _ = _formal_json(packet.get("category_distribution"), "category_distribution", errors)
    category_passed = _recompute_category_gate(category, errors) if category else False
    tests, tests_path = _formal_json(packet.get("full_test_run"), "full_test_run", errors)
    tests_passed = bool(tests and tests_path and freeze
                        and _check_full_test_run(tests, tests_path, freeze, freeze_sha, errors))
    reviews, _ = _formal_json(packet.get("review_gate"), "review_gate", errors)
    if reviews and tests_path:
        _check_review_gate(reviews, freeze_sha, _sha(tests_path), errors)

    task_records = packet.get("iid_tasks")
    if not isinstance(task_records, list):
        _error(errors, "iid_task_records_missing")
        task_records = []
    task_ids, real_pass5 = [], 0
    for task in task_records:
        instance_id, passed = _validate_formal_task(task, freeze_path, freeze_sha, errors)
        if instance_id:
            task_ids.append(instance_id)
        real_pass5 += int(passed)
    unique_ids = set(task_ids)
    if len(unique_ids) < 5 or not unique_ids <= static_ids:
        _error(errors, "formal_iid_task_quota_not_met", observed=len(unique_ids),
               static_valid=len(static_ids), required=5)
    if len(task_ids) != len(unique_ids):
        _error(errors, "duplicate_iid_task_record")
    if real_pass5 < 1:
        _error(errors, "real_pass5_evidence_missing")

    try:
        html_path = _bound_file(packet.get("submission_html", {}), "submission_html")
        if html_path != (EVIDENCE_ROOT / "final_pipeline_audit.html").resolve():
            raise ValueError("submission_html_path_invalid")
        text = html_path.read_text()
        required = [*CATEGORIES, *task_ids, "Pass@5", "F2P", "P2P", "人工"]
        missing = [marker for marker in required if marker not in text]
        if missing or "<script" in text.lower() or re.search(r"\son[a-z]+\s*=", text, re.I):
            raise ValueError("submission_html_static_audit_failed:" + ",".join(missing))
    except (OSError, TypeError, ValueError) as exc:
        _error(errors, "submission_html_invalid", reason=str(exc))

    return {
        "schema_version": "visual-exam-completion-audit-v1",
        "status": "passed" if not errors else "not_complete",
        "packet": {"path": str(packet_path), "sha256": _sha(packet_path)},
        "observed": {"formal_iid_tasks": len(unique_ids), "real_pass5_tasks": real_pass5,
                     "category_gate_passed": category_passed,
                     "full_test_run_passed": tests_passed,
                     "static_submission_status": static.get("status")},
        "errors": errors,
    }


def run(packet_path: Path, output: Path) -> dict:
    result = validate(packet_path)
    output = output.absolute()
    if (output != (EVIDENCE_ROOT / "completion_audit.json").absolute()
            or output.is_symlink()):
        raise ValueError("completion_audit_output_not_formal")
    assert_no_symlink_chain(output.parent)
    write_json(output, result)
    return result
