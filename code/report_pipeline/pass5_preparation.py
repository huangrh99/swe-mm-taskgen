"""Prepare network-isolated Pass@1/Pass@5 configurations without running agents.

The generated files are deliberately named ``*.pending.json``.  They bind the
current task bytes and Harbor executable for preparation, but they do not
invent an image ID, a frozen-task manifest, completed controls, or run
authorization.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from report_pipeline.atomic import write_json
from report_pipeline.paths import REPORT_ROOT, RUNTIME_ROOT, TMP_ROOT, WORKSPACE_ROOT
from report_pipeline.workflow import (
    OFFICIAL_CODEX_AGENT,
    OFFICIAL_CODEX_AGENT_HOSTS,
    OFFICIAL_CODEX_AGENT_RUNTIME,
    OFFICIAL_CODEX_AGENT_VERSION,
    OFFICIAL_CODEX_COMPOSE_OVERLAY,
    OFFICIAL_CODEX_DISABLED_FEATURES,
    OFFICIAL_CODEX_MODEL_ID,
    OFFICIAL_CODEX_PROVIDER_PROFILE,
    OFFICIAL_CODEX_TOOL_POLICY,
    OFFICIAL_K3_AGENT,
    OFFICIAL_K3_AGENT_HOSTS,
    OFFICIAL_K3_AGENT_RUNTIME,
    OFFICIAL_K3_AGENT_VERSION,
    OFFICIAL_K3_MAX_COMPLETION_TOKENS_ENV,
    OFFICIAL_K3_MODEL_ID,
    OFFICIAL_K3_PROVIDER_PROFILE,
    OFFICIAL_K3_TOOL_POLICY,
    _require_formal_pass5_config,
    _sha256,
    _task_inventory,
    _validate_formal_job_config,
    _validate_schema,
)
from report_pipeline.task_projection import materialize


SEVEN_CASE_IDS = (
    "bpmn-io__bpmn-js-2396",
    "googlechrome__lighthouse-16403",
    "automattic__wp-calypso-100957",
    "automattic__wp-calypso-99049",
    "mermaid-js__mermaid-7711",
    "excalidraw__excalidraw-9002",
)

DEFAULT_HARBOR_EXECUTABLE = (
    RUNTIME_ROOT / "venv/bin/harbor"
)
DEFAULT_CASE_ARTIFACTS_ROOT = REPORT_ROOT / "cases"


def _portable(path: Path) -> str:
    try:
        return path.resolve().relative_to(WORKSPACE_ROOT.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError(f"path_outside_workspace:{path}") from exc


def _load_expected_test_ids(task: Path) -> list[str]:
    config_path = task / "tests/config.json"
    value = json.loads(config_path.read_text())
    result: list[str] = []
    for field in ("FAIL_TO_PASS", "PASS_TO_PASS"):
        items = value.get(field)
        if not isinstance(items, list) or not all(
            isinstance(item, str) and item for item in items
        ):
            raise ValueError(f"invalid_test_id_list:{field}:{config_path}")
        result.extend(items)
    if not result or len(result) != len(set(result)):
        raise ValueError(f"expected_test_ids_empty_or_duplicated:{config_path}")
    return result


def _harbor_binding(executable: Path) -> dict:
    executable = executable.resolve()
    if not executable.is_file() or not executable.is_relative_to(RUNTIME_ROOT.resolve()):
        raise ValueError("harbor_executable_must_be_existing_file_under_runtime_root")
    completed = subprocess.run(
        [str(executable), "--version"], text=True, capture_output=True, check=False
    )
    if completed.returncode or not completed.stdout.strip():
        raise ValueError("harbor_version_probe_failed")
    return {
        "path": _portable(executable),
        "sha256": _sha256(executable),
        "version": completed.stdout.strip(),
    }


def _agent(provider: str, concurrency: int) -> dict:
    if provider == "kimi-k3":
        runtime = OFFICIAL_K3_AGENT_RUNTIME
        profile = OFFICIAL_K3_PROVIDER_PROFILE
        return {
            "name": OFFICIAL_K3_AGENT,
            "model_name": OFFICIAL_K3_MODEL_ID,
            "n_concurrent": concurrency,
            "override_timeout_sec": runtime["timeout_sec"],
            "override_setup_timeout_sec": runtime["setup_timeout_sec"],
            "extra_allowed_hosts": list(OFFICIAL_K3_AGENT_HOSTS),
            "kwargs": {"version": OFFICIAL_K3_AGENT_VERSION},
            "env": {
                "KIMI_MODEL_API_KEY": "${" + profile["credential_env"] + "}",
                "KIMI_MODEL_BASE_URL": profile["base_url"],
                "KIMI_MODEL_MAX_CONTEXT_SIZE": str(runtime["max_context_size"]),
                "KIMI_MODEL_MAX_COMPLETION_TOKENS":
                    OFFICIAL_K3_MAX_COMPLETION_TOKENS_ENV,
                "KIMI_MODEL_CAPABILITIES": ",".join(profile["capabilities"]),
                "KIMI_MODEL_THINKING_EFFORT": runtime["thinking_effort"],
                "KIMI_LOOP_MAX_STEPS_PER_TURN":
                    str(runtime["max_steps_per_turn"]),
            },
            "mcp_servers": [],
            "skills": [],
        }
    if provider == "codex-luna-max":
        runtime = OFFICIAL_CODEX_AGENT_RUNTIME
        return {
            "name": OFFICIAL_CODEX_AGENT,
            "model_name": OFFICIAL_CODEX_MODEL_ID,
            "n_concurrent": concurrency,
            "override_timeout_sec": runtime["timeout_sec"],
            "override_setup_timeout_sec": runtime["setup_timeout_sec"],
            "extra_allowed_hosts": list(OFFICIAL_CODEX_AGENT_HOSTS),
            "kwargs": {
                "version": OFFICIAL_CODEX_AGENT_VERSION,
                "reasoning_effort": runtime["thinking_effort"],
                "web_search": "disabled",
                "config": {
                    "cli_auth_credentials_store": "file",
                    "mcp_servers": {},
                    "features": dict(OFFICIAL_CODEX_DISABLED_FEATURES),
                },
            },
            "env": {"CODEX_FORCE_AUTH_JSON": "YES"},
            "mcp_servers": [],
            "skills": [],
        }
    raise ValueError(f"unknown_provider:{provider}")


def _job(task_path: str, provider: str, concurrency: int) -> dict:
    environment = {
        "type": "docker",
        "delete": True,
        "kwargs": {"keep_containers": False},
        "extra_allowed_hosts": [],
    }
    if provider == "codex-luna-max":
        environment["extra_docker_compose"] = [OFFICIAL_CODEX_COMPOSE_OVERLAY["path"]]
    return {
        # run-frozen-pass5 controls the actual batch size on the command line.
        # Keeping this at one is part of workflow's fail-closed job contract.
        "n_concurrent_trials": concurrency,
        "n_attempts": 1,
        "environment": environment,
        "agents": [_agent(provider, concurrency)],
        "tasks": [{"path": task_path}],
        "retry": {"max_retries": 0},
        "load_trajectory": None,
        "resume": None,
    }


def _config(
    provider: str,
    concurrency: int,
    expected_test_ids: list[str],
    harbor: dict,
    job_path: Path,
) -> dict:
    if provider == "kimi-k3":
        identity = {
            "model_id": OFFICIAL_K3_MODEL_ID,
            "agent": OFFICIAL_K3_AGENT,
            "agent_version": OFFICIAL_K3_AGENT_VERSION,
            "provider_profile": dict(OFFICIAL_K3_PROVIDER_PROFILE),
            "agent_runtime": dict(OFFICIAL_K3_AGENT_RUNTIME),
            "network_policy": {
                "environment_hosts": [],
                "agent_hosts": list(OFFICIAL_K3_AGENT_HOSTS),
            },
            "tool_policy": json.loads(json.dumps(OFFICIAL_K3_TOOL_POLICY)),
        }
    else:
        identity = {
            "model_id": OFFICIAL_CODEX_MODEL_ID,
            "agent": OFFICIAL_CODEX_AGENT,
            "agent_version": OFFICIAL_CODEX_AGENT_VERSION,
            "provider_profile": json.loads(json.dumps(OFFICIAL_CODEX_PROVIDER_PROFILE)),
            "agent_runtime": dict(OFFICIAL_CODEX_AGENT_RUNTIME),
            "network_policy": {
                "environment_hosts": [],
                "agent_hosts": list(OFFICIAL_CODEX_AGENT_HOSTS),
            },
            "tool_policy": json.loads(json.dumps(OFFICIAL_CODEX_TOOL_POLICY)),
        }
    return {
        "schema_version": "frozen-pass5-config-v1",
        **identity,
        "valid_trials": 5,
        "trial_concurrency": concurrency,
        "max_invalid_replacements": 10,
        "expected_test_ids": expected_test_ids,
        "harbor_executable": harbor["path"],
        "harbor_executable_sha256": harbor["sha256"],
        "harbor_version": harbor["version"],
        "harbor_job_config": {
            "path": _portable(job_path),
            "sha256": _sha256(job_path),
        },
    }


def _find_current_frozen_manifest(
    evidence_case: Path, task_sha256: str
) -> tuple[dict | None, list[str]]:
    stale: list[str] = []
    for path in sorted((evidence_case / "outputs").rglob("*.json")):
        try:
            value = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict):
            continue
        if value.get("schema_version") != "frozen-harbor-task-v1":
            continue
        task = value.get("task", {})
        image = value.get("image", {})
        if (
            value.get("state") == "frozen"
            and task.get("sha256") == task_sha256
            and isinstance(image.get("image_id"), str)
            and image["image_id"].startswith("sha256:")
        ):
            return value, stale
        stale.append(_portable(path))
    return None, stale


def prepare_case(case: Path, artifact_case: Path, harbor: dict) -> dict:
    case = case.resolve()
    artifact_case = artifact_case.resolve()
    if not (case / "task.toml").is_file():
        raise ValueError(f"case_task_missing:{case.name}")
    projection = materialize(case)
    task = projection["path"]
    task_path = _portable(task)
    task_sha256, task_files = projection["sha256"], projection["files"]
    expected_test_ids = _load_expected_test_ids(task)
    output = artifact_case / "outputs/09_network_policy_remediation"
    artifacts: list[dict] = []

    for provider in ("kimi-k3", "codex-luna-max"):
        provider_output = output / provider
        pass5_concurrency = 2 if provider == "kimi-k3" else 5
        for label, concurrency in (("pass1", 1), ("pass5", pass5_concurrency)):
            job_path = provider_output / f"{label}_job.pending.json"
            config_path = provider_output / f"{label}_frozen_pass5_config.pending.json"
            write_json(job_path, _job(task_path, provider, concurrency))
            config = _config(provider, concurrency, expected_test_ids, harbor, job_path)
            _validate_schema(config, "frozen_pass5_config_v1.schema.json", "pass5_config_invalid")
            _require_formal_pass5_config(config)
            _validate_formal_job_config(config, {"task": {"path": task_path}})
            write_json(config_path, config)
            artifacts.extend([
                {"path": _portable(job_path), "sha256": _sha256(job_path)},
                {"path": _portable(config_path), "sha256": _sha256(config_path)},
            ])

    frozen, stale_manifests = _find_current_frozen_manifest(artifact_case, task_sha256)
    reasons = []
    if frozen is None:
        reasons.extend(["current_frozen_manifest_missing", "current_image_binding_missing"])
    reasons.extend(["current_controls_not_bound", "run_authorization_missing"])
    status = {
        "schema_version": "network-isolated-pass5-preparation-v1",
        "instance_id": case.name,
        "status": "prepared_not_launchable" if reasons else "prepared",
        "launch_authorized": False,
        "model_invoked": False,
        "task": {
            "path": task_path,
            "source_case": _portable(case),
            "projection_entries": projection["entries"],
            "sha256": task_sha256,
            "file_count": len(task_files),
        },
        "harbor": harbor,
        "expected_test_ids": {
            "count": len(expected_test_ids),
            "source": _portable(case / "tests/config.json"),
            "source_sha256": _sha256(case / "tests/config.json"),
        },
        "freeze_binding": {
            "current": frozen is not None,
            "image_reference": frozen.get("image", {}).get("reference") if frozen else None,
            "image_id": frozen.get("image", {}).get("image_id") if frozen else None,
            "stale_frozen_manifests": stale_manifests,
        },
        "blocking_reasons": reasons,
        "artifacts": artifacts,
    }
    status_path = output / "00_preparation_status.json"
    write_json(status_path, status)
    return {"status_path": _portable(status_path), **status}


def run(
    case_ids: list[str] | None = None,
    *,
    cases_root: Path = REPORT_ROOT / "cases",
    case_artifacts_root: Path = DEFAULT_CASE_ARTIFACTS_ROOT,
    harbor_executable: Path = DEFAULT_HARBOR_EXECUTABLE,
) -> dict:
    selected = list(case_ids or SEVEN_CASE_IDS)
    if not selected or len(selected) != len(set(selected)):
        raise ValueError("case_ids_empty_or_duplicated")
    harbor = _harbor_binding(harbor_executable)
    results = [
        prepare_case(cases_root / case_id, case_artifacts_root / case_id, harbor)
        for case_id in selected
    ]
    return {
        "schema_version": "network-isolated-pass5-preparation-batch-v1",
        "status": "prepared_not_launchable"
        if any(item["blocking_reasons"] for item in results)
        else "prepared",
        "case_count": len(results),
        "launch_authorized_count": sum(bool(item["launch_authorized"]) for item in results),
        "results": results,
    }
