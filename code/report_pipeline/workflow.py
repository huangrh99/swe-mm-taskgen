"""Promotion and Pass@5 state machine for final visual Harbor tasks.

This module coordinates existing task, control, Docker, and Harbor artifacts. It
does not implement another Harbor agent adapter.
"""

from __future__ import annotations

import hashlib
import html
import json
import mmap
import os
import re
import secrets
import shutil
import stat
import subprocess
import fcntl
from datetime import datetime
from pathlib import Path

from report_pipeline.paths import (
    CASES_ROOT, REPORT_ROOT, RUNTIME_ROOT, RUNS_ROOT, TMP_ROOT, WORKSPACE_ROOT,
)
from report_pipeline.calibration import validate_human_gate_audit
from report_pipeline.atomic import assert_no_symlink_chain, write_json
from report_pipeline.trial_security import audit_trial_trace, trace_files


STATES = (
    "candidate",
    "visual_approved",
    "tests_measured",
    "tests_approved",
    "harbor_controls_passed",
    "frozen",
    "pass5_completed",
)
TRANSITION_CONTRACTS = (
    ("candidate", "visual_approved", "candidate inventory + visual human record", "visual gate evidence binding", "candidate_task_checksum_mismatch; visual_gate_not_approved; visual_gate_source_not_allowed; visual_gate_binding_*") ,
    ("visual_approved", "tests_measured", "measurement record + nonempty disjoint F2P/P2P", "measured IDs and evidence binding", "measurement_binding_*; test_measurement_invalid"),
    ("tests_measured", "tests_approved", "F2P/P2P human record", "tests gate evidence binding", "tests_gate_not_approved; tests_gate_source_not_allowed; tests_gate_binding_*"),
    ("tests_approved", "harbor_controls_passed", "same task SHA controls", "empty=0, gold=1, exception=0", "controls_binding_*; harbor_controls_invalid"),
    ("harbor_controls_passed", "frozen", "Pass@5 config + image build binding", "published task, image ID, freeze manifest", "pass5_config_*; destination/staging exists; copy mismatch; image build/identity invalid"),
    ("frozen", "pass5_completed", "unchanged freeze + exact real authorization", "five valid trials, trajectories, summary", "freeze/config/image/authorization drift; invalid verifier/trajectory; replacement budget exhausted"),
)
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
SHA256 = re.compile(r"[0-9a-f]{64}")
SECRET_MARKERS = (
    b"-----BEGIN PRIVATE KEY", b"-----BEGIN OPENSSH PRIVATE KEY",
    b"-----BEGIN RSA PRIVATE KEY", b"-----BEGIN EC PRIVATE KEY", b"AKIA",
)
SECRET_PATTERN = re.compile(
    rb'(?i)(api[_-]?key|authorization|access[_-]?token|refresh[_-]?token|token|secret|password|credentials)'
    rb'["\']?\s*[:=]\s*["\']?(?:bearer\s+)?[A-Za-z0-9._/+\-=]{12,}'
)
STANDALONE_SECRET_PATTERN = re.compile(
    rb"(?:gh[pousr]_[A-Za-z0-9]{20,}|"
    rb"xox[baprs]-[A-Za-z0-9-]{16,}|"
    rb"npm_[A-Za-z0-9]{20,}|"
    rb"glpat-[A-Za-z0-9_-]{20,}|"
    rb"AIza[A-Za-z0-9_-]{30,}|"
    rb"sk_live_[A-Za-z0-9]{16,}|"
    rb"sk-(?:proj|svcacct)-[A-Za-z0-9_-]{16,}|"
    rb"sk-ant-[A-Za-z0-9_-]{16,}|"
    rb"sk-[A-Za-z0-9]{20,}|"
    rb"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})"
)
TASK_FILE_LIMIT = 64 * 1024 * 1024
TASK_TOTAL_LIMIT = 256 * 1024 * 1024
TRAJECTORY_FILE_LIMIT = 32 * 1024 * 1024
TRAJECTORY_TOTAL_LIMIT = 128 * 1024 * 1024
OFFICIAL_K3_MODEL_ID = "ep-20260817150115-9fx8h"
OFFICIAL_K3_AGENT = "kimi-code"
OFFICIAL_K3_AGENT_VERSION = "0.29.0"
OFFICIAL_K3_PROVIDER_PROFILE = {
    "protocol": "responses",
    "base_url": "https://ark-cn-beijing.bytedance.net/api/v3",
    "allowed_host": "ark-cn-beijing.bytedance.net",
    "credential_env": "ARK_API_KEY",
    "capabilities": ["image_in", "thinking"],
}
OFFICIAL_K3_AGENT_RUNTIME = {
    "max_context_size": 1048576,
    # The provider accepts at most 131072 output tokens. Omitting this value
    # makes Kimi Code 0.29.0 incorrectly reuse the context window as the
    # completion limit, which the provider rejects.
    "max_completion_tokens": 131072,
    "thinking_effort": "max",
    "max_steps_per_turn": 0,
    "timeout_sec": 7200,
    "setup_timeout_sec": 1800,
}
# Harbor 0.22 treats every env key containing ``TOKEN`` as sensitive and then
# replaces the env value globally in text artifacts. A plain ``"131072"``
# therefore corrupts native Kimi wire JSON whenever the numeric request field
# contains 131072. Kimi Code 0.29.0 parses this env var with ``Number(value)``;
# a leading plus is integer-equivalent for the CLI while keeping Harbor's
# redaction needle distinct from bare JSON numbers. This representation is a
# pinned part of the formal runtime contract, not a user-configurable spelling.
OFFICIAL_K3_MAX_COMPLETION_TOKENS_ENV = (
    f"+{OFFICIAL_K3_AGENT_RUNTIME['max_completion_tokens']}"
)
OFFICIAL_K3_AGENT_HOSTS = [OFFICIAL_K3_PROVIDER_PROFILE["allowed_host"]]
OFFICIAL_K3_TOOL_POLICY = {
    # Kimi Code 0.29.0 has no CLI/config switch that removes its built-in
    # WebSearch and FetchURL tools. Formal runs therefore rely on runtime
    # egress denial plus fail-closed trajectory auditing for those tools.
    "enforcement": "runtime_network_deny_and_trace_rejection",
    "hosted_tools": "registered_but_runtime_denied_and_trace_rejected",
    "mcp_servers": [],
    "skills": [],
    "forbidden_tools": [
        "WebSearch", "FetchURL", "browser", "remote_mcp", "connectors",
        "file_search",
    ],
}

OFFICIAL_CODEX_MODEL_ID = "gpt-5.6-luna"
OFFICIAL_CODEX_AGENT = "codex"
OFFICIAL_CODEX_AGENT_VERSION = "0.148.0"
OFFICIAL_CODEX_AGENT_HOSTS = [
    "api.openai.com",
    "auth.openai.com",
    "chatgpt.com",
]
OFFICIAL_CODEX_COMPOSE_OVERLAY = {
    "path": "reproducibility/11_codex_auth_read_search.compose.yaml",
    "sha256": "c96ee46fbc780fd1bca4d93d6d19e2dbf1486813478c344971cdef10afaf6f7d",
}
OFFICIAL_CODEX_PROVIDER_PROFILE = {
    "protocol": "codex_cli_chatgpt_auth",
    "allowed_hosts": OFFICIAL_CODEX_AGENT_HOSTS,
    "credential_source": "harbor_auth_json",
    "capabilities": ["image_in", "thinking"],
    "compose_overlay": OFFICIAL_CODEX_COMPOSE_OVERLAY,
}
OFFICIAL_CODEX_AGENT_RUNTIME = {
    # Codex discovers the selected model's full context window. Zero means no
    # workflow-side truncation rather than a literal zero-token limit.
    "max_context_size": 0,
    "max_completion_tokens": 0,
    "thinking_effort": "max",
    "max_steps_per_turn": 0,
    "timeout_sec": 7200,
    "setup_timeout_sec": 1800,
}
OFFICIAL_CODEX_DISABLED_FEATURES = {
    "apps": False,
    "browser_use": False,
    "browser_use_external": False,
    "browser_use_full_cdp_access": False,
    "enable_mcp_apps": False,
    "image_generation": False,
    "in_app_browser": False,
    "mcp_2026_07_28": False,
    "non_prefixed_mcp_tool_names": False,
    "plugin_sharing": False,
    "plugins": False,
    "recommended_plugins": False,
    "remote_plugin": False,
    "skill_mcp_dependency_install": False,
    "skill_search": False,
    "standalone_web_search": False,
    "tool_call_mcp_elicitation": False,
    "tool_suggest": False,
    "web_search_cached": False,
    "web_search_request": False,
}
OFFICIAL_CODEX_TOOL_POLICY = {
    "enforcement": "cli_config_and_trace_rejection",
    "hosted_tools": "disabled",
    "mcp_servers": [],
    "skills": [],
    "forbidden_tools": [
        "web_search", "browser", "remote_mcp", "connectors", "file_search",
    ],
}


def _require_official_k3_config(config: dict) -> None:
    """Reject formal Pass@5 groups that drift from the approved Kimi K3 identity."""
    if (config.get("model_id") != OFFICIAL_K3_MODEL_ID
            or config.get("agent") != OFFICIAL_K3_AGENT
            or str(config.get("agent_version")) != OFFICIAL_K3_AGENT_VERSION
            or config.get("provider_profile") != OFFICIAL_K3_PROVIDER_PROFILE
            or config.get("agent_runtime") != OFFICIAL_K3_AGENT_RUNTIME
            or config.get("tool_policy") != OFFICIAL_K3_TOOL_POLICY
            or config.get("network_policy") != {
                "environment_hosts": [],
                "agent_hosts": OFFICIAL_K3_AGENT_HOSTS,
            }):
        raise ValueError("formal_pass5_config_not_official_kimi_k3")


def _require_official_codex_config(config: dict) -> None:
    """Reject Codex groups that drift from the frozen Luna Max profile."""
    if (config.get("model_id") != OFFICIAL_CODEX_MODEL_ID
            or config.get("agent") != OFFICIAL_CODEX_AGENT
            or str(config.get("agent_version")) != OFFICIAL_CODEX_AGENT_VERSION
            or config.get("provider_profile") != OFFICIAL_CODEX_PROVIDER_PROFILE
            or config.get("agent_runtime") != OFFICIAL_CODEX_AGENT_RUNTIME
            or config.get("tool_policy") != OFFICIAL_CODEX_TOOL_POLICY
            or config.get("network_policy") != {
                "environment_hosts": [],
                "agent_hosts": OFFICIAL_CODEX_AGENT_HOSTS,
            }):
        raise ValueError("formal_pass5_config_not_official_codex_luna_max")
    _bound_file(OFFICIAL_CODEX_COMPOSE_OVERLAY, "codex_compose_overlay")


def _formal_provider_kind(config: dict) -> str:
    identity = (config.get("agent"), config.get("model_id"))
    if identity == (OFFICIAL_K3_AGENT, OFFICIAL_K3_MODEL_ID):
        return "kimi_k3"
    if identity == (OFFICIAL_CODEX_AGENT, OFFICIAL_CODEX_MODEL_ID):
        return "codex_luna_max"
    raise ValueError("formal_pass5_provider_profile_not_supported")


def _require_offline_agent_image(frozen: dict, provider_kind: str) -> None:
    """Require the frozen task image to carry the pinned CLI before trial setup."""
    task_value = frozen.get("task", {}).get("path")
    raw = Path(str(task_value or ""))
    task = (WORKSPACE_ROOT / raw).resolve()
    dockerfile = task / "environment/Dockerfile"
    if (raw.is_absolute() or ".." in raw.parts or not dockerfile.is_file()
            or not task.is_relative_to(WORKSPACE_ROOT.resolve())):
        raise ValueError("formal_agent_image_prerequisite_missing")
    effective = "\n".join(
        line for line in dockerfile.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )
    run_text = "\n".join(re.findall(
        r"(?ims)^RUN\s+.*?(?=^[A-Z][A-Z0-9_-]*\s+|\Z)", effective
    ))
    requirements = {
        "kimi_k3": (
            f"@moonshot-ai/kimi-code@{OFFICIAL_K3_AGENT_VERSION}",
            "command -v kimi",
            "kimi --version",
            "npm config set offline true",
            "apk is disabled in this frozen glibc image",
        ),
        "codex_luna_max": (
            f"@openai/codex@{OFFICIAL_CODEX_AGENT_VERSION}",
            "command -v codex",
            "codex --version",
        ),
    }[provider_kind]
    if any(value not in run_text for value in requirements):
        raise ValueError(f"formal_{provider_kind}_offline_agent_image_prerequisite_missing")
def _require_formal_pass5_config(config: dict) -> str:
    """Validate and return one of the two frozen formal provider profiles."""
    kind = _formal_provider_kind(config)
    if kind == "kimi_k3":
        _require_official_k3_config(config)
    else:
        _require_official_codex_config(config)
    return kind


def _expected_harbor_binding(config: dict) -> dict:
    raw = Path(str(config.get("harbor_executable", "")))
    if (raw.is_absolute() or ".." in raw.parts
            or not SHA256.fullmatch(str(config.get("harbor_executable_sha256", "")))
            or not isinstance(config.get("harbor_version"), str)):
        raise ValueError("harbor_executable_invalid")
    path = (WORKSPACE_ROOT / raw).resolve()
    if not path.is_file() or not path.is_relative_to(RUNTIME_ROOT.resolve()):
        raise ValueError("harbor_executable_invalid")
    return {"path": path, "sha256": config["harbor_executable_sha256"],
            "version": config["harbor_version"]}


def _formal_source_inventory() -> tuple[set[str], set[str]]:
    """Return the closed production-source/schema surface that must be frozen."""
    code_roots = [
        REPORT_ROOT / "code/report_pipeline",
        REPORT_ROOT / "code/pr_crawler",
        REPORT_ROOT / "code/analysis/scripts",
        REPORT_ROOT / "code/harbor_tests",
        REPORT_ROOT / "code/tests",
    ]
    code = {
        path.relative_to(WORKSPACE_ROOT).as_posix()
        for root in code_roots for path in root.rglob("*.py") if path.is_file()
    }
    prompt_root = REPORT_ROOT / "code/analysis/prompts"
    code.update(path.relative_to(WORKSPACE_ROOT).as_posix()
                for path in prompt_root.glob("*.system.md") if path.is_file())
    code.update({
        "run.py",
        "reproducibility/10_refresh_pipeline_freeze.py",
        OFFICIAL_CODEX_COMPOSE_OVERLAY["path"],
    })
    schemas = {
        path.relative_to(WORKSPACE_ROOT).as_posix()
        for path in (REPORT_ROOT / "schemas").glob("*.schema.json")
        if path.is_file()
    }
    schemas.update(path.relative_to(WORKSPACE_ROOT).as_posix()
                   for path in prompt_root.glob("*.schema.json") if path.is_file())
    return code, schemas


REQUIRED_FREEZE_CODE, REQUIRED_FREEZE_SCHEMAS = _formal_source_inventory()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json(path: Path) -> dict:
    return json.loads(path.resolve().read_text())


def _validate_schema(value: dict, filename: str, code: str) -> None:
    import jsonschema

    schema = _json(REPORT_ROOT / "schemas" / filename)
    try:
        jsonschema.Draft202012Validator(schema).validate(value)
    except jsonschema.ValidationError as exc:
        raise ValueError(code) from exc


