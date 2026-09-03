"""Resumable orchestration for the five public visual-candidate stages.

The existing commands remain the implementation and debugging surface.  This
module provides the smaller operational interface: one plan-bound command per
stage, with command allowlists, checkpoints, and content-bound outputs.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

from report_pipeline.atomic import assert_no_symlink_chain, write_bytes, write_json


SCHEMA_VERSION = "pipeline-stage-plan-v1"
RUN_SCHEMA_VERSION = "pipeline-stage-run-v1"

STAGE_COMMANDS = {
    "prepare-pr-pool": frozenset({
        "collect", "export-indexed-prs", "screen-images", "type-media",
        "filter-merged",
    }),
    "recall-and-archive": frozenset({
        "select-balanced-recall", "probe-linked-issue-media", "archive-sources",
        "archive-selection-waves", "audit-source-archives", "recall-repairs",
    }),
    "construct-solver-inputs": frozenset({
        "classify-pr-images", "audit-pr-images", "select-solver-inputs",
        "visual-index",
    }),
    "screen-multimodal-candidates": frozenset({
        "verify-visual", "text-sufficiency", "aggregate-text-runs",
        "classify-before-review", "audit-category-distribution",
        "classify-capabilities", "convert-v3-capabilities",
        "build-capability-pool", "render-capability-pool", "unify-visual-review",
    }),
    "review-visual-gate": frozenset({
        "human-review", "audit-review", "render-visual-gate-review",
        "audit-visual-gate-review",
    }),
}

STEP_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
METRIC_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_binding(path: Path) -> dict[str, Any]:
    assert_no_symlink_chain(path)
    resolved = path.resolve(strict=True)
    if resolved.is_file():
        return {"path": str(resolved), "kind": "file", "sha256": _sha256(resolved),
                "bytes": resolved.stat().st_size}
    if not resolved.is_dir():
        raise ValueError(f"declared output is not a regular file or directory: {resolved}")
    digest = hashlib.sha256()
    files = 0
    size = 0
    for item in sorted(candidate for candidate in resolved.rglob("*") if candidate.is_file()):
        if item.is_symlink():
            raise ValueError(f"declared output contains a symlink: {item}")
        relative = item.relative_to(resolved).as_posix()
        item_sha = _sha256(item)
        item_size = item.stat().st_size
        digest.update(relative.encode() + b"\0" + item_sha.encode() + b"\0")
        files += 1
        size += item_size
    return {"path": str(resolved), "kind": "directory", "sha256": digest.hexdigest(),
            "files": files, "bytes": size}


def _json_pointer(value: Any, pointer: str) -> Any:
    if pointer == "":
        return value
    if not pointer.startswith("/"):
        raise ValueError(f"JSON pointer must be empty or start with '/': {pointer}")
    current = value
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            try:
                current = current[int(token)]
            except (ValueError, IndexError) as exc:
                raise ValueError(f"JSON pointer does not resolve: {pointer}") from exc
        elif isinstance(current, dict) and token in current:
            current = current[token]
        else:
            raise ValueError(f"JSON pointer does not resolve: {pointer}")
    return current


def _validate_plan(value: Any, expected_stage: str) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"plan must use schema_version {SCHEMA_VERSION}")
    if value.get("stage") != expected_stage:
        raise ValueError(
            f"plan stage {value.get('stage')!r} does not match command {expected_stage!r}"
        )
    steps = value.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("plan steps must be a non-empty list")
    seen: set[str] = set()
    allowed = STAGE_COMMANDS[expected_stage]
    for step in steps:
        if not isinstance(step, dict):
            raise ValueError("every plan step must be an object")
        step_id = step.get("id")
        if not isinstance(step_id, str) or not STEP_ID.fullmatch(step_id) or step_id in seen:
            raise ValueError(f"invalid or duplicate step id: {step_id!r}")
        seen.add(step_id)
        command = step.get("command")
        if command not in allowed:
            raise ValueError(f"command {command!r} is not allowed in stage {expected_stage}")
        arguments = step.get("arguments")
        if not isinstance(arguments, list) or not all(isinstance(arg, str) for arg in arguments):
            raise ValueError(f"step {step_id} arguments must be a string list")
        outputs = step.get("outputs")
        if not isinstance(outputs, list) or not outputs or not all(
            isinstance(output, str) and output for output in outputs
        ):
            raise ValueError(f"step {step_id} must declare at least one output")
    metrics = value.get("metrics", [])
    if not isinstance(metrics, list):
        raise ValueError("plan metrics must be a list")
    metric_names: set[str] = set()
    declared_outputs = {
        output for step in steps for output in step["outputs"]
    }
    for metric in metrics:
        if not isinstance(metric, dict):
            raise ValueError("every metric must be an object")
        name = metric.get("name")
        if not isinstance(name, str) or not METRIC_NAME.fullmatch(name) or name in metric_names:
            raise ValueError(f"invalid or duplicate metric name: {name!r}")
        metric_names.add(name)
        if not isinstance(metric.get("path"), str) or not metric["path"]:
            raise ValueError(f"metric {name} requires a JSON path")
        if metric["path"] not in declared_outputs:
            raise ValueError(f"metric {name} path must be a declared step output")
        if not isinstance(metric.get("pointer"), str):
            raise ValueError(f"metric {name} requires a JSON pointer")
    return value


def _default_runner(command: str, arguments: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    entrypoint = Path(__file__).resolve().parents[2] / "run.py"
    return subprocess.run(
        [sys.executable, str(entrypoint), command, *arguments], cwd=cwd,
        text=True, capture_output=True, check=False,
    )


def _metrics(plan: dict[str, Any], cwd: Path) -> dict[str, int]:
    result: dict[str, int] = {}
    for metric in plan.get("metrics", []):
        source = (cwd / metric["path"]).resolve() if not Path(metric["path"]).is_absolute() else Path(metric["path"]).resolve()
        value = _json_pointer(json.loads(source.read_text()), metric["pointer"])
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"metric {metric['name']} is not a non-negative integer")
        result[metric["name"]] = value
    return result


def run(
    stage: str,
    plan_path: Path,
    output: Path,
    *,
    execute: bool = False,
    resume: bool = False,
    cwd: Path | None = None,
    command_runner: Callable[[str, list[str], Path], subprocess.CompletedProcess[str]] = _default_runner,
) -> dict[str, Any]:
    """Validate or execute one public stage and return its bound run manifest."""
    if stage not in STAGE_COMMANDS:
        raise ValueError(f"unknown public stage: {stage}")
    if resume and not execute:
        raise ValueError("--resume requires --execute")
    cwd = (cwd or Path.cwd()).resolve()
    plan_path = plan_path.resolve(strict=True)
    plan_bytes = plan_path.read_bytes()
    plan_sha = hashlib.sha256(plan_bytes).hexdigest()
    plan = _validate_plan(json.loads(plan_bytes), stage)
    output = output.resolve()
    assert_no_symlink_chain(output)
    manifest_path = output / "stage_manifest.json"
    assert_no_symlink_chain(manifest_path)
    prior: dict[str, Any] | None = None
    if manifest_path.exists():
        if not resume:
            raise ValueError(f"stage output already exists; use --resume: {manifest_path}")
        prior = json.loads(manifest_path.read_text())
        if prior.get("schema_version") != RUN_SCHEMA_VERSION or prior.get("stage") != stage:
            raise ValueError("existing stage manifest is incompatible")
        if prior.get("plan_sha256") != plan_sha:
            raise ValueError("cannot resume because the stage plan changed")
    elif resume:
        raise ValueError(f"cannot resume without a stage manifest: {manifest_path}")

    previous = {step["id"]: step for step in (prior or {}).get("steps", [])}
    records: list[dict[str, Any]] = []
    manifest: dict[str, Any] = {
        "schema_version": RUN_SCHEMA_VERSION,
        "stage": stage,
        "plan": str(plan_path),
        "plan_sha256": plan_sha,
        "mode": "execute" if execute else "plan",
        "status": "running" if execute else "planned",
        "started_at": (prior or {}).get("started_at", _now()),
        "updated_at": _now(),
        "steps": records,
        "counts": {},
    }
    write_json(manifest_path, manifest)
    if not execute:
        records.extend({
            "id": step["id"], "command": step["command"],
            "arguments": step["arguments"], "declared_outputs": step["outputs"],
            "status": "planned", "attempts": [],
        } for step in plan["steps"])
        manifest.update(updated_at=_now(), status="planned")
        write_json(manifest_path, manifest)
        return manifest

    for step in plan["steps"]:
        old = previous.get(step["id"])
        if old and old.get("status") == "complete":
            try:
                bindings = [_tree_binding(Path(path) if Path(path).is_absolute() else cwd / path)
                            for path in step["outputs"]]
            except (FileNotFoundError, ValueError):
                bindings = []
            if bindings and bindings == old.get("outputs"):
                records.append(old)
                continue
        attempts = list((old or {}).get("attempts", []))
        attempt_number = len(attempts) + 1
        started = _now()
        completed = command_runner(step["command"], list(step["arguments"]), cwd)
        log_root = output / "logs"
        stdout_path = log_root / f"{step['id']}.attempt_{attempt_number:03d}.stdout.log"
        stderr_path = log_root / f"{step['id']}.attempt_{attempt_number:03d}.stderr.log"
        write_bytes(stdout_path, (completed.stdout or "").encode())
        write_bytes(stderr_path, (completed.stderr or "").encode())
        attempt = {
            "started_at": started, "finished_at": _now(),
            "exit_code": completed.returncode,
            "stdout": {"path": str(stdout_path), "sha256": _sha256(stdout_path)},
            "stderr": {"path": str(stderr_path), "sha256": _sha256(stderr_path)},
        }
        attempts.append(attempt)
        record = {
            "id": step["id"], "command": step["command"],
            "arguments": step["arguments"], "declared_outputs": step["outputs"],
            "status": "failed" if completed.returncode else "binding_outputs",
            "attempts": attempts, "outputs": [],
        }
        records.append(record)
        if completed.returncode:
            manifest.update(status="failed", failed_step=step["id"],
                            failure_scope="orchestration",
                            semantic_rejection=False, updated_at=_now())
            write_json(manifest_path, manifest)
            return manifest
        try:
            record["outputs"] = [
                _tree_binding(Path(path) if Path(path).is_absolute() else cwd / path)
                for path in step["outputs"]
            ]
        except (FileNotFoundError, ValueError) as exc:
            record.update(status="failed", binding_error=str(exc))
            manifest.update(status="failed", failed_step=step["id"],
                            failure_scope="output_binding",
                            semantic_rejection=False, updated_at=_now())
            write_json(manifest_path, manifest)
            return manifest
        record["status"] = "complete"
        manifest["updated_at"] = _now()
        write_json(manifest_path, manifest)

    try:
        manifest["counts"] = _metrics(plan, cwd)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        manifest.update(status="failed", failed_step="metrics", metrics_error=str(exc),
                        failure_scope="metric_binding", semantic_rejection=False,
                        updated_at=_now())
        write_json(manifest_path, manifest)
        return manifest
    manifest.update(status="complete", finished_at=_now(), updated_at=_now())
    write_json(manifest_path, manifest)
    return manifest
