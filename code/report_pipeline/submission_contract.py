"""Validate only the static written-test layout, never claim runtime readiness."""

from __future__ import annotations

import json
import re
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 compatibility for the public launcher.
    from pip._vendor import tomli as tomllib
from pathlib import Path


TASK_NAME = re.compile(r"^[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-[1-9][0-9]*$")
REQUIRED_TASK_FILES = (
    "environment/Dockerfile",
    "environment/base_image.json",
    "instruction.md",
    "solution/solve.sh",
    "solution/gold.patch",
    "task.toml",
    "tests/config.json",
    "tests/test.sh",
)


def _error(errors: list[dict], code: str, **details: object) -> None:
    errors.append({"code": code, **details})


def _warning(warnings: list[dict], code: str, **details: object) -> None:
    warnings.append({"code": code, **details})


def _instruction_asset_references(text: str) -> set[str]:
    """Return explicit assets plus compact ``asset_01 through asset_06`` ranges."""
    references = set(re.findall(r"/testbed/assets/([A-Za-z0-9_.-]+)", text))
    range_pattern = re.compile(
        r"/testbed/assets/(.*?)(\d+)(\.[A-Za-z0-9]+)`?\s+through\s+`?"
        r"/testbed/assets/\1(\d+)\3"
    )
    for match in range_pattern.finditer(text):
        prefix, start, suffix, end = match.groups()
        width = len(start)
        for index in range(int(start), int(end) + 1):
            references.add(f"{prefix}{index:0{width}d}{suffix}")
    return references