def _portable(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(WORKSPACE_ROOT.resolve()).as_posix()
    except ValueError:
        raise ValueError(f"artifact path is outside the workspace: {resolved}") from None


def _portable_lexical(path: Path) -> str:
    """Render an in-workspace artifact path without following a hostile symlink."""
    absolute = path.absolute()
    try:
        return absolute.relative_to(WORKSPACE_ROOT.absolute()).as_posix()
    except ValueError:
        return absolute.name


def _task_inventory(task: Path) -> tuple[str, dict[str, str]]:
    files = {
        path.relative_to(task).as_posix(): _sha256(path)
        for path in sorted(task.rglob("*"))
        if path.is_file()
    }
    entries = [{"path": name, "sha256": value} for name, value in sorted(files.items())]
    checksum = hashlib.sha256(
        json.dumps(entries, separators=(",", ":")).encode()
    ).hexdigest()
    return checksum, files


def _file_contains_secret(path: Path) -> bool:
    with path.open("rb") as stream:
        if path.stat().st_size == 0:
            return False
        with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as payload:
            return (any(payload.find(marker) >= 0 for marker in SECRET_MARKERS)
                    or SECRET_PATTERN.search(payload) is not None
                    or STANDALONE_SECRET_PATTERN.search(payload) is not None)


def _bytes_contains_secret(payload: bytes) -> bool:
    return (any(marker in payload for marker in SECRET_MARKERS)
            or SECRET_PATTERN.search(payload) is not None
            or STANDALONE_SECRET_PATTERN.search(payload) is not None)


def _sensitive_filename(path: Path) -> bool:
    name = path.name.lower()
    return (name in {".npmrc", ".pypirc", ".netrc", "credentials", "api_keys"}
            or name.startswith(".env"))


def _bound_file(record: dict, label: str) -> Path:
    path_value, expected = record.get("path"), record.get("sha256")
    if not isinstance(path_value, str) or not isinstance(expected, str):
        raise ValueError(f"{label}_binding_missing")
    raw = Path(path_value)
    if raw.is_absolute() or ".." in raw.parts:
        raise ValueError(f"{label}_binding_invalid")
    unresolved = WORKSPACE_ROOT / raw
    path = unresolved.resolve()
    relative_parts = unresolved.relative_to(WORKSPACE_ROOT).parts
    chain = [WORKSPACE_ROOT.joinpath(*relative_parts[:index])
             for index in range(1, len(relative_parts) + 1)]
    if (not path.is_relative_to(WORKSPACE_ROOT.resolve()) or not path.is_file()
            or any(item.is_symlink() for item in chain)):
        raise ValueError(f"{label}_binding_invalid")
    if _sha256(path) != expected:
        raise ValueError(f"{label}_binding_changed")
    return path


def _validate_task_tree(task: Path) -> None:
    if task.is_symlink() or not task.resolve().is_relative_to(TMP_ROOT.resolve()):
        raise ValueError("candidate_task_must_be_provisional_tmp")
    required = {"environment", "instruction.md", "solution", "task.toml", "tests"}
    observed = {path.name for path in task.iterdir()}
    if observed != required:
        raise ValueError("candidate_task_top_level_invalid")
    forbidden_names = {".git", "secrets", "id_rsa", "id_ed25519"}
    total_size = 0
    for path in [task, *sorted(task.rglob("*"))]:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise ValueError("candidate_task_symlink_or_special_file")
        if not path.resolve().is_relative_to(task.resolve()):
            raise ValueError("candidate_task_path_escape")
        if (path.name.lower() in forbidden_names or _sensitive_filename(path)
                or path.suffix.lower() in {".pem", ".key", ".p12"}):
            raise ValueError("candidate_task_forbidden_file")
        if path.is_file():
            size = path.stat().st_size
            total_size += size
            if size > TASK_FILE_LIMIT or total_size > TASK_TOTAL_LIMIT:
                raise ValueError("candidate_task_size_budget_exceeded")
            if _file_contains_secret(path):
                raise ValueError("candidate_task_secret_marker")
    from report_pipeline.harbor_export import validate_publication
    publication = validate_publication(task)
    checksum, _ = _task_inventory(task)
    if publication.get("task_material_sha256") != checksum:
        raise ValueError("candidate_task_export_publication_mismatch")


def _validate_frozen_task_tree(task: Path, instance_id: str) -> None:
    from report_pipeline.task_projection import materialize

    source = (CASES_ROOT / instance_id).resolve()
    expected = materialize(source)["path"].resolve()
    if (task.absolute() != expected or task.is_symlink()
            or task.resolve() != expected
            or not expected.is_relative_to((TMP_ROOT / "harbor-task-projections").resolve())):
        raise ValueError("frozen_task_path_invalid")
    checksum, _ = _task_inventory(expected)
    if expected.name != checksum:
        raise ValueError("frozen_task_projection_checksum_mismatch")
    for path in [task, *sorted(task.rglob("*"))]:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise ValueError("frozen_task_symlink_or_special_file")
        if not path.resolve().is_relative_to(task.resolve()):
            raise ValueError("frozen_task_path_escape")


def _pass5_output_allowed(instance_id: str, output: Path, *, simulation: bool) -> bool:
    """Keep simulations disposable and real traces beside their owning case."""
    allowed = (RUNS_ROOT.resolve() if simulation else
               (CASES_ROOT / instance_id / "outputs" / "07_pass5").resolve())
    return output != allowed and output.is_relative_to(allowed)


def _task_test_ids(task: Path) -> tuple[list[str], list[str]]:
    config_path = task / "tests/config.json"
    if not config_path.is_file():
        raise ValueError("candidate_task_test_config_missing")
    config = _json(config_path)
    f2p, p2p = config.get("FAIL_TO_PASS"), config.get("PASS_TO_PASS")
    if not isinstance(f2p, list) or not isinstance(p2p, list):
        raise ValueError("candidate_task_test_inventory_invalid")
    return f2p, p2p


def _expected_test_rows(f2p: list[str], p2p: list[str]) -> list[dict]:
    return ([{"test_id": test_id, "class": "F2P"} for test_id in f2p]
            + [{"test_id": test_id, "class": "P2P"} for test_id in p2p])


def _validate_verifier_details(details: dict, expected_rows: list[dict]) -> tuple[int, list[str]]:
    results = details.get("results")
    if not isinstance(results, list):
        raise ValueError("verifier_results_missing")
    observed_rows = [{"test_id": item.get("test_id"), "class": item.get("class")}
                     for item in results if isinstance(item, dict)]
    if len(observed_rows) != len(results) or observed_rows != expected_rows:
        raise ValueError("verifier_test_inventory_mismatch")
    statuses = [item.get("status") for item in results]
    allowed = {"pass", "fail", "skip", "missing", "error"}
    if any(status not in allowed for status in statuses):
        raise ValueError("verifier_test_status_invalid")
    counts = {name: statuses.count(name) for name in allowed}
    summary = details.get("summary")
    if not isinstance(summary, dict) or any(summary.get(name) != count for name, count in counts.items()):
        raise ValueError("verifier_summary_mismatch")
    if details.get("contract_errors") not in (None, []):
        raise ValueError("verifier_contract_error")
    reward = 1 if statuses and all(status == "pass" for status in statuses) else 0
    if details.get("reward") != reward:
        raise ValueError("verifier_reward_mismatch")
    return reward, statuses


def _validate_negative_controls(binding: dict, task_sha256: str,
                                simulation: bool,
                                expected_harbor: dict | None = None) -> dict:
    path = _bound_file(binding, "negative_controls")
    if not simulation and path.is_relative_to(TMP_ROOT.resolve()):
        raise ValueError("negative_controls_temporary_evidence_not_allowed")
    value = _json(path)
    from report_pipeline.harbor_negative_controls import (
        CONTROL_SPECS, _expected, _material_checksum,
    )
    required = [kind for _name, kind, _agent in CONTROL_SPECS]
    controls = value.get("controls")
    if not simulation and (not isinstance(expected_harbor, dict)
                           or set(expected_harbor) != {"path", "sha256", "version"}):
        raise ValueError("negative_controls_frozen_harbor_required")
    if (value.get("schema_version") != "visual-harbor-negative-controls-v1"
            or value.get("status") != "all_controls_passed"
            or value.get("canonical_task_material_sha256") != task_sha256
            or value.get("completed_controls") != len(required)
            or not isinstance(controls, dict) or list(controls) != required):
        raise ValueError("negative_controls_inventory_invalid")
    for kind in required:
        record = controls[kind]
        if not isinstance(record, dict) or record.get("control_passed") is not True:
            raise ValueError(f"negative_control_not_passed:{kind}")
        if not simulation:
            raw = record.get("raw")
            if not isinstance(raw, dict) or set(raw) != {
                    "control_manifest", "frozen_inventory", "job_config", "command_log",
                    "command_receipt", "harbor_executable", "job_result", "trial_result",
                    "verifier_result", "exception_log"}:
                raise ValueError(f"negative_control_raw_bindings_invalid:{kind}")
            control_manifest_path = _bound_file(
                raw["control_manifest"], f"negative_{kind}_control_manifest")
            inventory_path = _bound_file(
                raw["frozen_inventory"], f"negative_{kind}_frozen_inventory")
            job_config_path = _bound_file(raw["job_config"], f"negative_{kind}_job_config")
            command_log_path = _bound_file(raw["command_log"], f"negative_{kind}_command_log")
            receipt_path = _bound_file(
                raw["command_receipt"], f"negative_{kind}_command_receipt")
            harbor_path = _bound_file(
                raw["harbor_executable"], f"negative_{kind}_harbor_executable")
            job_result_path = _bound_file(raw["job_result"], f"negative_{kind}_job_result")
            trial_result_path = _bound_file(
                raw["trial_result"], f"negative_{kind}_trial_result")
            trial_path = trial_result_path.parent
            job_path = trial_path.parent
            manifest = _json(control_manifest_path)
            inventory = _json(inventory_path)
            job_config = _json(job_config_path)
            receipt = _json(receipt_path)
            job_result = _json(job_result_path)
            native = _json(trial_result_path)
            verifier_binding = raw.get("verifier_result")
            verifier_path = (_bound_file(verifier_binding, f"negative_{kind}_verifier_result")
                             if verifier_binding else None)
            details = _json(verifier_path) if verifier_path else None
            exception_binding = raw.get("exception_log")
            exception_path = (_bound_file(exception_binding, f"negative_{kind}_exception_log")
                              if exception_binding else None)
            raw_secret_paths = [command_log_path, receipt_path, job_result_path,
                                trial_result_path]
            if verifier_path:
                raw_secret_paths.append(verifier_path)
            if exception_path:
                raw_secret_paths.append(exception_path)
            if any(_file_contains_secret(item) for item in raw_secret_paths):
                raise ValueError(f"negative_control_raw_secret_detected:{kind}")
            native_exception = native.get("exception_info")
            configured_tasks = job_config.get("tasks")
            configured_task = (Path(configured_tasks[0].get("path", "")).resolve()
                               if isinstance(configured_tasks, list)
                               and len(configured_tasks) == 1
                               and isinstance(configured_tasks[0], dict) else None)
            public_exception = ({key: native_exception.get(key) for key in
                                 ("exception_type", "exception_message", "occurred_at")}
                                if native_exception else None)
            expected_agent = next(
                agent for _name, candidate_kind, agent in CONTROL_SPECS
                if candidate_kind == kind)
            configured_agents = job_config.get("agents")
            expected_job_agents = ([{"name": "nop"}] if expected_agent == "nop" else None)
            native_config = native.get("config") if isinstance(native.get("config"), dict) else {}
            native_task = (native_config.get("task")
                           if isinstance(native_config.get("task"), dict) else {})
            native_agent = (native_config.get("agent")
                            if isinstance(native_config.get("agent"), dict) else {})
            native_reward = ((native.get("verifier_result") or {}).get("rewards") or {}).get("reward")
            checksum, files = _material_checksum(control_manifest_path.parent)
            from dirhash import dirhash
            native_task_checksum = dirhash(control_manifest_path.parent, "sha256")
            stats = job_result.get("stats") if isinstance(job_result.get("stats"), dict) else {}
            if (any(path.is_relative_to(TMP_ROOT.resolve()) for path in (
                    control_manifest_path, inventory_path, job_config_path, command_log_path,
                    receipt_path, job_result_path, trial_result_path))
                    or not isinstance(record.get("verifier_reached"), bool)
                    or record.get("task") != _portable(control_manifest_path.parent)
                    or record.get("trial") != _portable(trial_path)
                    or record.get("job") != _portable(job_path)
                    or manifest.get("control_kind") != kind
                    or manifest.get("task_material_sha256")
                       != record.get("task_material_sha256")
                    or manifest.get("parent_task_material_sha256") != task_sha256
                    or manifest.get("files") != files
                    or manifest.get("task_material_sha256") != checksum
                    or inventory_path != control_manifest_path.parent / "tests/frozen_inventory.json"
                    or record.get("expected_tests") != inventory.get("expected_tests")
                    or configured_task != control_manifest_path.parent.resolve()
                    or job_config.get("job_name") != record.get("indexed_name")
                    or job_config.get("n_concurrent_trials") != 1
                    or configured_agents != expected_job_agents
                    or receipt.get("schema_version") != "harbor-command-receipt-v1"
                    or receipt.get("argv") != [str(harbor_path), "run", "-c", str(job_config_path)]
                    or receipt.get("returncode") != record.get("command_returncode")
                    or receipt.get("combined_log_sha256") != _sha256(command_log_path)
                    or _file_contains_secret(command_log_path)
                    or (not simulation and (
                        harbor_path != expected_harbor["path"]
                        or _sha256(harbor_path) != expected_harbor["sha256"]
                        or receipt.get("harbor_version") != expected_harbor["version"]))
                    or job_result_path != job_path / "result.json"
                    or job_result.get("n_total_trials") != 1
                    or not job_result.get("finished_at")
                    or stats.get("n_completed_trials") != 1
                    or stats.get("n_errored_trials") != int(native_exception is not None)
                    or any(stats.get(name) != 0 for name in (
                        "n_running_trials", "n_pending_trials", "n_cancelled_trials", "n_retries"))
                    or Path(str(job_config.get("jobs_dir", ""))).resolve() != job_path.parent.resolve()
                    or native.get("config", {}).get("job_id") != job_result.get("id")
                    or native.get("trial_name") != trial_path.name
                    or Path(str(native.get("task_id", {}).get("path", ""))).resolve()
                       != control_manifest_path.parent.resolve()
                    or Path(str(native_task.get("path", ""))).resolve()
                       != control_manifest_path.parent.resolve()
                    or native_agent.get("name") != expected_agent
                    or native.get("agent_info", {}).get("name") != expected_agent
                    or native.get("task_checksum") != native_task_checksum
                    or (details is not None and native_reward != details.get("reward"))
                    or record.get("harbor_exception") != public_exception
                    or record.get("raw_exception_log")
                       != (_portable(exception_path) if exception_path else None)
                    or (exception_path is not None
                        and exception_path != trial_path / "exception.txt")
                    or (exception_path is None) != (native_exception is None)
                    or (verifier_path is not None
                        and verifier_path != trial_path / "verifier/test_results.json")
                    or (details is None) != (record.get("verifier_reached") is False)):
                raise ValueError(f"negative_control_raw_identity_invalid:{kind}")
            if details is not None and (
                    record.get("reward") != details.get("reward")
                    or record.get("summary") != details.get("summary")
                    or record.get("contract_errors") != details.get("contract_errors")
                    or record.get("results") != details.get("results")):
                raise ValueError(f"negative_control_raw_verifier_mismatch:{kind}")
            passed, expectation = _expected(kind, record)
            if not passed or record.get("expected_outcome") != expectation:
                raise ValueError(f"negative_control_semantics_invalid:{kind}")
    return {"path": _portable(path), "sha256": _sha256(path)}


def _validate_measurement_run(path: Path, side: str, repetition: int,
                              expected_rows: list[dict], manifest_sha256: str,
                              task_sha256: str, test_payload_sha256: str,
                              repository: str, baseline_commit: str,
                              reference_commit: str, image_id: str,
                              simulation: bool) -> tuple[str, str]:
    run = _json(path)
    if not simulation:
        _validate_schema(run, "pipeline_test_side_run_v1.schema.json",
                         "test_measurement_run_schema_invalid")
    if (run.get("schema_version") != "pipeline-test-side-run-v1"
            or run.get("side") != side or run.get("repetition") != repetition
            or run.get("test_manifest_sha256") != manifest_sha256):
        raise ValueError("test_measurement_run_identity_invalid")
    if not simulation:
        raw_output = _bound_file(run["raw_output"], "test_measurement_raw_output")
        harbor_result_path = _bound_file(run["harbor_result"], "test_measurement_harbor_result")
        raw = _json(raw_output)
        harbor_result = _json(harbor_result_path)
        if (raw_output.is_relative_to(TMP_ROOT.resolve())
                or harbor_result_path.is_relative_to(TMP_ROOT.resolve())
                or run.get("task_sha256") != task_sha256
                or run.get("test_payload_sha256") != test_payload_sha256
                or run.get("repository") != repository
                or run.get("baseline_commit") != baseline_commit
                or run.get("reference_commit") != reference_commit
                or run.get("command") != ["/tests/test.sh"]
                or run.get("environment", {}).get("image_id") != image_id
                or run.get("exit_code") != 0
                or run.get("trial_id") != harbor_result.get("id")
                or run.get("job_id") != harbor_result.get("config", {}).get("job_id")
                or run.get("native_task_checksum") != harbor_result.get("task_checksum")
                or run.get("agent") != ("nop" if side == "baseline" else "oracle")
                or harbor_result.get("agent_info", {}).get("name") != run.get("agent")
                or run.get("native_reward") != (0 if side == "baseline" else 1)
                or harbor_result.get("verifier_result", {}).get("rewards", {}).get("reward")
                   != run.get("native_reward")
                or harbor_result.get("exception_info") is not None
                or harbor_result.get("started_at") != run.get("started_at")
                or harbor_result.get("finished_at") != run.get("finished_at")
                or run.get("tested_commit") != run.get(f"{side}_commit")
                or baseline_commit == reference_commit
                or raw.get("results") != run.get("results")
                or any(raw.get("summary", {}).get(key) != run.get("summary", {}).get(key)
                       for key in ("pass", "fail", "skip", "missing", "error"))):
            raise ValueError("test_measurement_run_execution_identity_invalid")
    results = run.get("results")
    observed = [{"test_id": item.get("test_id"), "class": item.get("class")}
                for item in results] if isinstance(results, list) else []
    if observed != expected_rows:
        raise ValueError("test_measurement_run_inventory_invalid")
    expected_status = {
        "baseline": {"F2P": "fail", "P2P": "pass"},
        "reference": {"F2P": "pass", "P2P": "pass"},
    }[side]
    if any(item.get("status") != expected_status[item["class"]] for item in results):
        raise ValueError("test_measurement_run_transition_invalid")
    statuses = [item["status"] for item in results]
    summary = run.get("summary")
    if (not isinstance(summary, dict) or summary.get("pass") != statuses.count("pass")
            or summary.get("fail") != statuses.count("fail")
            or any(summary.get(name, 0) != 0 for name in ("skip", "missing", "error", "flaky", "unexecuted"))):
        raise ValueError("test_measurement_run_summary_invalid")
    return (str(run.get("trial_id") or path.resolve()),
            str(run.get("native_task_checksum") or "simulation"))


def _event(source: str, target: str, status: str, code: str, evidence: dict | None = None) -> dict:
    return {
        "from": source,
        "to": target,
        "status": status,
        "code": code,
        "evidence": evidence or {},
    }


def _reject(record: dict, target: str, code: str, details: dict | None = None) -> dict:
    record["events"].append(_event(record["current_state"], target, "rejected", code, details))
    record["status"] = "rejected"
    record["rejection"] = {"code": code, **(details or {})}
    return record


def _advance(record: dict, target: str, code: str, evidence: dict) -> None:
    record["events"].append(_event(record["current_state"], target, "accepted", code, evidence))
    record["current_state"] = target


def _write(path: Path, value: dict) -> None:
    write_json(path.resolve(), value)


def _json_sha(value: dict) -> str:
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
    return hashlib.sha256(payload).hexdigest()


def _write_ledger(path: Path, value: dict) -> None:
    _validate_schema(value, "pipeline_state_ledger_v1.schema.json",
                     "state_ledger_schema_invalid")
    _write(path, value)


def _promotion_transaction_paths(record_path: Path, instance_id: str) -> tuple[Path, Path]:
    return (record_path.parent / f".{instance_id}.promotion.transaction.json",
            record_path.parent / f"{instance_id}.promotion.commit.json")


def _promotion_artifact_sha(path: Path, kind: str) -> str:
    if path.is_symlink():
        raise ValueError("promotion_transaction_artifact_symlink")
    assert_no_symlink_chain(path.parent)
    if kind == "task_tree":
        return _task_inventory(path)[0]
    if kind == "json":
        return _sha256(path)
    raise ValueError("promotion_transaction_artifact_kind_invalid")


def _promotion_entry_path(value: str, label: str) -> Path:
    raw = Path(value)
    path = (WORKSPACE_ROOT / raw).absolute()
    if (raw.is_absolute() or ".." in raw.parts
            or not path.is_relative_to(WORKSPACE_ROOT.absolute())):
        raise ValueError(f"promotion_transaction_{label}_invalid")
    return path


def _validate_promotion_entries(entries: object, destination: Path, record_path: Path,
                                frozen_path: Path) -> list[tuple[dict, Path, Path]]:
    if not isinstance(entries, list) or len(entries) != 3:
        raise ValueError("promotion_transaction_entries_invalid")
    expected_targets = {destination.absolute(), record_path.absolute(), frozen_path.absolute()}
    validated = []
    observed = set()
    for entry in entries:
        if (not isinstance(entry, dict)
                or set(entry) != {"kind", "target", "staging", "sha256"}
                or entry["kind"] not in {"task_tree", "json"}
                or not SHA256.fullmatch(str(entry["sha256"]))):
            raise ValueError("promotion_transaction_entry_invalid")
        target = _promotion_entry_path(entry["target"], "target")
        staging = _promotion_entry_path(entry["staging"], "staging")
        if target in observed or target not in expected_targets or staging in expected_targets:
            raise ValueError("promotion_transaction_target_set_invalid")
        observed.add(target)
        validated.append((entry, target, staging))
    if observed != expected_targets:
        raise ValueError("promotion_transaction_target_set_invalid")
    return validated


def _recover_promotion(destination: Path, record_path: Path, frozen_path: Path,
                       instance_id: str) -> dict | None:
    """Finish a committed publication or roll back only hash-bound pre-commit files."""
    transaction, commit = _promotion_transaction_paths(record_path, instance_id)
    if commit.exists():
        if commit.is_symlink():
            raise ValueError("promotion_commit_invalid")
        value = _json(commit)
        if value.get("schema_version") != "pipeline-promotion-commit-v1":
            raise ValueError("promotion_commit_invalid")
        entries = _validate_promotion_entries(
            value.get("entries"), destination, record_path, frozen_path)
        for entry, target, _staging in entries:
            if (not target.exists()
                    or _promotion_artifact_sha(target, entry["kind"]) != entry["sha256"]):
                raise ValueError("promotion_committed_artifact_changed")
        if transaction.exists():
            if _sha256(transaction) != value.get("transaction_sha256"):
                raise ValueError("promotion_committed_transaction_changed")
            transaction.unlink()
        return _json(record_path)
    if not transaction.exists():
        return None
    if transaction.is_symlink():
        raise ValueError("promotion_transaction_invalid")
    value = _json(transaction)
    if value.get("schema_version") != "pipeline-promotion-transaction-v1":
        raise ValueError("promotion_transaction_invalid")
    entries = _validate_promotion_entries(
        value.get("entries"), destination, record_path, frozen_path)
    for entry, target, staging in entries:
        for artifact in (target, staging):
            if not artifact.exists():
                continue
            if _promotion_artifact_sha(artifact, entry["kind"]) != entry["sha256"]:
                raise ValueError("promotion_interrupted_artifact_changed")
            if artifact.is_dir():
                shutil.rmtree(artifact)
            else:
                artifact.unlink()
    transaction.unlink()
    return None


def _cleanup_promotion_orphans(output_root: Path, record_path: Path, frozen_path: Path,
                               instance_id: str) -> None:
    patterns = (
        (output_root, re.compile(rf"\.{re.escape(instance_id)}\.[0-9a-f]{{32}}\.promotion-staging")),
        (record_path.parent, re.compile(
            rf"\.{re.escape(record_path.name)}\.[0-9a-f]{{32}}\.staging")),
        (frozen_path.parent, re.compile(
            rf"\.{re.escape(frozen_path.name)}\.[0-9a-f]{{32}}\.staging")),
    )
    for parent, pattern in patterns:
        if not parent.is_dir() or parent.is_symlink():
            continue
        for artifact in parent.iterdir():
            if not pattern.fullmatch(artifact.name):
                continue
            if artifact.is_symlink() or artifact.is_file():
                artifact.unlink()
            elif artifact.is_dir():
                shutil.rmtree(artifact)
            else:
                raise ValueError("promotion_orphan_special_file")


def _validate_promotion_commit(destination: Path, record_path: Path, frozen_path: Path,
                               instance_id: str) -> Path:
    """Read-only proof that all three formal promotion artifacts committed together."""
    transaction, commit = _promotion_transaction_paths(record_path, instance_id)
    if transaction.exists() or not commit.is_file() or commit.is_symlink():
        raise ValueError("promotion_commit_missing_or_incomplete")
    value = _json(commit)
    if (value.get("schema_version") != "pipeline-promotion-commit-v1"
            or value.get("instance_id") != instance_id
            or not SHA256.fullmatch(str(value.get("transaction_sha256", "")))):
        raise ValueError("promotion_commit_invalid")
    entries = _validate_promotion_entries(
        value.get("entries"), destination, record_path, frozen_path)
    for entry, target, _staging in entries:
        if (not target.exists()
                or _promotion_artifact_sha(target, entry["kind"]) != entry["sha256"]):
            raise ValueError("promotion_committed_artifact_changed")
    return commit


def _fsync_tree(path: Path) -> None:
    for item in sorted(path.rglob("*"), reverse=True):
        descriptor = os.open(item, os.O_RDONLY | (getattr(os, "O_DIRECTORY", 0)
                                                   if item.is_dir() else 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validate_gate(packet: dict, name: str, simulation: bool,
                   task_override: Path | None = None) -> tuple[bool, str, dict]:
    gate = packet.get(name, {})
    if gate.get("status") != "approved":
        return False, f"{name}_not_approved", {}
    source = gate.get("source")
    if source not in ({"human", "mock"} if simulation else {"human"}):
        return False, f"{name}_source_not_allowed", {"source": source}
    try:
        evidence_path = _bound_file(gate.get("evidence", {}), name)
    except ValueError as exc:
        return False, str(exc), {}
    if not simulation and evidence_path.is_relative_to(TMP_ROOT.resolve()):
        return False, f"{name}_temporary_evidence_not_allowed", {}
    contents = _json(evidence_path)
    expected_gate = "multimodal_necessity" if name == "visual_gate" else "f2p_p2p_semantic_validity"
    if simulation:
        try:
            _validate_schema(contents, "pipeline_human_gate_v1.schema.json",
                             f"{name}_evidence_schema_invalid")
        except ValueError as exc:
            return False, str(exc), {}
        if (contents.get("schema_version") != "pipeline-human-gate-v1"
                or contents.get("mode") != "simulation" or contents.get("gate") != expected_gate
                or contents.get("decision") != "approved" or contents.get("source") != source
                or not contents.get("reviewer") or contents.get("instance_id") != packet.get("instance_id")
                or contents.get("task_sha256") != packet.get("candidate_task", {}).get("sha256")):
            return False, f"{name}_evidence_semantics_invalid", {}
        return True, "approved", {
            "source": source, "path": _portable(evidence_path), "sha256": _sha256(evidence_path)}
    try:
        _validate_schema(contents, "dual_human_calibration_v2.schema.json",
                         f"{name}_evidence_schema_invalid")
        context = gate.get("context") or {}
        paths = {key: _bound_file(context.get(key, {}), f"{name}_{key}")
                 for key in ("dossier", "test_review_context", "measurement")}
        canonical_context = packet.get("review_context") or {}
        canonical_paths = {
            key: _bound_file(canonical_context.get(key, {}), f"canonical_{key}")
            for key in ("dossier", "test_review_context")
        }
        if task_override is None:
            paths["test_manifest"] = _bound_file(
                context.get("test_manifest", {}), f"{name}_test_manifest")
            canonical_paths["test_manifest"] = _bound_file(
                canonical_context.get("test_manifest", {}), "canonical_test_manifest")
        else:
            frozen_manifest = (task_override / "tests/test_manifest.json").resolve()
            if (not frozen_manifest.is_file()
                    or context.get("test_manifest") != canonical_context.get("test_manifest")
                    or context.get("test_manifest", {}).get("sha256")
                       != _sha256(frozen_manifest)):
                raise ValueError(f"{name}_test_manifest_frozen_copy_mismatch")
            paths["test_manifest"] = frozen_manifest
            canonical_paths["test_manifest"] = frozen_manifest
        canonical_measurement = _bound_file(
            packet.get("measurement", {}).get("evidence", {}), "canonical_measurement")
        if (any(paths[key] != canonical_paths[key]
                for key in ("dossier", "test_manifest", "test_review_context"))
                or paths["measurement"] != canonical_measurement):
            raise ValueError(f"{name}_canonical_context_mismatch")
        if any(paths[key].is_relative_to(TMP_ROOT.resolve())
               for key in ("dossier", "test_review_context", "measurement")):
            raise ValueError(f"{name}_temporary_context_not_allowed")
        candidate = (task_override.resolve() if task_override is not None
                     else WORKSPACE_ROOT / packet["candidate_task"]["path"])
        if paths["test_manifest"] != (candidate / "tests/test_manifest.json").resolve():
            raise ValueError(f"{name}_test_manifest_path_mismatch")
        if (contents.get("candidate_id") != packet["instance_id"]
                or contents.get("task_directory_checksum") != packet["candidate_task"]["sha256"]
                or contents.get("dossier_sha256") != _sha256(paths["dossier"])
                or contents.get("test_manifest_sha256") != _sha256(paths["test_manifest"])
                or contents.get("test_review_context_sha256") != _sha256(paths["test_review_context"])
                or contents.get("measurement_sha256") != _sha256(paths["measurement"])):
            raise ValueError(f"{name}_calibration_binding_mismatch")
        dossier = _json(paths["dossier"])
        manifest = _json(paths["test_manifest"])
        test_context = _json(paths["test_review_context"])
        expected_rows = _expected_test_rows(*_task_test_ids(candidate))
        manifest_rows = [{"test_id": item.get("test_id"), "class": item.get("class")}
                         for item in manifest.get("tests", [])]
        context_rows = [{"test_id": item.get("test_id"), "class": item.get("class")}
                        for item in test_context.get("tests", [])]
        if (dossier.get("candidate_id") != packet["instance_id"] or manifest_rows != expected_rows
                or test_context.get("candidate_id") != packet["instance_id"]
                or test_context.get("source_test_manifest_sha256") != _sha256(paths["test_manifest"])
                or context_rows != expected_rows):
            raise ValueError(f"{name}_calibration_context_invalid")
        decision = contents[expected_gate]
        if decision.get("state") != "approved":
            raise ValueError(f"{name}_human_decision_invalid")
        validate_human_gate_audit(
            decision, expected_gate, text_first=expected_gate == "multimodal_necessity")
        if expected_gate == "multimodal_necessity":
            safe_ids = {item.get("asset_id") for item in
                        dossier.get("leakage_policy", {}).get("safe_agent_assets", [])}
            selected = decision.get("evidence_asset_ids") or []
            if (decision.get("text_only_sufficiency") != "insufficient"
                    or decision.get("ocr_replaceable") != "no"
                    or not str(decision.get("non_text_visual_fact") or "").strip()
                    or not selected or not set(selected) <= safe_ids):
                raise ValueError("visual_gate_human_semantics_invalid")
        else:
            reviews = decision.get("test_reviews") or []
            review_rows = [{"test_id": item.get("test_id"), "class": item.get("class")}
                           for item in reviews]
            if (decision.get("coverage") != "complete" or review_rows != expected_rows
                    or any(item.get("decision") != "valid" or not item.get("reason")
                           for item in reviews)
                    or str(decision.get("missing_behaviors") or "").strip()):
                raise ValueError("tests_gate_human_semantics_invalid")
    except (KeyError, TypeError, ValueError) as exc:
        return False, str(exc) if isinstance(exc, ValueError) else f"{name}_evidence_semantics_invalid", {}
    evidence_context = {
        key: ({"path": _portable(path), "sha256": _sha256(path)}
              if key != "test_manifest" or task_override is None
              else dict(context["test_manifest"]))
        for key, path in paths.items()
    }
    evidence = {"source": source, "path": _portable(evidence_path), "sha256": _sha256(evidence_path),
                "context": evidence_context}
    return True, "approved", evidence


def _validate_pipeline_freeze(binding: dict) -> tuple[Path, dict]:
    path = _bound_file(binding, "pipeline_freeze")
    manifest = _json(path)
    if manifest.get("schema_version") != "pipeline-freeze-manifest-v1":
        raise ValueError("pipeline_freeze_schema_invalid")
    required = {"code": REQUIRED_FREEZE_CODE, "schemas": REQUIRED_FREEZE_SCHEMAS}
    for section in ("code", "schemas"):
        items = manifest.get(section, [])
        paths = [item.get("path") for item in items if isinstance(item, dict)]
        if len(paths) != len(items) or len(paths) != len(set(paths)):
            raise ValueError(f"pipeline_freeze_{section}_inventory_invalid")
        missing = sorted(required[section] - set(paths))
        if missing:
            raise ValueError(f"pipeline_freeze_{section}_incomplete:{','.join(missing)}")
        for item in items:
            bound = _bound_file(item, f"pipeline_freeze_{section}")
            if _portable(bound) != item.get("path"):
                raise ValueError("pipeline_freeze_path_mismatch")
    for runtime in ("harbor_runtime", "verifier_runtime"):
        snapshot = manifest.get("dependencies", {}).get(runtime, {}).get("installed_snapshot")
        _bound_file(snapshot or {}, f"pipeline_freeze_{runtime}_snapshot")
    if (not manifest.get("dependencies", {}).get("harbor_runtime", {}).get("python")
            or not manifest.get("dependencies", {}).get("harbor_runtime", {}).get("harbor")
            or not manifest.get("dependencies", {}).get("verifier_runtime", {}).get("python")
            or not manifest.get("docker", {}).get("client_version")
            or not manifest.get("docker", {}).get("compose_version")
            or not manifest.get("harbor", {}).get("version")
            or not manifest.get("harbor", {}).get("task_schema")):
        raise ValueError("pipeline_freeze_runtime_binding_incomplete")
    readiness = manifest.get("formal_promotion_ready")
    if not isinstance(readiness, dict):
        raise ValueError("pipeline_freeze_formal_readiness_contract_invalid")
    return path, manifest


def _require_formal_freeze_ready(manifest: dict) -> None:
    readiness = manifest["formal_promotion_ready"]
    dependencies = manifest["dependencies"]
    docker = manifest["docker"]
    harbor = manifest["harbor"]
    expected_runtime = {
        "harbor_runtime_snapshot_sha256":
            dependencies["harbor_runtime"]["installed_snapshot"]["sha256"],
        "verifier_runtime_snapshot_sha256":
            dependencies["verifier_runtime"]["installed_snapshot"]["sha256"],
    }
    expected_docker = {key: docker.get(key) for key in (
        "client_version", "compose_version", "daemon_version", "daemon_observed_at")}
    expected_harbor = {key: harbor.get(key) for key in ("version", "task_schema")}
    if (readiness.get("status") != "ready"
            or readiness.get("clean_hash_locked_resolution") is not True
            or dependencies.get("clean_hash_locked_resolution") is not True
            or readiness.get("blocking_limitations") != []
            or manifest.get("limitations") != []
            or readiness.get("runtime_bindings") != expected_runtime
            or readiness.get("docker_binding") != expected_docker
            or readiness.get("harbor_binding") != expected_harbor
            or not docker.get("daemon_version") or not docker.get("daemon_observed_at")):
        raise ValueError("pipeline_freeze_not_formal_ready")


def _validate_promotion_chain(ledger: dict, frozen: dict) -> None:
    expected = list(zip(STATES[:5], STATES[1:6]))
    observed = [(event.get("from"), event.get("to")) for event in ledger.get("events", [])]
    if (observed != expected or any(event.get("status") != "accepted" for event in ledger["events"])
            or ledger.get("instance_id") != frozen.get("instance_id")
            or ledger.get("mode") != frozen.get("mode")
            or ledger.get("candidate_task", {}).get("sha256") != frozen.get("task", {}).get("sha256")):
        raise ValueError("promotion_ledger_chain_invalid")


def _replay_promotion_evidence(ledger: dict, frozen: dict) -> None:
    """Re-evaluate gate, measurement, and control evidence from bound raw files."""
    _validate_promotion_chain(ledger, frozen)
    packet_path = _bound_file(frozen.get("promotion_packet", {}), "promotion_packet")
    if ledger.get("packet") != frozen.get("promotion_packet"):
        raise ValueError("promotion_packet_ledger_binding_mismatch")
    packet = _json(packet_path)
    _validate_schema(packet, "pipeline_promotion_packet_v1.schema.json",
                     "promotion_packet_schema_invalid")
    simulation = frozen.get("mode") == "simulation"
    task = (WORKSPACE_ROOT / frozen["task"]["path"]).resolve()
    task_sha, _ = _task_inventory(task)
    f2p, p2p = _task_test_ids(task)
    expected_rows = _expected_test_rows(f2p, p2p)
    if (packet.get("instance_id") != frozen.get("instance_id")
            or packet.get("candidate_task", {}).get("sha256") != task_sha):
        raise ValueError("promotion_packet_task_binding_mismatch")

    for name, event_index in (("visual_gate", 0), ("tests_gate", 2)):
        ok, code, evidence = _validate_gate(
            packet, name, simulation, task_override=task if not simulation else None)
        if not ok or code != "approved" or ledger["events"][event_index].get("evidence") != evidence:
            raise ValueError(f"{name}_replay_mismatch")

    measurement = packet.get("measurement") or {}
    measurement_path = _bound_file(measurement.get("evidence", {}), "measurement")
    measured = _json(measurement_path)
    _validate_schema(measured, "pipeline_test_measurement_v1.schema.json",
                     "test_measurement_evidence_schema_invalid")
    manifest_path = _bound_file(measured.get("test_manifest", {}),
                                "measurement_test_manifest")
    if (_sha256(manifest_path) != _sha256(task / "tests/test_manifest.json")
            or measurement.get("f2p_ids") != f2p or measurement.get("p2p_ids") != p2p
            or measured.get("mode") != ("simulation" if simulation else "real")
            or measured.get("instance_id") != frozen["instance_id"]
            or measured.get("task_sha256") != task_sha
            or measured.get("FAIL_TO_PASS") != f2p or measured.get("PASS_TO_PASS") != p2p
            or measured.get("all_transitions_match") is not True):
        raise ValueError("test_measurement_replay_semantics_invalid")
    expected_transitions = [
        {"test_id": row["test_id"], "class": row["class"],
         "expected": "fail->pass" if row["class"] == "F2P" else "pass->pass",
         "actual": "fail->pass" if row["class"] == "F2P" else "pass->pass",
         "matches": True}
        for row in expected_rows
    ]
    if measured.get("transitions") != expected_transitions:
        raise ValueError("test_measurement_transition_evidence_invalid")
    if simulation:
        repository = baseline_commit = reference_commit = image_id = "simulation"
        test_payload_sha256 = _sha256(manifest_path)
    else:
        dossier_path = _bound_file(packet["review_context"]["dossier"],
                                   "measurement_dossier")
        dossier = _json(dossier_path)
        base_image = _json(task / "environment/base_image.json")
        repository = dossier["repository"]
        baseline_commit = dossier["git"]["baseline_sha"]
        reference_commit = dossier["git"]["reference_sha"]
        image_id = base_image["image_id"]
        test_payload_sha256 = _task_inventory(task / "tests")[0]
    run_paths, run_ids, native_checksums = [], [], []
    for side in ("baseline", "reference"):
        for repetition, binding in enumerate(measured[f"{side}_runs"], 1):
            run_path = _bound_file(binding, f"measurement_{side}_run_{repetition}")
            trial_id, native_checksum = _validate_measurement_run(
                run_path, side, repetition, expected_rows, _sha256(manifest_path),
                task_sha, test_payload_sha256, repository, baseline_commit,
                reference_commit, image_id, simulation)
            run_paths.append(run_path.resolve())
            run_ids.append(trial_id)
            native_checksums.append(native_checksum)
    if len(run_paths) != len(set(run_paths)) or len(run_ids) != len(set(run_ids)):
        raise ValueError("test_measurement_run_reused")
    expected_measurement_event = {
        "path": _portable(measurement_path), "sha256": _sha256(measurement_path),
        "test_manifest": {"path": _portable(manifest_path),
                          "sha256": _sha256(manifest_path)},
        "baseline_run_count": len(measured["baseline_runs"]),
        "reference_run_count": len(measured["reference_runs"]),
        "f2p_ids": f2p, "p2p_ids": p2p,
    }
    if ledger["events"][1].get("evidence") != expected_measurement_event:
        raise ValueError("test_measurement_ledger_replay_mismatch")

    controls_path = _bound_file((packet.get("controls") or {}).get("evidence", {}),
                                "controls")
    controls = _json(controls_path)
    _validate_schema(controls, "pipeline_harbor_controls_v1.schema.json",
                     "harbor_controls_evidence_schema_invalid")
    expected_negative_harbor = None
    if not simulation:
        pass5_config_path = _bound_file(frozen.get("pass5_config", {}), "pass5_config")
        pass5_config = _json(pass5_config_path)
        _validate_schema(pass5_config, "frozen_pass5_config_v1.schema.json",
                         "pass5_config_invalid")
        _require_formal_pass5_config(pass5_config)
        expected_negative_harbor = _expected_harbor_binding(pass5_config)
    negative_controls_evidence = _validate_negative_controls(
        controls.get("negative_controls", {}), task_sha, simulation,
        expected_negative_harbor)
    if (controls.get("mode") != ("simulation" if simulation else "real")
            or controls.get("instance_id") != frozen["instance_id"]
            or controls.get("task_sha256") != task_sha
            or controls.get("harbor_task_checksum") != frozen["harbor_task_checksum"]
            or controls.get("empty_reward") != 0 or controls.get("gold_reward") != 1
            or controls.get("exception_count") != 0
            or (not simulation and set(native_checksums) != {frozen["harbor_task_checksum"]})):
        raise ValueError("harbor_controls_replay_semantics_invalid")
    expected_control_evidence = []
    candidate_path = packet["candidate_task"]["path"]
    for run, (role, agent, expected_reward) in zip(
            controls["runs"], (("baseline_nop", "nop", 0), ("oracle", "oracle", 1)),
            strict=True):
        result_path = _bound_file(run["result"], f"controls_{role}_result")
        verifier_path = _bound_file(run["verifier_result"], f"controls_{role}_verifier")
        result, details = _json(result_path), _json(verifier_path)
        reward, statuses = _validate_verifier_details(details, expected_rows)
        expected_statuses = (["fail"] * len(f2p) + ["pass"] * len(p2p)
                             if role == "baseline_nop" else ["pass"] * len(expected_rows))
        if (run.get("role") != role or run.get("agent") != agent
                or run.get("reward") != expected_reward or reward != expected_reward
                or statuses != expected_statuses
                or result.get("task_checksum") != frozen["harbor_task_checksum"]
                or result.get("exception_info") is not None
                or result.get("agent_info", {}).get("name") != agent
                or result.get("config", {}).get("task", {}).get("path") != candidate_path
                or result.get("verifier_result", {}).get("rewards", {}).get("reward")
                   != expected_reward):
            raise ValueError("harbor_controls_raw_run_semantics_invalid")
        expected_control_evidence.append({
            "role": role, "task_checksum": frozen["harbor_task_checksum"],
            "reward": reward,
            "result": {"path": _portable(result_path), "sha256": _sha256(result_path)},
            "verifier_result": {"path": _portable(verifier_path),
                                "sha256": _sha256(verifier_path)},
        })
    expected_controls_event = {
        "path": _portable(controls_path), "sha256": _sha256(controls_path),
        "empty_reward": 0, "gold_reward": 1,
        "harbor_task_checksum": frozen["harbor_task_checksum"],
        "negative_controls": negative_controls_evidence,
        "runs": expected_control_evidence,
    }
    if ledger["events"][3].get("evidence") != expected_controls_event:
        raise ValueError("harbor_controls_ledger_replay_mismatch")


def _build_image(task: Path, reference: str) -> str:
    completed = subprocess.run(
        ["docker", "build", "--tag", reference, str(task / "environment")],
        text=True, capture_output=True, check=False,
    )
    if completed.returncode:
        raise ValueError("formal_image_build_failed")
    inspected = subprocess.run(
        ["docker", "image", "inspect", reference, "--format", "{{.Id}}"],
        text=True, capture_output=True, check=False,
    )
    image_id = inspected.stdout.strip()
    if inspected.returncode or not IMAGE_ID.fullmatch(image_id):
        raise ValueError("formal_image_identity_invalid")
    return image_id


def _validate_formal_dossier(path: Path, instance_id: str) -> dict:
    """Rebuild the immutable V3 admission instead of trusting dossier prose."""
    dossier = _json(path)
    bindings = dossier.get("source_bindings") or {}
    required = ("verifier", "archive", "classification")
    if any(not bindings.get(f"{name}_path") or not bindings.get(f"{name}_sha256")
           for name in required):
        raise ValueError("formal_dossier_v3_bindings_missing")
    from report_pipeline.candidate import build
    rebuilt = build(Path(bindings["verifier_path"]), Path(bindings["archive_path"]),
                    Path(bindings["classification_path"]))
    immutable_keys = (
        "candidate_id", "status", "repository", "pr_number", "url", "title",
        "source_bindings", "git", "changed_files", "author_test_change_detected",
        "leakage_policy",
    )
    if (dossier.get("candidate_id") != instance_id
            or any(dossier.get(key) != rebuilt.get(key) for key in immutable_keys)):
        raise ValueError("formal_dossier_differs_from_v3_rebuild")
    admission = rebuilt.get("visual_admission") or {}
    v3 = admission.get("v3_classification") or {}
    if (rebuilt.get("status") != "admitted_to_test_construction"
            or admission.get("admission_route") != "v3_strict_nontext_visual"
            or v3.get("status") != "complete"
            or v3.get("strict_multimodal_admission") != "非文字视觉信息候选不可替代"
            or v3.get("human_review_required") is not False):
        raise ValueError("formal_dossier_v3_admission_invalid")
    return rebuilt


def _promote_unlocked(packet_path: Path, output_root: Path, record_path: Path,
                      *, simulation: bool) -> dict:
    """Validate every promotion gate and publish a frozen task or simulation."""
    packet_path = packet_path.resolve()
    packet = _json(packet_path)
    _validate_schema(packet, "pipeline_promotion_packet_v1.schema.json",
                     "promotion_packet_schema_invalid")
    instance_id = packet.get("instance_id", "")
    if packet.get("schema_version") != "pipeline-promotion-packet-v1":
        raise ValueError("promotion_packet_schema_invalid")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-[1-9][0-9]*", instance_id):
        raise ValueError("promotion_instance_id_invalid")
    output_root, record_path = output_root.resolve(), record_path.resolve()
    required_root = TMP_ROOT.resolve() if simulation else CASES_ROOT.resolve()
    if (not simulation and output_root != required_root) or (simulation and output_root != required_root and not output_root.is_relative_to(required_root)):
        raise ValueError("simulation_output_must_be_tmp" if simulation else "formal_output_must_be_cases")
    required_record_root = RUNS_ROOT.resolve() if simulation else (REPORT_ROOT / "evidence").resolve()
    if record_path != required_record_root and not record_path.is_relative_to(required_record_root):
        raise ValueError("promotion_record_path_outside_allowed_root")
    expected_record_name = f"{instance_id}.promotion_ledger.json"
    if not simulation and (record_path.parent != required_record_root or record_path.name != expected_record_name):
        raise ValueError("formal_promotion_record_name_invalid")
    frozen_path = record_path.parent / f"{instance_id}.frozen.json"
    destination = output_root / instance_id
    recovered = _recover_promotion(destination, record_path, frozen_path, instance_id)
    if recovered is not None:
        return recovered
    _cleanup_promotion_orphans(output_root, record_path, frozen_path, instance_id)
    if record_path.exists() or frozen_path.exists() or record_path == frozen_path:
        raise ValueError("promotion_evidence_destination_exists")
    record = {
        "schema_version": "pipeline-state-ledger-v1",
        "mode": "simulation" if simulation else "real",
        "instance_id": instance_id,
        "status": "running",
        "current_state": "candidate",
        "packet": {"path": _portable(packet_path), "sha256": _sha256(packet_path)},
        "pipeline_freeze": dict(packet.get("pipeline_freeze") or {}),
        "candidate_task": dict(packet.get("candidate_task") or {}),
        "events": [],
    }
    try:
        pipeline_freeze_path, pipeline_freeze = _validate_pipeline_freeze(
            packet.get("pipeline_freeze", {}))
        if (not simulation and pipeline_freeze_path
                != (REPORT_ROOT / "reproducibility/09_pipeline_freeze_manifest.json").resolve()):
            raise ValueError("formal_pipeline_freeze_path_invalid")
        record["pipeline_freeze"] = {
            "path": _portable(pipeline_freeze_path),
            "sha256": _sha256(pipeline_freeze_path),
        }
        task_binding = packet.get("candidate_task", {})
        candidate_value = task_binding.get("path", "")
        candidate = WORKSPACE_ROOT / candidate_value
        if (Path(candidate_value).is_absolute() or ".." in Path(candidate_value).parts
                or not candidate.is_dir()):
            raise ValueError("candidate_task_path_invalid")
        _validate_task_tree(candidate)
        candidate_checksum, files = _task_inventory(candidate)
        record["candidate_task"] = {
            "path": _portable(candidate), "sha256": candidate_checksum}
        if not simulation:
            dossier_path = _bound_file(
                (packet.get("review_context") or {}).get("dossier", {}),
                "formal_dossier")
            if dossier_path.is_relative_to(TMP_ROOT.resolve()):
                raise ValueError("formal_dossier_temporary_evidence_not_allowed")
            _validate_formal_dossier(dossier_path, instance_id)
    except (KeyError, TypeError, ValueError, OSError) as exc:
        code = str(exc) if isinstance(exc, ValueError) else "promotion_preflight_invalid"
        _reject(record, "visual_approved", code, {"stage": "preflight"})
        _write_ledger(record_path, record)
        return record
    if task_binding.get("sha256") != candidate_checksum:
        _reject(record, "visual_approved", "candidate_task_checksum_mismatch")
        _write_ledger(record_path, record)
        return record

    ok, code, evidence = _validate_gate(packet, "visual_gate", simulation)
    if not ok:
        _reject(record, "visual_approved", code, evidence); _write_ledger(record_path, record); return record
    _advance(record, "visual_approved", "visual_gate_accepted", evidence)

    measurement = packet.get("measurement", {})
    try:
        measurement_path = _bound_file(measurement.get("evidence", {}), "measurement")
    except ValueError as exc:
        _reject(record, "tests_measured", str(exc)); _write_ledger(record_path, record); return record
    f2p, p2p = measurement.get("f2p_ids"), measurement.get("p2p_ids")
    if (measurement.get("status") != "measured" or measurement.get("all_transitions_match") is not True
            or not isinstance(f2p, list) or not f2p or not isinstance(p2p, list) or not p2p
            or len(f2p + p2p) != len(set(f2p + p2p))):
        _reject(record, "tests_measured", "test_measurement_invalid"); _write_ledger(record_path, record); return record
    measurement_contents = _json(measurement_path)
    try:
        _validate_schema(measurement_contents, "pipeline_test_measurement_v1.schema.json",
                         "test_measurement_evidence_schema_invalid")
    except ValueError as exc:
        _reject(record, "tests_measured", str(exc)); _write_ledger(record_path, record); return record
    task_f2p, task_p2p = _task_test_ids(candidate)
    expected_rows = _expected_test_rows(task_f2p, task_p2p)
    expected_mode = "simulation" if simulation else "real"
    if (not simulation and measurement_path.is_relative_to(TMP_ROOT.resolve())) or (
            measurement_contents.get("schema_version") != "pipeline-test-measurement-v1"
            or measurement_contents.get("mode") != expected_mode
            or measurement_contents.get("instance_id") != instance_id
            or measurement_contents.get("task_sha256") != candidate_checksum
            or measurement_contents.get("all_transitions_match") is not True
            or measurement_contents.get("FAIL_TO_PASS") != f2p
            or measurement_contents.get("PASS_TO_PASS") != p2p
            or task_f2p != f2p or task_p2p != p2p):
        _reject(record, "tests_measured", "test_measurement_evidence_semantics_invalid"); _write_ledger(record_path, record); return record
    try:
        manifest_path = _bound_file(measurement_contents["test_manifest"], "measurement_test_manifest")
        if manifest_path != (candidate / "tests/test_manifest.json").resolve():
            raise ValueError("measurement_test_manifest_path_mismatch")
        run_paths: list[Path] = []
        run_ids: list[str] = []
        measurement_task_checksums: list[str] = []
        if not simulation:
            dossier_path = _bound_file(packet["review_context"]["dossier"], "measurement_dossier")
            dossier = _json(dossier_path)
            base_image_path = candidate / "environment/base_image.json"
            base_image = _json(base_image_path)
            repository = dossier["repository"]
            baseline_commit = dossier["git"]["baseline_sha"]
            reference_commit = dossier["git"]["reference_sha"]
            image_id = base_image["image_id"]
            test_payload_sha256 = _task_inventory(candidate / "tests")[0]
        else:
            repository = baseline_commit = reference_commit = image_id = "simulation"
            test_payload_sha256 = _sha256(manifest_path)
        for side in ("baseline", "reference"):
            bindings = measurement_contents[f"{side}_runs"]
            for repetition, binding in enumerate(bindings, 1):
                run_path = _bound_file(binding, f"measurement_{side}_run_{repetition}")
                if not simulation and run_path.is_relative_to(TMP_ROOT.resolve()):
                    raise ValueError("test_measurement_run_temporary_evidence_not_allowed")
                trial_id, native_checksum = _validate_measurement_run(
                    run_path, side, repetition, expected_rows,
                    _sha256(manifest_path), candidate_checksum,
                    test_payload_sha256, repository, baseline_commit,
                    reference_commit, image_id, simulation)
                run_ids.append(trial_id)
                measurement_task_checksums.append(native_checksum)
                run_paths.append(run_path)
        if len({path.resolve() for path in run_paths}) != len(run_paths):
            raise ValueError("test_measurement_run_reused")
        if len(set(run_ids)) != len(run_ids):
            raise ValueError("test_measurement_native_trial_reused")
        expected_transitions = [
            {"test_id": row["test_id"], "class": row["class"],
             "expected": "fail->pass" if row["class"] == "F2P" else "pass->pass",
             "actual": "fail->pass" if row["class"] == "F2P" else "pass->pass",
             "matches": True}
            for row in expected_rows
        ]
        if measurement_contents["transitions"] != expected_transitions:
            raise ValueError("test_measurement_transition_evidence_invalid")
    except (KeyError, TypeError, ValueError) as exc:
        code = str(exc) if isinstance(exc, ValueError) else "test_measurement_raw_evidence_invalid"
        _reject(record, "tests_measured", code); _write_ledger(record_path, record); return record
    _advance(record, "tests_measured", "test_measurement_accepted", {
        "path": _portable(measurement_path), "sha256": _sha256(measurement_path),
        "test_manifest": {"path": _portable(manifest_path), "sha256": _sha256(manifest_path)},
        "baseline_run_count": len(measurement_contents["baseline_runs"]),
        "reference_run_count": len(measurement_contents["reference_runs"]),
        "f2p_ids": f2p, "p2p_ids": p2p,
    })

    ok, code, evidence = _validate_gate(packet, "tests_gate", simulation)
    if not ok:
        _reject(record, "tests_approved", code, evidence); _write_ledger(record_path, record); return record
    _advance(record, "tests_approved", "tests_gate_accepted", evidence)

    controls = packet.get("controls", {})
    try:
        controls_path = _bound_file(controls.get("evidence", {}), "controls")
    except ValueError as exc:
        _reject(record, "harbor_controls_passed", str(exc)); _write_ledger(record_path, record); return record
    if (controls.get("status") != "passed" or controls.get("task_sha256") != candidate_checksum
            or controls.get("empty_reward") != 0 or controls.get("gold_reward") != 1
            or controls.get("exception_count") != 0):
        _reject(record, "harbor_controls_passed", "harbor_controls_invalid")
        _write_ledger(record_path, record); return record
    controls_contents = _json(controls_path)
    try:
        _validate_schema(controls_contents, "pipeline_harbor_controls_v1.schema.json",
                         "harbor_controls_evidence_schema_invalid")
        expected_negative_harbor = None
        if not simulation:
            preview_config_path = _bound_file(packet.get("pass5_config", {}), "pass5_config")
            preview_config = _json(preview_config_path)
            _validate_schema(preview_config, "frozen_pass5_config_v1.schema.json",
                             "pass5_config_invalid")
            _require_formal_pass5_config(preview_config)
            expected_negative_harbor = _expected_harbor_binding(preview_config)
        negative_controls_evidence = _validate_negative_controls(
            controls_contents.get("negative_controls", {}), candidate_checksum,
            simulation, expected_negative_harbor)
    except ValueError as exc:
        _reject(record, "harbor_controls_passed", str(exc)); _write_ledger(record_path, record); return record
    if (not simulation and controls_path.is_relative_to(TMP_ROOT.resolve())) or (
            controls_contents.get("schema_version") != "pipeline-harbor-controls-v1"
            or controls_contents.get("mode") != expected_mode
            or controls_contents.get("instance_id") != instance_id
            or controls_contents.get("task_sha256") != candidate_checksum
            or not SHA256.fullmatch(str(controls_contents.get("harbor_task_checksum", "")))
            or controls_contents.get("empty_reward") != 0
            or controls_contents.get("gold_reward") != 1
            or controls_contents.get("exception_count") != 0):
        _reject(record, "harbor_controls_passed", "harbor_controls_evidence_semantics_invalid")
        _write_ledger(record_path, record); return record
    if (not simulation
            and set(measurement_task_checksums) != {controls_contents["harbor_task_checksum"]}):
        _reject(record, "harbor_controls_passed", "measurement_controls_task_checksum_mismatch")
        _write_ledger(record_path, record); return record
    control_evidence = []
    try:
        expected_controls = (("baseline_nop", "nop", 0), ("oracle", "oracle", 1))
        for run, (role, agent, expected_reward) in zip(controls_contents["runs"], expected_controls, strict=True):
            result_path = _bound_file(run["result"], f"controls_{role}_result")
            verifier_path = _bound_file(run["verifier_result"], f"controls_{role}_verifier")
            if (not simulation
                    and verifier_path != (result_path.parent / "verifier/test_results.json").resolve()):
                raise ValueError("harbor_controls_result_verifier_pair_mismatch")
            if not simulation and (result_path.is_relative_to(TMP_ROOT.resolve())
                                   or verifier_path.is_relative_to(TMP_ROOT.resolve())):
                raise ValueError("harbor_controls_temporary_run_evidence_not_allowed")
            result = _json(result_path)
            details = _json(verifier_path)
            reward, statuses = _validate_verifier_details(details, expected_rows)
            expected_statuses = (["fail"] * len(f2p) + ["pass"] * len(p2p)
                                 if role == "baseline_nop" else ["pass"] * len(expected_rows))
            reported_reward = result.get("verifier_result", {}).get("rewards", {}).get("reward")
            if (run.get("role") != role or run.get("agent") != agent
                    or run.get("task_checksum") != controls_contents["harbor_task_checksum"]
                    or run.get("reward") != expected_reward or reward != expected_reward
                    or statuses != expected_statuses or result.get("task_checksum") != run["task_checksum"]
                    or result.get("exception_info") is not None
                    or result.get("agent_info", {}).get("name") != agent
                    or result.get("config", {}).get("task", {}).get("path") != _portable(candidate)
                    or reported_reward != expected_reward):
                raise ValueError("harbor_controls_raw_run_semantics_invalid")
            control_evidence.append({
                "role": role, "task_checksum": run["task_checksum"], "reward": reward,
                "result": {"path": _portable(result_path), "sha256": _sha256(result_path)},
                "verifier_result": {"path": _portable(verifier_path), "sha256": _sha256(verifier_path)},
            })
    except (KeyError, TypeError, ValueError) as exc:
        code = str(exc) if isinstance(exc, ValueError) else "harbor_controls_raw_evidence_invalid"
        _reject(record, "harbor_controls_passed", code); _write_ledger(record_path, record); return record
    _advance(record, "harbor_controls_passed", "harbor_controls_accepted", {
        "path": _portable(controls_path), "sha256": _sha256(controls_path),
        "empty_reward": 0, "gold_reward": 1,
        "harbor_task_checksum": controls_contents["harbor_task_checksum"],
        "negative_controls": negative_controls_evidence,
        "runs": control_evidence,
    })

    if not simulation:
        try:
            _require_formal_freeze_ready(pipeline_freeze)
        except ValueError as exc:
            _reject(record, "frozen", str(exc))
            _write_ledger(record_path, record)
            return record

    try:
        pass5_config_path = _bound_file(packet.get("pass5_config", {}), "pass5_config")
    except ValueError as exc:
        _reject(record, "frozen", str(exc)); _write_ledger(record_path, record); return record
    pass5_config = _json(pass5_config_path)
    if (pass5_config.get("schema_version") != "frozen-pass5-config-v1"
            or pass5_config.get("valid_trials") != 5
            or not pass5_config.get("model_id") or not pass5_config.get("agent")
            or not pass5_config.get("agent_version")):
        _reject(record, "frozen", "pass5_config_invalid"); _write_ledger(record_path, record); return record
    if not simulation and pass5_config_path.is_relative_to(TMP_ROOT.resolve()):
        _reject(record, "frozen", "pass5_config_temporary_evidence_not_allowed"); _write_ledger(record_path, record); return record
    if pass5_config.get("expected_test_ids") != f2p + p2p:
        _reject(record, "frozen", "pass5_test_inventory_mismatch"); _write_ledger(record_path, record); return record
    try:
        _validate_schema(pass5_config, "frozen_pass5_config_v1.schema.json",
                         "pass5_config_invalid")
        if not simulation:
            _require_formal_pass5_config(pass5_config)
    except ValueError as exc:
        _reject(record, "frozen", str(exc)); _write_ledger(record_path, record); return record

    token = secrets.token_hex(16)
    staging = output_root / f".{instance_id}.{token}.promotion-staging"
    if destination.exists():
        _reject(record, "frozen", "promotion_destination_exists", {"path": _portable(destination)})
        _write_ledger(record_path, record); return record
    if staging.exists():
        _reject(record, "frozen", "promotion_staging_exists", {"path": _portable(staging)})
        _write_ledger(record_path, record); return record
    try:
        shutil.copytree(candidate, staging)
    except (OSError, shutil.Error):
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        _reject(record, "frozen", "promotion_copy_failed")
        _write_ledger(record_path, record)
        return record
    destination_checksum, destination_files = _task_inventory(staging)
    if destination_checksum != candidate_checksum or destination_files != files:
        shutil.rmtree(staging)
        _reject(record, "frozen", "promoted_task_copy_mismatch"); _write_ledger(record_path, record); return record
    image = packet.get("image", {})
    if simulation:
        if image.get("mode") != "simulation":
            shutil.rmtree(staging)
            _reject(record, "frozen", "simulated_image_build_config_invalid"); _write_ledger(record_path, record); return record
        image_id = image.get("simulated_image_id")
        if not IMAGE_ID.fullmatch(str(image_id)):
            shutil.rmtree(staging)
            _reject(record, "frozen", "simulated_image_id_invalid"); _write_ledger(record_path, record); return record
        image_reference = image.get("reference", "simulation-only")
    else:
        if image.get("mode") != "docker_build" or not image.get("reference"):
            shutil.rmtree(staging)
            _reject(record, "frozen", "formal_image_build_config_invalid"); _write_ledger(record_path, record); return record
        image_reference = image["reference"]
        try:
            image_id = _build_image(staging, image_reference)
        except ValueError as exc:
            shutil.rmtree(staging)
            _reject(record, "frozen", str(exc)); _write_ledger(record_path, record); return record

    _advance(record, "frozen", "task_frozen", {
        "task_path": _portable(destination), "task_sha256": destination_checksum,
        "image_id": image_id, "frozen_manifest": _portable(frozen_path),
    })
    record["status"] = "completed"
    record["frozen_manifest"] = _portable(frozen_path)
    _validate_schema(record, "pipeline_state_ledger_v1.schema.json",
                     "state_ledger_schema_invalid")
    frozen = {
        "schema_version": "frozen-harbor-task-v1",
        "state": "frozen",
        "mode": record["mode"],
        "instance_id": instance_id,
        "task": {"path": _portable(destination), "sha256": destination_checksum,
                 "files": destination_files},
        "harbor_task_checksum": controls_contents["harbor_task_checksum"],
        "image": {"reference": image_reference, "image_id": image_id},
        "promotion_packet": record["packet"],
        "promotion_ledger": {"path": _portable(record_path), "sha256": _json_sha(record)},
        "pipeline_freeze": record["pipeline_freeze"],
        "pass5_config": {"path": _portable(pass5_config_path),
                         "sha256": _sha256(pass5_config_path),
                         "model_id": pass5_config["model_id"],
                         "agent": pass5_config["agent"],
                         "agent_version": pass5_config["agent_version"]},
    }
    _validate_schema(frozen, "frozen_harbor_task_v1.schema.json",
                     "frozen_manifest_schema_invalid")
    if not simulation:
        _validate_formal_job_config(pass5_config, frozen)
    ledger_staging = record_path.parent / f".{record_path.name}.{token}.staging"
    frozen_staging = frozen_path.parent / f".{frozen_path.name}.{token}.staging"
    _write_ledger(ledger_staging, record)
    _write(frozen_staging, frozen)
    transaction_path, commit_path = _promotion_transaction_paths(record_path, instance_id)
    entries = [
        {"kind": "task_tree", "target": _portable(destination),
         "staging": _portable(staging), "sha256": destination_checksum},
        {"kind": "json", "target": _portable(record_path),
         "staging": _portable(ledger_staging), "sha256": _sha256(ledger_staging)},
        {"kind": "json", "target": _portable(frozen_path),
         "staging": _portable(frozen_staging), "sha256": _sha256(frozen_staging)},
    ]
    _write(transaction_path, {
        "schema_version": "pipeline-promotion-transaction-v1",
        "instance_id": instance_id,
        "entries": entries,
    })
    staging.rename(destination)
    _fsync_tree(destination)
    destination_parent = os.open(destination.parent,
                                 os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(destination_parent)
    finally:
        os.close(destination_parent)
    ledger_staging.replace(record_path)
    frozen_staging.replace(frozen_path)
    _write(commit_path, {
        "schema_version": "pipeline-promotion-commit-v1",
        "instance_id": instance_id,
        "transaction_sha256": _sha256(transaction_path),
        "entries": entries,
    })
    transaction_path.unlink()
    return record


def promote(packet_path: Path, output_root: Path, record_path: Path, *, simulation: bool) -> dict:
    """Serialize promotion per instance; the kernel releases the lock on crashes."""
    packet = _json(packet_path.resolve())
    instance_id = packet.get("instance_id", "invalid")
    if not isinstance(instance_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-[1-9][0-9]*", instance_id):
        raise ValueError("promotion_instance_id_invalid")
    record_path = record_path.absolute()
    allowed = (RUNS_ROOT.absolute() if simulation
               else (REPORT_ROOT / "evidence").absolute())
    if (record_path != allowed and not record_path.is_relative_to(allowed)):
        raise ValueError("promotion_record_path_outside_allowed_root")
    if (not simulation and (record_path.parent != allowed
                            or record_path.name != f"{instance_id}.promotion_ledger.json")):
        raise ValueError("formal_promotion_record_name_invalid")
    assert_no_symlink_chain(record_path.parent)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    parent_descriptor = os.open(
        record_path.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        descriptor = os.open(
            f".{instance_id}.promotion.lock",
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600,
            dir_fd=parent_descriptor,
        )
    finally:
        os.close(parent_descriptor)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("promotion_in_progress") from None
        return _promote_unlocked(packet_path, output_root, record_path,
                                 simulation=simulation)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _validate_run_authorization(path: Path | None, frozen_path: Path, frozen: dict,
                                config: dict, output: Path) -> dict:
    if path is None:
        raise ValueError("real_run_authorization_required")
    authorization = _json(path)
    if path.resolve().is_relative_to(TMP_ROOT.resolve()):
        raise ValueError("run_authorization_temporary_evidence_not_allowed")
    _validate_schema(authorization, "pass5_run_authorization_v1.schema.json",
                     "run_authorization_schema_invalid")
    expected = {
        "schema_version": "pass5-run-authorization-v1",
        "authorized": True,
        "output_path": _portable(output),
        "pipeline_freeze_sha256": frozen["pipeline_freeze"]["sha256"],
        "frozen_manifest_sha256": _sha256(frozen_path),
        "task_sha256": frozen["task"]["sha256"],
        "harbor_task_checksum": frozen["harbor_task_checksum"],
        "image_id": frozen["image"]["image_id"],
        "model_id": config["model_id"],
        "agent": config["agent"],
        "agent_version": config["agent_version"],
        "harbor_job_config_sha256": config["harbor_job_config"]["sha256"],
        "valid_trials": 5,
    }
    for field, value in expected.items():
        if authorization.get(field) != value:
            raise ValueError(f"run_authorization_{field}_mismatch")
    if not isinstance(authorization.get("maximum_harbor_attempts"), int) or authorization["maximum_harbor_attempts"] < 5:
        raise ValueError("run_authorization_attempt_budget_invalid")
    return {"path": _portable(path), "sha256": _sha256(path),
            "run_id": authorization["run_id"], "nonce": authorization["nonce"],
            "output_path": authorization["output_path"],
            "harbor_job_config_sha256": authorization["harbor_job_config_sha256"],
            "maximum_harbor_attempts": authorization["maximum_harbor_attempts"]}


def _consume_run_authorization(output: Path, frozen_path: Path, authorization: dict) -> dict:
    receipt_path = output / "authorization_receipt.json"
    if receipt_path.exists():
        raise ValueError("run_authorization_already_consumed")
    registry = REPORT_ROOT / "evidence/pass5_authorization_receipts"
    registry.mkdir(parents=True, exist_ok=True)
    registry_path = registry / f"{authorization['nonce']}.json"
    receipt = {
        "schema_version": "pass5-authorization-receipt-v1",
        "status": "consumed",
        "run_id": authorization["run_id"],
        "nonce": authorization["nonce"],
        "output_path": authorization["output_path"],
        "authorization": {"path": authorization["path"], "sha256": authorization["sha256"]},
        "frozen_manifest": {"path": _portable(frozen_path), "sha256": _sha256(frozen_path)},
    }
    _validate_schema(receipt, "pass5_authorization_receipt_v1.schema.json",
                     "run_authorization_receipt_invalid")
    # The output directory is recoverable, so single-use authorization also
    # needs an append-only receipt outside it. O_EXCL makes concurrent replay
    # attempts deterministic: exactly one process can consume the nonce.
    try:
        descriptor = os.open(registry_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ValueError("run_authorization_nonce_already_consumed") from exc
    with os.fdopen(descriptor, "w") as stream:
        stream.write(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    _write(receipt_path, receipt)
    return {"path": _portable(receipt_path), "sha256": _sha256(receipt_path)}


def _runtime_projection(value: dict, *, job_level: bool) -> dict:
    """Project persisted Harbor config onto the security-relevant interface."""
    if job_level:
        agents = value.get("agents")
        tasks = value.get("tasks")
        if (not isinstance(agents, list) or len(agents) != 1
                or not isinstance(agents[0], dict)
                or not isinstance(tasks, list) or len(tasks) != 1
                or not isinstance(tasks[0], dict)):
            raise ValueError("resolved_job_config_single_identity_required")
        agent, task = agents[0], tasks[0]
    else:
        agent, task = value.get("agent"), value.get("task")
        if not isinstance(agent, dict) or not isinstance(task, dict):
            raise ValueError("result_resolved_config_identity_missing")
    environment = value.get("environment")
    if not isinstance(environment, dict):
        raise ValueError("resolved_environment_config_missing")

    raw_env = agent.get("env")
    if not isinstance(raw_env, dict):
        raise ValueError("resolved_agent_env_missing")
    # Harbor redacts keys containing TOKEN/API_KEY in persisted artifacts. The
    # presence and name of every variable remain bound; only the secret-shaped
    # value is normalized before comparison.
    normalized_env = {
        key: ("<redacted-sensitive-value>" if re.search(
            r"(?i)(?:token|api[_-]?key|authorization|credential|secret|password)", key)
              else item)
        for key, item in raw_env.items()
    }
    extra_compose = environment.get("extra_docker_compose", [])
    if not isinstance(extra_compose, list):
        raise ValueError("resolved_compose_config_invalid")
    projection = {
        "task_path": task.get("path"),
        "agent": {
            "name": agent.get("name"),
            "model_name": agent.get("model_name"),
            "extra_allowed_hosts": sorted(agent.get("extra_allowed_hosts") or []),
            "kwargs": agent.get("kwargs"),
            "env": normalized_env,
            "mcp_servers": agent.get("mcp_servers"),
            "skills": agent.get("skills"),
        },
        "environment": {
            "type": environment.get("type"),
            "extra_allowed_hosts": sorted(environment.get("extra_allowed_hosts") or []),
            "extra_docker_compose": extra_compose,
        },
    }
    if not job_level:
        projection["forbidden_runtime_inputs"] = {
            "source_trial": value.get("source_trial"),
            "user_agent": value.get("user_agent"),
            "extra_instruction_paths": value.get("extra_instruction_paths") or [],
            "extra_instructions": value.get("extra_instructions") or [],
            "agent_load_trajectory": agent.get("load_trajectory"),
            "agent_resume_trajectory": agent.get("resume_trajectory", False),
            "environment_mounts": environment.get("mounts"),
        }
    return projection


def _validate_trial_runtime_binding(result_path: Path, result: dict, frozen: dict,
                                    config: dict) -> dict:
    """Bind source, job-resolved, and result-resolved Harbor configurations."""
    if config.get("agent") not in {OFFICIAL_K3_AGENT, OFFICIAL_CODEX_AGENT}:
        return {}
    source_path = _bound_file(config.get("harbor_job_config", {}), "harbor_job_config")
    source = _json(source_path)
    job_config_path = result_path.parent.parent / "config.json"
    if (job_config_path.is_symlink() or not job_config_path.is_file()
            or result_path.parent.parent.is_symlink()):
        raise ValueError("resolved_job_config_missing")
    resolved_job = _json(job_config_path)
    result_config = result.get("config")
    if not isinstance(result_config, dict):
        raise ValueError("result_resolved_config_missing")

    expected = _runtime_projection(source, job_level=True)
    observed_job = _runtime_projection(resolved_job, job_level=True)
    observed_result = _runtime_projection(result_config, job_level=False)
    allowed_resolved_job_keys = set(source) | {"job_name", "jobs_dir"}
    source_agent_keys = set(source["agents"][0])
    source_environment_keys = set(source["environment"])
    if (set(resolved_job) - allowed_resolved_job_keys
            or set(resolved_job["agents"][0]) != source_agent_keys
            or set(resolved_job["environment"]) != source_environment_keys
            or set(resolved_job["tasks"][0]) != set(source["tasks"][0])):
        raise ValueError("resolved_job_config_unapproved_extension")
    expected["agent"]["extra_allowed_hosts"] = sorted(
        config["network_policy"]["agent_hosts"])
    expected["environment"]["extra_allowed_hosts"] = sorted(
        config["network_policy"]["environment_hosts"])
    if expected["environment"]["extra_allowed_hosts"] != []:
        raise ValueError("trial_environment_network_not_denied")
    if observed_job != expected:
        raise ValueError("resolved_job_config_frozen_mismatch")
    comparable_result = {key: observed_result[key] for key in (
        "task_path", "agent", "environment")}
    if comparable_result != expected:
        raise ValueError("result_resolved_config_frozen_mismatch")
    forbidden = observed_result["forbidden_runtime_inputs"]
    if (forbidden["source_trial"] is not None
            or forbidden["user_agent"] is not None
            or forbidden["extra_instruction_paths"]
            or forbidden["extra_instructions"]
            or forbidden["agent_load_trajectory"] is not None
            or forbidden["agent_resume_trajectory"] is not False
            or forbidden["environment_mounts"] is not None):
        raise ValueError("result_resolved_config_unsafe_capability")
    return {
        "frozen_job_config": {"path": _portable(source_path),
                              "sha256": _sha256(source_path)},
        "resolved_job_config": {"path": _portable(job_config_path),
                                "sha256": _sha256(job_config_path)},
        "resolved_runtime_sha256": hashlib.sha256(json.dumps(
            observed_result, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
    }


def _classify_harbor_trial(result_path: Path, expected_ids: list[str], frozen: dict,
                           config: dict) -> dict:
    if result_path.is_symlink() or result_path.parent.is_symlink():
        return {"classification": "infrastructure_invalid", "reason": "harbor_artifact_symlink",
                "result": _portable_lexical(result_path)}
    result = _json(result_path)
    native_id = result.get("id")
    trial_name = result.get("trial_name")
    job_id = result.get("config", {}).get("job_id")
    started_at, finished_at = result.get("started_at"), result.get("finished_at")
    try:
        started = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        finished = datetime.fromisoformat(str(finished_at).replace("Z", "+00:00"))
    except ValueError:
        started = finished = None
    if (not isinstance(native_id, str) or not native_id
            or not isinstance(job_id, str) or not job_id
            or trial_name != result_path.parent.name
            or result.get("config", {}).get("trial_name") != trial_name
            or result.get("config", {}).get("source_trial") is not None
            or started is None or finished is None or finished <= started):
        return {"classification": "infrastructure_invalid", "reason": "trial_independence_evidence_invalid",
                "result": _portable(result_path)}
    try:
        runtime_bindings = _validate_trial_runtime_binding(
            result_path, result, frozen, config)
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {"classification": "infrastructure_invalid", "reason": str(exc),
                "result": _portable(result_path)}
    if result.get("exception_info") is not None:
        return {"classification": "infrastructure_invalid", "reason": "harbor_exception",
                "result": _portable(result_path), **runtime_bindings}
    task_path = result.get("config", {}).get("task", {}).get("path")
    agent_info = result.get("agent_info", {})
    model_info = agent_info.get("model_info")
    if (task_path != frozen["task"]["path"]
            or result.get("task_checksum") != frozen["harbor_task_checksum"]
            or agent_info.get("name") != config["agent"]
            or str(agent_info.get("version")) != str(config["agent_version"])
            or not isinstance(model_info, dict)
            or model_info.get("name") != config["model_id"]):
        return {"classification": "infrastructure_invalid", "reason": "trial_identity_binding_mismatch",
                "result": _portable(result_path)}
    verifier_path = result_path.parent / "verifier/test_results.json"
    if (verifier_path.is_symlink() or verifier_path.parent.is_symlink()
            or not verifier_path.is_file()):
        return {"classification": "infrastructure_invalid", "reason": "verifier_not_reached",
                "result": _portable(result_path)}
    verifier = _json(verifier_path)
    task = WORKSPACE_ROOT / frozen["task"]["path"]
    f2p, p2p = _task_test_ids(task)
    if expected_ids != f2p + p2p:
        return {"classification": "infrastructure_invalid", "reason": "invalid_verifier_contract",
                "result": _portable(result_path), "verifier": _portable(verifier_path)}
    try:
        reward, statuses = _validate_verifier_details(verifier, _expected_test_rows(f2p, p2p))
    except ValueError as exc:
        return {"classification": "infrastructure_invalid", "reason": str(exc),
                "result": _portable(result_path), "verifier": _portable(verifier_path)}
    reported_reward = result.get("verifier_result", {}).get("rewards", {}).get("reward")
    if reported_reward != reward:
        return {"classification": "infrastructure_invalid", "reason": "harbor_verifier_reward_mismatch",
                "result": _portable(result_path), "verifier": _portable(verifier_path)}
    if any(status in {"skip", "missing", "error"} for status in statuses):
        return {"classification": "infrastructure_invalid", "reason": "verifier_incomplete_execution",
                "result": _portable(result_path), "verifier": _portable(verifier_path)}
    trajectory_files = [path for path in sorted((result_path.parent / "agent").rglob("*"))
                        if path.is_file()]
    if not trajectory_files:
        return {"classification": "infrastructure_invalid", "reason": "trajectory_not_captured",
                "result": _portable(result_path), "verifier": _portable(verifier_path)}
    if (result_path.parent / "agent").is_symlink() or any(path.is_symlink() for path in trajectory_files):
        return {"classification": "infrastructure_invalid", "reason": "trajectory_symlink_detected",
                "result": _portable(result_path), "verifier": _portable(verifier_path)}
    total_trajectory_size = 0
    for path in trajectory_files:
        size = path.stat().st_size
        total_trajectory_size += size
        if size > TRAJECTORY_FILE_LIMIT or total_trajectory_size > TRAJECTORY_TOTAL_LIMIT:
            return {"classification": "infrastructure_invalid",
                    "reason": "trajectory_size_budget_exceeded"}
        if _sensitive_filename(path) or _file_contains_secret(path):
            return {"classification": "infrastructure_invalid",
                    "reason": "trajectory_secret_detected"}
    if config.get("agent") in {OFFICIAL_K3_AGENT, OFFICIAL_CODEX_AGENT}:
        trace_audit = audit_trial_trace(
            result_path.parent, agent=config["agent"],
            allowed_network_hosts=config["network_policy"]["agent_hosts"])
        if not trace_audit.get("valid"):
            return {
                "classification": trace_audit["classification"],
                "reason": trace_audit["reason"],
                "result": {"path": _portable(result_path), "sha256": _sha256(result_path)},
                "verifier": {"path": _portable(verifier_path),
                             "sha256": _sha256(verifier_path)},
                "answer_leakage_hits": trace_audit.get("answer_leakage_hits", []),
                **runtime_bindings,
            }
    trajectory_digest = hashlib.sha256(json.dumps(
        sorted(_sha256(path) for path in trajectory_files),
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"classification": "valid", "reward": reward,
            "trial_id": native_id, "trial_name": trial_name, "job_id": job_id,
            "started_at": started_at, "finished_at": finished_at,
            "trajectory_digest": trajectory_digest,
            "result": {"path": _portable(result_path), "sha256": _sha256(result_path)},
            "verifier": {"path": _portable(verifier_path), "sha256": _sha256(verifier_path)},
            **runtime_bindings,
            "trajectory_index": [{"path": _portable(path), "sha256": _sha256(path)}
                                 for path in trajectory_files]}


def _audit_pass5_summary(summary: dict, frozen: dict | None = None,
                         config: dict | None = None) -> dict:
    attempts = summary.get("attempts")
    if not isinstance(attempts, list):
        raise ValueError("pass5_summary_attempts_invalid")
    ordinals = [item.get("attempt_ordinal") for item in attempts]
    if ordinals != list(range(1, len(attempts) + 1)):
        raise ValueError("pass5_summary_attempt_ordinals_invalid")
    valid = [item for item in attempts if item.get("classification") == "valid"]
    invalid = [item for item in attempts if item.get("classification") == "infrastructure_invalid"]
    leakage = [item for item in attempts
               if item.get("classification") == "invalid_answer_leakage"]
    if len(valid) != 5 or [item.get("valid_trial_index") for item in valid] != [1, 2, 3, 4, 5]:
        raise ValueError("pass5_summary_valid_trial_indexes_invalid")
    if summary.get("mode") == "real":
        if frozen is None or config is None:
            raise ValueError("pass5_summary_real_audit_context_missing")
        _require_formal_pass5_config(config)
        _validate_formal_job_config(config, frozen)
        frozen_path = _bound_file(summary.get("frozen_manifest", {}),
                                  "pass5_summary_frozen_manifest")
        if _json(frozen_path) != frozen:
            raise ValueError("pass5_summary_frozen_manifest_mismatch")
        pipeline_freeze_path = _bound_file(summary.get("pipeline_freeze", {}),
                                           "pass5_summary_pipeline_freeze")
        if (summary.get("pipeline_freeze") != frozen.get("pipeline_freeze")
                or summary.get("task_sha256") != frozen.get("task", {}).get("sha256")
                or summary.get("harbor_task_checksum") != frozen.get("harbor_task_checksum")
                or summary.get("image_id") != frozen.get("image", {}).get("image_id")
                or summary.get("model_id") != config.get("model_id")
                or summary.get("agent") != config.get("agent")
                or str(summary.get("agent_version")) != str(config.get("agent_version"))
                or summary.get("trial_concurrency") != config.get("trial_concurrency")
                or config.get("harbor_job_config", {}).get("sha256")
                   != summary.get("run_authorization", {}).get("harbor_job_config_sha256")):
            raise ValueError("pass5_summary_frozen_identity_mismatch")
        if _sha256(pipeline_freeze_path) != frozen["pipeline_freeze"]["sha256"]:
            raise ValueError("pass5_summary_pipeline_freeze_mismatch")
        authorization = summary.get("run_authorization")
        if not isinstance(authorization, dict):
            raise ValueError("pass5_summary_authorization_missing")
        authorization_path = _bound_file(authorization, "pass5_summary_authorization")
        output_path = (WORKSPACE_ROOT / authorization.get("output_path", "")).resolve()
        validated_authorization = _validate_run_authorization(
            authorization_path, frozen_path, frozen, config, output_path)
        for key, value in validated_authorization.items():
            if authorization.get(key) != value:
                raise ValueError("pass5_summary_authorization_mismatch")
        receipt_path = _bound_file(authorization.get("receipt", {}),
                                   "pass5_summary_authorization_receipt")
        registry_path = _bound_file(authorization.get("registry_receipt", {}),
                                    "pass5_summary_authorization_registry_receipt")
        expected_registry = (REPORT_ROOT / "evidence/pass5_authorization_receipts"
                             / f"{authorization['nonce']}.json").resolve()
        if (receipt_path != (output_path / "authorization_receipt.json").resolve()
                or registry_path != expected_registry
                or receipt_path.read_bytes() != registry_path.read_bytes()):
            raise ValueError("pass5_summary_authorization_receipt_location_mismatch")
        receipt = _json(receipt_path)
        _validate_schema(receipt, "pass5_authorization_receipt_v1.schema.json",
                         "run_authorization_receipt_invalid")
        if (receipt.get("run_id") != authorization.get("run_id")
                or receipt.get("nonce") != authorization.get("nonce")
                or receipt.get("output_path") != authorization.get("output_path")
                or receipt.get("authorization") != {
                    "path": authorization["path"], "sha256": authorization["sha256"]}
                or receipt.get("frozen_manifest") != summary.get("frozen_manifest")):
            raise ValueError("pass5_summary_authorization_receipt_mismatch")
        if authorization.get("maximum_harbor_attempts", 0) < len(attempts):
            raise ValueError("pass5_summary_authorization_attempt_budget_exceeded")
        for field in ("trial_id", "trial_name", "trajectory_digest"):
            values = [item.get(field) for item in valid]
            if any(not value for value in values) or len(set(values)) != 5:
                raise ValueError(f"pass5_summary_{field}_not_independent")
    successes = sum(item.get("reward") == 1 for item in valid)
    if (summary.get("valid_trial_count") != len(valid)
            or summary.get("infrastructure_invalid_count") != len(invalid)
            or summary.get("answer_leakage_invalid_count") != len(leakage)
            or summary.get("success_count") != successes
            or summary.get("pass_at_5") != int(successes > 0)
            or len(valid) + len(invalid) + len(leakage) != len(attempts)):
        raise ValueError("pass5_summary_counts_invalid")
    for attempt in valid:
        if summary.get("mode") == "real":
            result_path = _bound_file(attempt.get("result", {}), "pass5_summary_result")
            verifier_path = _bound_file(attempt.get("verifier", {}), "pass5_summary_verifier")
            if verifier_path != (result_path.parent / "verifier/test_results.json").resolve():
                raise ValueError("pass5_summary_result_verifier_pair_mismatch")
            result, verifier = _json(result_path), _json(verifier_path)
            task = WORKSPACE_ROOT / frozen["task"]["path"]
            f2p, p2p = _task_test_ids(task)
            reward, statuses = _validate_verifier_details(
                verifier, _expected_test_rows(f2p, p2p))
            if (config.get("expected_test_ids") != f2p + p2p
                    or reward != attempt.get("reward")
                    or result.get("verifier_result", {}).get("rewards", {}).get("reward") != reward
                    or result.get("exception_info") is not None
                    or result.get("id") != attempt.get("trial_id")
                    or result.get("trial_name") != attempt.get("trial_name")
                    or result.get("config", {}).get("job_id") != attempt.get("job_id")
                    or result.get("started_at") != attempt.get("started_at")
                    or result.get("finished_at") != attempt.get("finished_at")
                    or any(status in {"skip", "missing", "error"} for status in statuses)):
                raise ValueError("pass5_summary_raw_trial_mismatch")
            trajectories = attempt.get("trajectory_index")
            if not isinstance(trajectories, list) or not trajectories:
                raise ValueError("pass5_summary_trajectory_index_invalid")
            trajectory_paths = [
                _bound_file(binding, "pass5_summary_trajectory") for binding in trajectories]
            digest = hashlib.sha256(json.dumps(
                sorted(_sha256(path) for path in trajectory_paths),
                sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            if digest != attempt.get("trajectory_digest"):
                raise ValueError("pass5_summary_trajectory_digest_mismatch")
            rebuilt = _classify_harbor_trial(
                result_path, config["expected_test_ids"], frozen, config)
            if rebuilt.get("classification") != "valid" or any(
                    attempt.get(key) != value for key, value in rebuilt.items()):
                raise ValueError("pass5_summary_trial_reclassification_mismatch")
        else:
            _bound_file(attempt.get("trajectory", {}), "pass5_summary_simulation_trajectory")
    for attempt in leakage:
        if summary.get("mode") == "real":
            result_path = _bound_file(attempt.get("result", {}),
                                      "pass5_summary_leakage_result")
            rebuilt = _classify_harbor_trial(
                result_path, config["expected_test_ids"], frozen, config)
            if (rebuilt.get("classification") != "invalid_answer_leakage"
                    or rebuilt.get("reason") != attempt.get("reason")
                    or rebuilt.get("answer_leakage_hits")
                       != attempt.get("answer_leakage_hits")):
                raise ValueError("pass5_summary_leakage_reclassification_mismatch")
    return {
        "schema_version": "pass5-summary-audit-v1",
        "status": "passed",
        "mode": summary["mode"],
        "instance_id": summary["instance_id"],
        "attempt_count": len(attempts),
        "valid_trial_count": len(valid),
        "infrastructure_invalid_count": len(invalid),
        "answer_leakage_invalid_count": len(leakage),
        "success_count": successes,
        "pass_at_5": int(successes > 0),
    }


def _classify_harbor_job(job: Path, expected_ids: list[str], frozen: dict,
                         config: dict, expected_trials: int) -> list[dict]:
    if job.is_symlink():
        return [{"classification": "infrastructure_invalid",
                 "reason": "harbor_job_symlink"} for _ in range(expected_trials)]
    summary_path = job / "result.json"
    trial_results = sorted(path / "result.json" for path in job.iterdir()
                           if path.is_dir() and not path.is_symlink()
                           and (path / "result.json").is_file()) if job.is_dir() else []
    classified = []
    for path in trial_results:
        try:
            item = _classify_harbor_trial(path, expected_ids, frozen, config)
            raw_trial = _json(path)
            item.setdefault("job_id", raw_trial.get("config", {}).get("job_id"))
            item.setdefault("trial_name", raw_trial.get("trial_name"))
            item["harbor_exception_observed"] = raw_trial.get("exception_info") is not None
            classified.append(item)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            classified.append({
                "classification": "infrastructure_invalid",
                "reason": "malformed_harbor_trial_artifact",
                "result": _portable_lexical(path),
                "parse_error": type(exc).__name__,
            })
    if len(trial_results) > expected_trials:
        return [{"classification": "infrastructure_invalid",
                 "reason": "unexpected_extra_harbor_trial",
                 "result": _portable(path)} for path in trial_results]
    for _ in range(expected_trials - len(trial_results)):
        classified.append({"classification": "infrastructure_invalid",
                           "reason": "missing_harbor_trial_result"})
    if not summary_path.is_file():
        for item in classified:
            if item.get("classification") == "valid":
                item.pop("reward", None)
                item["classification"] = "infrastructure_invalid"
                item["reason"] = "job_summary_missing"
            item["job_warning"] = "job_summary_missing"
        return classified
    try:
        if summary_path.is_symlink():
            raise ValueError("job summary is a symlink")
        job_summary = _json(summary_path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        for item in classified:
            if item.get("classification") == "valid":
                item.pop("reward", None)
                item["classification"] = "infrastructure_invalid"
                item["reason"] = "job_summary_malformed"
            item["job_warning"] = "job_summary_malformed"
            item["job_summary"] = _portable(summary_path)
            item["job_summary_error"] = type(exc).__name__
        return classified
    stats = job_summary.get("stats") if isinstance(job_summary.get("stats"), dict) else {}
    expected_errored = sum(item.get("harbor_exception_observed") is True for item in classified)
    summary_invalid = (
        job_summary.get("n_total_trials") != expected_trials
        or not isinstance(job_summary.get("id"), str) or not job_summary["id"]
        or not job_summary.get("finished_at")
        or stats.get("n_completed_trials") != expected_trials
        or not isinstance(stats.get("n_errored_trials"), int)
        or stats["n_errored_trials"] != expected_errored
        or any(stats.get(field) != 0 for field in (
            "n_running_trials", "n_pending_trials", "n_cancelled_trials", "n_retries"))
        or any(item.get("job_id") != job_summary.get("id") for item in classified)
        or sorted(item.get("trial_name") for item in classified if item.get("trial_name"))
           != sorted(path.parent.name for path in trial_results)
    )
    if summary_invalid:
        for item in classified:
            if item.get("classification") == "valid":
                item.pop("reward", None)
                item["classification"] = "infrastructure_invalid"
                item["reason"] = "job_summary_contract_mismatch"
            item["job_warning"] = "job_summary_contract_mismatch"
            item["job_summary"] = _portable(summary_path)
    return classified


def _classify_harbor_attempt(job: Path, expected_ids: list[str], frozen: dict,
                             config: dict) -> dict:
    """Backward-compatible single-trial classifier used by focused tests."""
    return _classify_harbor_job(job, expected_ids, frozen, config, 1)[0]


def _validate_formal_job_config(config: dict, frozen: dict) -> Path:
    """Validate the bound Harbor job for a supported formal provider."""
    provider_kind = _require_formal_pass5_config(config)
    _require_offline_agent_image(frozen, provider_kind)
    job_config_path = _bound_file(config.get("harbor_job_config", {}), "harbor_job_config")
    job_config = _json(job_config_path)
    tasks = job_config.get("tasks")
    agents = job_config.get("agents")
    if not (isinstance(tasks, list) and len(tasks) == 1 and isinstance(tasks[0], dict)
            and isinstance(agents, list) and len(agents) == 1 and isinstance(agents[0], dict)):
        raise ValueError("harbor_job_config_single_identity_required")
    agent = agents[0]
    allowed_job_keys = {
        "n_concurrent_trials", "n_attempts", "environment", "agents", "tasks",
        "retry", "load_trajectory", "resume",
    }
    allowed_agent_keys = {
        "name", "model_name", "n_concurrent", "override_timeout_sec",
        "override_setup_timeout_sec", "extra_allowed_hosts", "kwargs", "env",
        "mcp_servers", "skills",
    }
    if (set(job_config) - allowed_job_keys or set(agent) - allowed_agent_keys
            or set(agent) != allowed_agent_keys
            or set(tasks[0]) != {"path"}):
        raise ValueError("harbor_job_config_unapproved_extension")
    if (tasks[0].get("path") != frozen["task"]["path"]
            or agent.get("name") != config["agent"]
            or agent.get("model_name") != config["model_id"]
            or str(agent.get("kwargs", {}).get("version")) != str(config["agent_version"])):
        raise ValueError("harbor_job_config_binding_mismatch")
    profile = config["provider_profile"]
    runtime = config["agent_runtime"]
    if provider_kind == "kimi_k3":
        expected_kwargs = {"version": OFFICIAL_K3_AGENT_VERSION}
        expected_agent_env = {
            "KIMI_MODEL_API_KEY": "${" + profile["credential_env"] + "}",
            "KIMI_MODEL_BASE_URL": profile["base_url"],
            "KIMI_MODEL_MAX_CONTEXT_SIZE": str(runtime["max_context_size"]),
            "KIMI_MODEL_MAX_COMPLETION_TOKENS":
                OFFICIAL_K3_MAX_COMPLETION_TOKENS_ENV,
            "KIMI_MODEL_CAPABILITIES": ",".join(profile["capabilities"]),
            "KIMI_MODEL_THINKING_EFFORT": runtime["thinking_effort"],
            "KIMI_LOOP_MAX_STEPS_PER_TURN": str(runtime["max_steps_per_turn"]),
        }
    else:
        expected_kwargs = {
            "version": OFFICIAL_CODEX_AGENT_VERSION,
            "reasoning_effort": runtime["thinking_effort"],
            "web_search": "disabled",
            "config": {
                "cli_auth_credentials_store": "file",
                "mcp_servers": {},
                "features": OFFICIAL_CODEX_DISABLED_FEATURES,
            },
        }
        # Harbor provides the host's bound auth.json to the Codex sidecar. The
        # job may select that mechanism, but may not embed the credential.
        # Use a truthy selector that is not a one-character value. Harbor 0.22
        # scrubs sensitive env values by global text replacement; using "1"
        # corrupts every numeric 1 in trial JSON and trajectory files.
        expected_agent_env = {"CODEX_FORCE_AUTH_JSON": "YES"}
    if (agent.get("kwargs") != expected_kwargs
            or agent.get("env") != expected_agent_env
            or agent.get("mcp_servers") != []
            or agent.get("skills") != []
            or float(agent.get("override_timeout_sec", -1)) != runtime["timeout_sec"]
            or float(agent.get("override_setup_timeout_sec", -1))
               != runtime["setup_timeout_sec"]
            or sorted(agent.get("extra_allowed_hosts", [])) != sorted(
                [profile["allowed_host"]] if provider_kind == "kimi_k3"
                else profile["allowed_hosts"]
            )):
        raise ValueError(f"harbor_job_config_official_{provider_kind}_runtime_mismatch")
    if (job_config.get("n_attempts") not in (None, 1)
            or job_config.get("n_concurrent_trials") != config["trial_concurrency"]
            or agent.get("n_concurrent") != config["trial_concurrency"]
            or job_config.get("retry") != {"max_retries": 0}
            or job_config.get("load_trajectory") is not None
            or job_config.get("resume") is not None):
        raise ValueError("harbor_job_config_budget_invalid")
    environment = job_config.get("environment")
    policy = config.get("network_policy")
    expected_environment_keys = {"type", "delete", "kwargs", "extra_allowed_hosts"}
    if provider_kind == "codex_luna_max":
        expected_environment_keys.add("extra_docker_compose")
    if (not isinstance(environment, dict) or not isinstance(policy, dict)
            or set(environment) != expected_environment_keys
            or environment.get("type") != "docker" or environment.get("delete") is not True
            or environment.get("kwargs") != {"keep_containers": False}
            or (provider_kind == "codex_luna_max"
                and environment.get("extra_docker_compose")
                    != [OFFICIAL_CODEX_COMPOSE_OVERLAY["path"]])
            or sorted(environment.get("extra_allowed_hosts", []))
               != sorted(policy.get("environment_hosts", []))
            or sorted(agent.get("extra_allowed_hosts", []))
               != sorted(policy.get("agent_hosts", []))):
        raise ValueError("harbor_job_config_network_policy_mismatch")
    forbidden_runtime_keys = {
        "mounts", "volumes",
        "load_trajectory", "resume", "source_trial",
    }
    def contains_forbidden(value: object) -> bool:
        if isinstance(value, dict):
            return bool(forbidden_runtime_keys & value.keys()) or any(
                contains_forbidden(item) for item in value.values())
        if isinstance(value, list):
            return any(contains_forbidden(item) for item in value)
        return False
    if contains_forbidden({"environment": environment, "agent": agent, "tasks": tasks}):
        raise ValueError("harbor_job_config_unsafe_capability")
    sensitive_key = re.compile(
        r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|"
        r"password|secret|credentials)"
    )
    def reject_literal_secrets(value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "cli_auth_credentials_store" and item == "file":
                    continue
                if (sensitive_key.search(str(key))
                        and (not isinstance(item, str)
                             or not re.fullmatch(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}", item))):
                    raise ValueError("harbor_job_config_literal_secret_forbidden")
                reject_literal_secrets(item)
        elif isinstance(value, list):
            for item in value:
                reject_literal_secrets(item)
    reject_literal_secrets(job_config)
    return job_config_path


def _validate_official_k3_job_config(config: dict, frozen: dict) -> Path:
    """Backward-compatible K3-specific alias used by older callers/tests."""
    _require_official_k3_config(config)
    return _validate_formal_job_config(config, frozen)


def _harbor_runtime(config: dict, frozen: dict) -> tuple[Path, Path]:
    job_config_path = _validate_formal_job_config(config, frozen)
    executable_value = config.get("harbor_executable", "")
    raw = Path(executable_value)
    executable = (WORKSPACE_ROOT / raw).resolve()
    if (raw.is_absolute() or ".." in raw.parts or not executable.is_file()
            or executable.name != "harbor" or executable.parent.name != "bin"
            or not executable.is_relative_to(TMP_ROOT.resolve())):
        raise ValueError("harbor_executable_invalid")
    if _sha256(executable) != config.get("harbor_executable_sha256"):
        raise ValueError("harbor_executable_binding_changed")
    version = subprocess.run([str(executable), "--version"], text=True,
                             capture_output=True, check=False)
    if version.returncode or version.stdout.strip() != config.get("harbor_version"):
        raise ValueError("harbor_version_binding_changed")
    return executable, job_config_path


def _run_real_batch(config: dict, frozen: dict, output: Path, first_ordinal: int,
                    batch_size: int) -> list[dict]:
    executable, job_config = _harbor_runtime(config, frozen)
    last_ordinal = first_ordinal + batch_size - 1
    job_name = f"pass5-batch-{first_ordinal:02d}-{last_ordinal:02d}"
    job_root = output / "jobs" / job_name
    command = [str(executable), "run", "--config", str(job_config), "--job-name", job_name,
               "--jobs-dir", str((output / "jobs").resolve()), "--n-attempts", str(batch_size),
               "--n-concurrent", str(min(config["trial_concurrency"], batch_size)), "--yes"]
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    classified = _classify_harbor_job(
        job_root, config["expected_test_ids"], frozen, config, batch_size)
    if completed.returncode:
        for item in classified:
            item["command_returncode"] = completed.returncode
            if item.get("reason") == "missing_harbor_trial_result":
                item["reason"] = "harbor_command_failed_without_trial_result"
    for item in classified:
        item["command"] = command
        item["job_config"] = {"path": _portable(job_config), "sha256": _sha256(job_config)}
        item["batch"] = {"first_attempt_ordinal": first_ordinal,
                         "last_attempt_ordinal": last_ordinal,
                         "size": batch_size}
    return classified


def run_pass5(frozen_path: Path, output: Path, *, simulation: bool,
              mock_trials_path: Path | None = None,
              authorization_path: Path | None = None) -> dict:
    """Run or simulate five valid independent attempts from an exact freeze."""
    frozen_path, output = frozen_path.resolve(), output.resolve()
    frozen = _json(frozen_path)
    _validate_schema(frozen, "frozen_harbor_task_v1.schema.json",
                     "frozen_manifest_schema_invalid")
    if frozen.get("schema_version") != "frozen-harbor-task-v1" or frozen.get("state") != "frozen":
        raise ValueError("task_not_frozen")
    if simulation != (frozen.get("mode") == "simulation"):
        raise ValueError("pass5_mode_mismatch")
    if not _pass5_output_allowed(frozen["instance_id"], output, simulation=simulation):
        raise ValueError("pass5_output_must_be_case_local" if not simulation
                         else "pass5_output_must_be_numbered_runs")
    if output.exists():
        raise ValueError("pass5_output_exists")
    task = WORKSPACE_ROOT / frozen["task"]["path"]
    if not simulation:
        _validate_frozen_task_tree(task, frozen["instance_id"])
    checksum, files = _task_inventory(task)
    if checksum != frozen["task"].get("sha256") or files != frozen["task"].get("files"):
        raise ValueError("frozen_task_binding_changed")
    config_path = _bound_file(frozen.get("pass5_config", {}), "pass5_config")
    pipeline_freeze_path, pipeline_freeze = _validate_pipeline_freeze(
        frozen.get("pipeline_freeze", {})
    )
    if not simulation:
        _require_formal_freeze_ready(pipeline_freeze)
    promotion_ledger_path = _bound_file(frozen.get("promotion_ledger", {}), "promotion_ledger")
    promotion_ledger = _json(promotion_ledger_path)
    if promotion_ledger.get("current_state") != "frozen" or promotion_ledger.get("status") != "completed":
        raise ValueError("promotion_ledger_not_frozen")
    _validate_promotion_chain(promotion_ledger, frozen)
    config = _json(config_path)
    _validate_schema(config, "frozen_pass5_config_v1.schema.json",
                     "pass5_config_invalid")
    if not simulation:
        _require_formal_pass5_config(config)
    for field in ("model_id", "agent", "agent_version"):
        if config.get(field) != frozen["pass5_config"].get(field):
            raise ValueError(f"frozen_{field}_changed")
    if config.get("valid_trials") != 5:
        raise ValueError("frozen_valid_trial_count_invalid")
    if not simulation:
        _harbor_runtime(config, frozen)
    if not simulation:
        reference, expected_id = frozen["image"]["reference"], frozen["image"]["image_id"]
        inspected = subprocess.run(
            ["docker", "image", "inspect", reference, "--format", "{{.Id}}"],
            text=True, capture_output=True, check=False,
        )
        if inspected.returncode or inspected.stdout.strip() != expected_id:
            raise ValueError("frozen_image_binding_changed")
        authorization = _validate_run_authorization(authorization_path, frozen_path, frozen, config, output)
    else:
        if authorization_path is not None:
            raise ValueError("simulation_authorization_not_allowed")
        authorization = {"mode": "simulation_exempt"}

    output.mkdir(parents=True)
    if not simulation:
        try:
            authorization["receipt"] = _consume_run_authorization(output, frozen_path, authorization)
            registry_path = REPORT_ROOT / "evidence/pass5_authorization_receipts" / f"{authorization['nonce']}.json"
            authorization["registry_receipt"] = {
                "path": _portable(registry_path), "sha256": _sha256(registry_path)}
        except Exception as exc:
            _write(output / "pass5_rejection.json", {
                "schema_version": "pass5-rejection-v1", "status": "rejected",
                "state": "frozen", "instance_id": frozen["instance_id"],
                "code": str(exc), "valid_trial_count": 0,
                "infrastructure_invalid_count": 0,
                "answer_leakage_invalid_count": 0, "attempts": [],
            })
            raise
    attempts: list[dict] = []
    mock_attempts = []
    if simulation:
        if mock_trials_path is None:
            raise ValueError("simulation_mock_trials_required")
        mock = _json(mock_trials_path)
        if mock.get("schema_version") != "pass5-mock-trials-v1":
            raise ValueError("mock_trials_schema_invalid")
        mock_attempts = list(mock.get("attempts", []))
    max_invalid = config.get("max_invalid_replacements", 10)
    if not simulation:
        max_invalid = min(max_invalid, authorization["maximum_harbor_attempts"] - 5)
    valid_count = invalid_count = leakage_count = success_count = 0
    ordinal = 0
    try:
        while valid_count < 5:
            if simulation:
                if ordinal >= len(mock_attempts):
                    raise ValueError("mock_trials_exhausted_before_five_valid")
                batch = [dict(mock_attempts[ordinal])]
            else:
                remaining_budget = authorization["maximum_harbor_attempts"] - ordinal
                if remaining_budget <= 0:
                    raise ValueError("harbor_attempt_budget_exhausted")
                batch_size = min(config["trial_concurrency"], 5 - valid_count,
                                 remaining_budget)
                batch = _run_real_batch(config, frozen, output, ordinal + 1, batch_size)
            batch_error: str | None = None
            for attempt in batch:
                attempt_error: str | None = None
                ordinal += 1
                classification = attempt.get("classification")
                attempt["attempt_ordinal"] = ordinal
                if classification == "valid":
                    reward = attempt.get("reward")
                    if reward not in (0, 1):
                        attempt_error = "valid_trial_reward_invalid"
                    elif simulation:
                        trajectory_path = _bound_file(attempt.get("trajectory", {}), "trajectory")
                        attempt["trajectory"] = {"path": _portable(trajectory_path),
                                                 "sha256": _sha256(trajectory_path)}
                    elif not attempt.get("trajectory_index"):
                        attempt_error = "valid_trial_trajectory_missing"
                    if attempt_error is None:
                        valid_count += 1
                        success_count += int(reward == 1)
                        attempt["valid_trial_index"] = valid_count
                elif classification == "infrastructure_invalid":
                    invalid_count += 1
                elif classification == "invalid_answer_leakage":
                    leakage_count += 1
                else:
                    attempt_error = "trial_classification_invalid"
                batch_error = batch_error or attempt_error
                attempts.append(attempt)
                _write(output / "attempts.json", {"status": "running", "attempts": attempts})
            if invalid_count + leakage_count > max_invalid:
                batch_error = batch_error or "invalid_trial_replacement_budget_exhausted"
            if batch_error is not None:
                raise ValueError(batch_error)
    except Exception as exc:
        _write(output / "pass5_rejection.json", {
            "schema_version": "pass5-rejection-v1",
            "status": "rejected",
            "state": "frozen",
            "instance_id": frozen["instance_id"],
            "code": str(exc),
            "valid_trial_count": valid_count,
            "infrastructure_invalid_count": invalid_count,
            "answer_leakage_invalid_count": leakage_count,
            "attempts": attempts,
        })
        _write(output / "attempts.json", {"status": "rejected", "attempts": attempts})
        raise

    completion_ledger = dict(promotion_ledger)
    completion_ledger["events"] = list(promotion_ledger["events"])
    _advance(completion_ledger, "pass5_completed", "five_valid_trials_completed", {
        "valid_trial_count": valid_count, "infrastructure_invalid_count": invalid_count,
        "answer_leakage_invalid_count": leakage_count,
        "success_count": success_count,
    })
    completion_ledger["status"] = "completed"
    completion_ledger_path = output / "pipeline_state_ledger.json"
    summary = {
        "schema_version": "frozen-pass5-summary-v1",
        "state": "pass5_completed",
        "mode": "simulation" if simulation else "real",
        "instance_id": frozen["instance_id"],
        "frozen_manifest": {"path": _portable(frozen_path), "sha256": _sha256(frozen_path)},
        "pipeline_freeze": {"path": _portable(pipeline_freeze_path),
                            "sha256": _sha256(pipeline_freeze_path)},
        "task_sha256": checksum,
        "harbor_task_checksum": frozen["harbor_task_checksum"],
        "image_id": frozen["image"]["image_id"],
        "model_id": config["model_id"],
        "agent": config["agent"],
        "agent_version": config["agent_version"],
        "trial_concurrency": 1 if simulation else config["trial_concurrency"],
        "run_authorization": authorization,
        "state_ledger": {"path": _portable(completion_ledger_path),
                         "sha256": _json_sha(completion_ledger)},
        "valid_trial_count": valid_count,
        "infrastructure_invalid_count": invalid_count,
        "answer_leakage_invalid_count": leakage_count,
        "success_count": success_count,
        "pass_at_5": 1 if success_count else 0,
        "attempts": attempts,
        "unexecuted_real_work": [] if not simulation else [
            "real_visual_human_gate", "real_f2p_p2p_human_gate",
            "real_docker_image_build_and_freeze", "real_frontier_model_pass5",
        ],
    }
    _validate_schema(summary, "frozen_pass5_summary_v1.schema.json",
                     "pass5_summary_schema_invalid")
    audit = _audit_pass5_summary(summary, frozen, config)
    _write(output / "attempts.json", {"status": "completed", "attempts": attempts})
    _write(output / "pass5_summary.json", summary)
    audit["summary"] = {"path": _portable(output / "pass5_summary.json"),
                        "sha256": _sha256(output / "pass5_summary.json")}
    _validate_schema(audit, "pass5_summary_audit_v1.schema.json",
                     "pass5_summary_audit_schema_invalid")
    _write(output / "pass5_summary_audit.json", audit)
    _render_audit(frozen, completion_ledger, summary, output / "pipeline_audit.html")
    # This is the terminal completion marker. Publish it only after every
    # summary/audit artifact has been validated and written successfully.
    _write_ledger(completion_ledger_path, completion_ledger)
    return summary


def _render_audit(frozen: dict, ledger: dict, summary: dict, output: Path) -> None:
    def esc(value: object) -> str:
        return html.escape(str(value))

    events = "".join(
        f"<tr><td>{esc(index + 1)}</td><td>{esc(event['from'])}</td><td>{esc(event['to'])}</td>"
        f"<td><span class='{esc(event['status'])}'>{esc(event['status'])}</span></td>"
        f"<td>{esc(event['code'])}</td><td><code>{esc(json.dumps(event.get('evidence', {}), ensure_ascii=False))}</code></td></tr>"
        for index, event in enumerate(ledger["events"])
    )
    attempts = "".join(
        f"<tr><td>{esc(item['attempt_ordinal'])}</td><td>{esc(item.get('valid_trial_index', '—'))}</td>"
        f"<td>{esc(item['classification'])}</td><td>{esc(item.get('reward', '—'))}</td>"
        f"<td>{esc(item.get('reason', '—'))}</td><td><code>{esc(json.dumps(item.get('trajectory', item.get('trajectory_index', [])), ensure_ascii=False))}</code></td></tr>"
        for item in summary["attempts"]
    )
    pending = "".join(f"<li>{esc(item)}</li>" for item in summary["unexecuted_real_work"])
    contracts = "".join(
        f"<tr><td>{esc(source)} → {esc(target)}</td><td>{esc(inputs)}</td><td>{esc(outputs)}</td><td><code>{esc(rejections)}</code></td></tr>"
        for source, target, inputs, outputs, rejections in TRANSITION_CONTRACTS
    )
    banner = "SIMULATION ONLY — no human approval, Docker freeze, or external model call" if summary["mode"] == "simulation" else "REAL FROZEN RUN"
    document = f"""<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><title>Pipeline audit · {esc(summary['instance_id'])}</title>
<style>body{{font:13px/1.4 system-ui;margin:24px;color:#202124}}h1{{margin:0 0 8px}}.banner{{padding:10px;background:#fff3cd;border:1px solid #e0b400;font-weight:700}}.grid{{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:8px;margin:12px 0}}.card{{border:1px solid #ddd;padding:8px}}table{{width:100%;border-collapse:collapse;margin:8px 0 18px}}th,td{{border-bottom:1px solid #ddd;padding:6px;text-align:left;vertical-align:top}}code{{font-size:11px;white-space:pre-wrap;word-break:break-all}}.accepted,.valid{{color:#08783e;font-weight:700}}.rejected,.infrastructure_invalid,.invalid_answer_leakage{{color:#a33;font-weight:700}}ul{{margin-top:4px}}</style></head><body>
<h1>Harbor task pipeline audit</h1><div class='banner'>{esc(banner)}</div>
<div class='grid'><div class='card'><b>Instance</b><br>{esc(summary['instance_id'])}</div><div class='card'><b>State</b><br>{esc(summary['state'])}</div><div class='card'><b>Pass@5</b><br>{esc(summary['pass_at_5'])} ({esc(summary['success_count'])}/5)</div><div class='card'><b>Concurrency</b><br>{esc(summary['trial_concurrency'])}</div><div class='card'><b>Infra replaced</b><br>{esc(summary['infrastructure_invalid_count'])}</div><div class='card'><b>Leakage replaced</b><br>{esc(summary['answer_leakage_invalid_count'])}</div></div>
<div class='grid'><div class='card'><b>Task SHA</b><br><code>{esc(summary['task_sha256'])}</code></div><div class='card'><b>Harbor task checksum</b><br><code>{esc(summary['harbor_task_checksum'])}</code></div><div class='card'><b>Image ID</b><br><code>{esc(summary['image_id'])}</code></div><div class='card'><b>Model</b><br>{esc(summary['model_id'])}</div><div class='card'><b>Agent</b><br>{esc(summary['agent'])} · {esc(summary['agent_version'])}</div></div>
<h2>Frozen transition contract</h2><table><thead><tr><th>Transition</th><th>Required input</th><th>Durable output</th><th>Rejection reasons</th></tr></thead><tbody>{contracts}</tbody></table>
<h2>State transitions</h2><table><thead><tr><th>#</th><th>From</th><th>To</th><th>Decision</th><th>Code</th><th>Bound evidence</th></tr></thead><tbody>{events}</tbody></table>
<h2>Pass@5 attempts</h2><table><thead><tr><th>Attempt</th><th>Valid #</th><th>Class</th><th>Reward</th><th>Reason</th><th>Trajectory</th></tr></thead><tbody>{attempts}</tbody></table>
<h2>Real work not executed</h2><ul>{pending or '<li>None</li>'}</ul>
<p><b>Frozen manifest:</b> <code>{esc(summary['frozen_manifest']['path'])}</code><br><b>Completion ledger:</b> <code>{esc(summary['state_ledger']['path'])}</code></p>
</body></html>"""
    output.write_text(document)
