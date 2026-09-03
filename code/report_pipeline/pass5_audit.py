"""Audit Harbor Pass@5 trials without turning infrastructure failures into model failures."""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any, Iterable

from report_pipeline.trial_security import audit_trial_trace, trace_files


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _reward(result: dict[str, Any]) -> float | None:
    try:
        value = result["verifier_result"]["rewards"]["reward"]
    except (KeyError, TypeError):
        return None
    return float(value) if isinstance(value, (int, float)) else None


def _identity(result: dict[str, Any]) -> tuple[str | None, str | None]:
    agent = result.get("agent_info") or {}
    model = agent.get("model_info") or {}
    configured = (result.get("config") or {}).get("agent") or {}
    return agent.get("name") or configured.get("name"), (
        model.get("name") or model.get("model_name") or configured.get("model_name")
    )


_MODEL_HOSTS = {
    "kimi-code": ("ark-gateway.invalid",),
    "codex": ("api.openai.com", "auth.openai.com", "chatgpt.com"),
}


def _trial_record(trial: Path, *, instance_id: str, task_checksum: str,
                  agent: str, model: str) -> dict[str, Any]:
    result_path = trial / "result.json"
    base = {"trial": trial.name, "path": str(trial.resolve()),
            "result_path": str(result_path.resolve()),
            "trace_files": [str(path.resolve()) for path in trace_files(trial)]}
    if not result_path.is_file():
        return {**base, "classification": "pending", "valid": False,
                "reason": "missing_result"}
    try:
        result = _load(result_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        exception_path = trial / "exception.txt"
        exception_text = exception_path.read_text(errors="replace") if exception_path.is_file() else ""
        known = next((name for name in (
            "ApiRateLimitError", "NonZeroAgentExitCodeError", "AgentTimeoutError",
            "EnvironmentBuildError", "VerifierError") if name in exception_text), None)
        return {**base, "classification": "infrastructure_invalid", "valid": False,
                "reason": f"invalid_result:{type(exc).__name__}" + (f":{known}" if known else ""),
                "exception_path": str(exception_path.resolve()) if exception_path.is_file() else None}
    observed_agent, observed_model = _identity(result)
    common = {**base, "task_checksum": result.get("task_checksum"),
              "agent": observed_agent, "model": observed_model,
              "exception_info": result.get("exception_info"), "reward": _reward(result)}
    if result.get("task_checksum") != task_checksum:
        return {**common, "classification": "infrastructure_invalid", "valid": False,
                "reason": "task_checksum_mismatch"}
    task_name = str(result.get("task_name") or "")
    if instance_id not in task_name:
        return {**common, "classification": "infrastructure_invalid", "valid": False,
                "reason": "instance_id_mismatch"}
    if observed_agent != agent or observed_model != model:
        return {**common, "classification": "infrastructure_invalid", "valid": False,
                "reason": "agent_or_model_mismatch"}
    if result.get("exception_info") is not None:
        return {**common, "classification": "infrastructure_invalid", "valid": False,
                "reason": "harbor_exception"}
    reward = common["reward"]
    if reward not in (0.0, 1.0):
        return {**common, "classification": "infrastructure_invalid", "valid": False,
                "reason": "missing_or_nonbinary_reward"}
    trace_audit = audit_trial_trace(
        trial, agent=agent, allowed_network_hosts=_MODEL_HOSTS.get(agent, ()))
    if not trace_audit["valid"]:
        return {**common, **trace_audit}
    return {**common, "classification": "model_success" if reward == 1.0 else "model_failure",
            "valid": True, "reason": None}


def _trial_dirs(job_dirs: Iterable[Path]) -> list[Path]:
    found: list[Path] = []
    for job in job_dirs:
        root = job.resolve(strict=True)
        found.extend(path for path in sorted(root.iterdir())
                     if path.is_dir() and (path.name.startswith("task__") or
                                           (path / "result.json").is_file()))
    return found


def _render(value: dict[str, Any]) -> str:
    rows = []
    for trial in value["trials"]:
        trace = "<br>".join(html.escape(Path(p).name) for p in trial["trace_files"]) or "—"
        rows.append("<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            html.escape(trial["trial"]), html.escape(trial["classification"]),
            "—" if trial.get("reward") is None else trial["reward"],
            html.escape(trial.get("reason") or "—"), trace))
    return """<!doctype html><meta charset=utf-8><title>Pass@5 audit</title>
<style>body{{font:14px system-ui;margin:24px;color:#18202a}}table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #d9dee7;padding:6px 8px;text-align:left;vertical-align:top}}
.summary{{display:flex;gap:10px;flex-wrap:wrap}}.summary b{{background:#eef3ff;padding:8px 12px;border-radius:8px}}</style>
<h1>{instance}</h1><p>{agent} · {model} · <code>{checksum}</code></p>
<div class=summary><b>有效 {valid}/5</b><b>成功 {success}</b><b>模型失败 {failure}</b><b>基础设施无效 {infra}</b><b>答案泄漏无效 {leakage}</b><b>待补跑 {replacement}</b></div>
<table><thead><tr><th>trial</th><th>分类</th><th>reward</th><th>原因</th><th>trace</th></tr></thead><tbody>{rows}</tbody></table>""".format(
        instance=html.escape(value["instance_id"]), agent=html.escape(value["agent"]),
        model=html.escape(value["model"]), checksum=html.escape(value["task_checksum"]),
        valid=value["valid_trial_count"], success=value["success_count"],
        failure=value["model_failure_count"], infra=value["infrastructure_invalid_count"],
        leakage=value["answer_leakage_invalid_count"],
        replacement=value["replacement_trials_needed"], rows="".join(rows))


def run(job_dirs: list[Path], output: Path, *, instance_id: str,
        task_checksum: str, agent: str, model: str) -> dict[str, Any]:
    if len(task_checksum) != 64:
        raise ValueError("task checksum must be a 64-character SHA-256")
    trials = [_trial_record(path, instance_id=instance_id, task_checksum=task_checksum,
                            agent=agent, model=model) for path in _trial_dirs(job_dirs)]
    valid = [trial for trial in trials if trial["valid"]][:5]
    value = {
        "schema_version": "case-pass5-audit-v1", "instance_id": instance_id,
        "task_checksum": task_checksum, "agent": agent, "model": model,
        "status": "complete" if len(valid) == 5 else "needs_replacement_trials",
        "valid_trial_count": len(valid),
        "success_count": sum(trial["classification"] == "model_success" for trial in valid),
        "model_failure_count": sum(trial["classification"] == "model_failure" for trial in valid),
        "infrastructure_invalid_count": sum(
            trial["classification"] == "infrastructure_invalid" for trial in trials),
        "answer_leakage_invalid_count": sum(
            trial["classification"] == "invalid_answer_leakage" for trial in trials),
        "pending_count": sum(trial["classification"] == "pending" for trial in trials),
        "replacement_trials_needed": max(0, 5 - len(valid)),
        "selected_valid_trials": [trial["trial"] for trial in valid], "trials": trials,
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "pass5_audit.json").write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    (output / "pass5_audit.html").write_text(_render(value))
    return value