def validate(root: Path, minimum_tasks: int = 5) -> dict:
    root = root.resolve()
    errors: list[dict] = []
    warnings: list[dict] = []
    for relative in ("README.md", "SUBMISSION_CONTRACT.md", "pipeline_design.svg", "code"):
        if not (root / relative).exists():
            _error(errors, "missing_submission_artifact", path=relative)

    cases_root = root / "cases"
    if not cases_root.is_dir():
        _error(errors, "missing_submission_artifact", path="cases")
        tasks = []
    else:
        candidates = sorted(path for path in cases_root.iterdir()
                            if path.is_dir() and TASK_NAME.fullmatch(path.name))
        tasks = [path for path in candidates if (path / "task.toml").is_file()]
    if len(tasks) < minimum_tasks:
        _error(errors, "insufficient_iid_tasks", expected_minimum=minimum_tasks,
               observed=len(tasks))

    task_records = []
    for case in tasks:
        task_errors: list[dict] = []
        task_warnings: list[dict] = []
        task = case
        allowed = {
            "environment", "instruction.md", "solution", "task.toml", "tests",
            "meta", "outputs",
        }
        for entry in sorted(path.name for path in case.iterdir() if path.name not in allowed):
            _error(task_errors, "non_submission_root_entry", path=entry)
        for relative in REQUIRED_TASK_FILES:
            if not (task / relative).is_file():
                _error(task_errors, "missing_task_file", path=relative)

        assets = sorted(path for path in (task / "environment/assets").glob("**/*")
                        if path.is_file()) if (task / "environment/assets").is_dir() else []
        if not assets:
            _error(task_errors, "missing_visual_asset")

        task_toml = task / "task.toml"
        if task_toml.is_file():
            try:
                config = tomllib.loads(task_toml.read_text())
                environment = config.get("environment", {})
                if config.get("schema_version") != "1.2":
                    _error(task_errors, "wrong_task_schema",
                           observed=config.get("schema_version"))
                if environment.get("allow_internet") is not False:
                    _error(task_errors, "task_not_offline")
                for field in ("cpus", "memory_mb", "storage_mb"):
                    if field not in environment:
                        _error(task_errors, "missing_environment_field", field=field)
                if "timeout_sec" not in config.get("agent", {}):
                    _error(task_errors, "missing_agent_timeout")
                if "timeout_sec" not in config.get("verifier", {}):
                    _error(task_errors, "missing_verifier_timeout")
            except Exception as exc:
                _error(task_errors, "invalid_task_toml", error=f"{type(exc).__name__}: {exc}")

        instruction = task / "instruction.md"
        if instruction.is_file():
            text = instruction.read_text()
            if "/testbed" not in text:
                _error(task_errors, "instruction_missing_testbed")
            if "/testbed/assets/" not in text:
                _error(task_errors, "instruction_missing_asset_reference")
            referenced_assets = _instruction_asset_references(text)
            for asset in assets:
                if asset.name not in referenced_assets:
                    _error(task_errors, "unreferenced_visual_asset", asset=asset.name)

        dockerfile = task / "environment/Dockerfile"
        if dockerfile.is_file():
            from_lines = [line.strip() for line in dockerfile.read_text().splitlines()
                          if line.strip().upper().startswith("FROM ")]
            final_from = from_lines[-1] if from_lines else ""
            reference_match = re.fullmatch(
                r"FROM\s+(visual-harbor-base:[0-9a-f]{64}|[^\s@]+@sha256:[0-9a-f]{64})"
                r"(?:\s+AS\s+[A-Za-z0-9_.-]+)?",
                final_from,
                re.IGNORECASE,
            )
            if not reference_match:
                _error(task_errors, "base_image_reference_not_frozen", observed=final_from)
            binding_path = task / "environment/base_image.json"
            if binding_path.is_file():
                try:
                    binding = json.loads(binding_path.read_text())
                    expected_ref = (reference_match.group(1) if reference_match else
                                    final_from.removeprefix("FROM ").split()[0])
                    image_id = binding.get("image_id")
                    expected_parent_id = ("sha256:" + expected_ref.rsplit(":", 1)[-1]
                                          if expected_ref.startswith("visual-harbor-base:")
                                          else None)
                    reference_matches = (binding.get("build_reference") == expected_ref
                                         or (expected_parent_id is not None
                                             and binding.get("parent_image_id") == expected_parent_id))
                    if (not reference_matches
                            or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(image_id))):
                        _error(task_errors, "base_image_binding_mismatch")
                    if "@sha256:" in expected_ref and binding.get("repo_digest") != expected_ref:
                        _error(task_errors, "base_image_repo_digest_mismatch")
                    if expected_ref.startswith("visual-harbor-base:"):
                        for field in ("offline_archive", "offline_archive_sha256"):
                            if not binding.get(field):
                                _warning(task_warnings, "missing_local_base_restore_binding", field=field)
                        if binding.get("offline_archive_sha256") and not re.fullmatch(
                            r"[0-9a-f]{64}", str(binding["offline_archive_sha256"])
                        ):
                            _error(task_errors, "invalid_local_base_archive_sha256")
                except Exception as exc:
                    _error(task_errors, "invalid_base_image_binding",
                           error=f"{type(exc).__name__}: {exc}")

        config_path = task / "tests/config.json"
        if config_path.is_file():
            try:
                judge = json.loads(config_path.read_text())
                for field in ("repo", "instance_id", "base_commit", "FAIL_TO_PASS",
                              "PASS_TO_PASS", "log_parser"):
                    if field not in judge:
                        _error(task_errors, "missing_judge_field", field=field)
                for field in ("FAIL_TO_PASS", "PASS_TO_PASS"):
                    if not isinstance(judge.get(field), list) or not judge.get(field):
                        _error(task_errors, "empty_judge_inventory", field=field)
                f2p, p2p = judge.get("FAIL_TO_PASS", []), judge.get("PASS_TO_PASS", [])
                if len(f2p) != len(set(f2p)) or len(p2p) != len(set(p2p)):
                    _error(task_errors, "duplicate_judge_test_id")
                if set(f2p) & set(p2p):
                    _error(task_errors, "overlapping_f2p_p2p")
                if not re.fullmatch(r"[0-9a-f]{40}", str(judge.get("base_commit", ""))):
                    _error(task_errors, "invalid_base_commit")
            except Exception as exc:
                _error(task_errors, "invalid_judge_config", error=f"{type(exc).__name__}: {exc}")

        solve = task / "solution/solve.sh"
        if solve.is_file() and "git apply" not in solve.read_text():
            _error(task_errors, "gold_patch_not_git_apply")
        test_script = task / "tests/test.sh"
        if test_script.is_file():
            script = test_script.read_text()
            if "/logs/verifier" not in script:
                _error(task_errors, "test_script_missing_reward_path")
            payloads = [path for path in (task / "tests").rglob("*") if path.is_file()
                        and path.name not in {"config.json", "integrity.sha256", "test.sh",
                                              "test_manifest.json"}]
            referenced_payload = any(f"/tests/{path.relative_to(task / 'tests').as_posix()}" in script
                                     for path in payloads)
            # The shell may invoke a grader which then installs hidden payloads;
            # requiring one hard-coded `test.patch`/Python filename rejects valid
            # Harbor tasks and confuses packaging with the judge contract.
            if not payloads:
                _error(task_errors, "missing_judge_payload")
            elif not referenced_payload:
                _error(task_errors, "test_script_missing_judge_entrypoint")

        task_records.append({"instance_id": case.name, "task_path": ".",
                             "asset_count": len(assets),
                             "status": "valid_static_contract" if not task_errors else "invalid",
                             "errors": task_errors, "warnings": task_warnings})
        errors.extend({"instance_id": case.name, **item} for item in task_errors)
        warnings.extend({"instance_id": case.name, **item} for item in task_warnings)

    static_complete = not errors
    return {
        "schema_version": "exam-submission-validation-v1",
        "validation_scope": "static_layout_only",
        "status": "static_layout_complete_not_exam_ready" if static_complete else "incomplete",
        "exam_readiness": "not_evaluated",
        "exam_ready": False,
        "runtime_acceptance_required": [
            "empty_patch_reward_0", "gold_patch_reward_1", "negative_controls",
            "visual_necessity_human_gate", "f2p_p2p_human_gate", "frozen_environment",
        ],
        "minimum_iid_tasks": minimum_tasks,
        "observed_iid_tasks": len(tasks),
        "tasks": task_records,
        "errors": errors,
        "warnings": warnings,
    }
