"""Fail-closed aggregate runtime audit for the active provisional IID cases."""

from __future__ import annotations

import html
import json
import os
from pathlib import Path
from typing import Any, Iterable

from report_pipeline.atomic import write_json


CASE_IDS = (
    "bpmn-io__bpmn-js-2396",
    "googlechrome__lighthouse-16403",
    "automattic__wp-calypso-100957",
    "automattic__wp-calypso-99049",
    "mermaid-js__mermaid-7711",
    "excalidraw__excalidraw-9002",
)
PROVIDERS = ("kimi-k3", "codex-luna-max")
EXPECTED_REWARDS = {
    "gold_oracle": 1.0,
    "empty_patch": 0.0,
    "nop": 0.0,
    "empty_reply": 0.0,
}


def _load(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _portable(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _reward(result: dict[str, Any]) -> float | None:
    value: Any = result
    for key in ("verifier_result", "rewards", "reward"):
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return float(value) if isinstance(value, (int, float)) else None


def _control_name(name: str) -> str | None:
    lowered = name.lower()
    if "gold_oracle" in lowered:
        return "gold_oracle"
    if "empty_patch" in lowered:
        return "empty_patch"
    if "empty_no_reply" in lowered or "empty_reply" in lowered:
        return "empty_reply"
    if "nop" in lowered:
        return "nop"
    return None


def _trial_result(run_dir: Path) -> tuple[dict[str, Any] | None, Path | None]:
    candidates = []
    for path in run_dir.glob("*/result.json"):
        value = _load(path)
        if value and value.get("task_checksum"):
            candidates.append((path, value))
    if not candidates:
        return None, None
    path, value = sorted(candidates, key=lambda item: item[0].stat().st_mtime)[-1]
    return value, path


def _digest(run_dir: Path) -> str | None:
    lock = _load(run_dir / "lock.json")
    try:
        value = lock["trials"][0]["task"]["digest"]  # type: ignore[index]
    except (KeyError, IndexError, TypeError):
        return None
    return value if isinstance(value, str) and value.startswith("sha256:") else None


def _controls(case: Path, workspace: Path) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    outputs = case / "outputs"
    roots = [path for path in outputs.iterdir() if path.is_dir() and "controls" in path.name
             and not path.name.startswith("00_")] if outputs.is_dir() else []
    for root in roots:
        for run_dir in root.iterdir():
            if not run_dir.is_dir():
                continue
            name = _control_name(run_dir.name)
            if name is None:
                continue
            result, result_path = _trial_result(run_dir)
            if result is None or result_path is None:
                continue
            checksum = result.get("task_checksum")
            digest = _digest(run_dir)
            if not isinstance(checksum, str) or len(checksum) != 64 or digest is None:
                continue
            records.append({
                "name": name,
                "reward": _reward(result),
                "exception": result.get("exception_info"),
                "task_checksum": checksum,
                "trial_lock_digest": digest,
                "evidence": _portable(result_path, workspace),
                "mtime": result_path.stat().st_mtime,
            })

    groups: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    for record in records:
        key = (record["task_checksum"], record["trial_lock_digest"])
        previous = groups.setdefault(key, {}).get(record["name"])
        if previous is None or record["mtime"] > previous["mtime"]:
            groups[key][record["name"]] = record
    complete = [(key, rows) for key, rows in groups.items()
                if set(rows) == set(EXPECTED_REWARDS)]
    if not complete:
        # Several early case envelopes retain the complete, checksum-bound control
        # evidence in the technical-readiness record rather than one-level Harbor
        # run directories.  Accept only the exact four-value contract.
        readiness_files = sorted((case / "outputs" / "06_freeze" / "kimi-k3").glob(
            "*technical_readiness*.json"))
        for path in reversed(readiness_files):
            value = _load(path) or {}
            binding = value.get("task_binding") or {}
            observed = value.get("controls") or {}
            checksum = binding.get("harbor_reported_checksum")
            digest = binding.get("trial_lock_digest")
            exceptions = observed.get("exceptions")
            observed_values = {
                "gold_oracle": observed.get("gold_oracle"),
                "empty_patch": observed.get("empty_patch"),
                "nop": observed.get("nop"),
                "empty_reply": observed.get("empty_no_reply", observed.get("empty_reply")),
            }
            if (not isinstance(checksum, str) or len(checksum) != 64
                    or not isinstance(digest, str) or not digest.startswith("sha256:")):
                continue
            checks = {}
            for name, expected in EXPECTED_REWARDS.items():
                passed = observed_values[name] == expected and exceptions == 0
                checks[name] = {"name": name, "reward": observed_values[name],
                                "expected_reward": expected, "exception_count": exceptions,
                                "evidence": _portable(path, workspace),
                                "status": "passed" if passed else "failed"}
            passed = all(item["status"] == "passed" for item in checks.values())
            return {"status": "passed" if passed else "failed",
                    "reason": None if passed else "reward_or_exception_mismatch",
                    "task_checksum": checksum, "trial_lock_digest": digest, "checks": checks}
        return {"status": "missing", "reason": "no_complete_checksum_bound_control_group",
                "task_checksum": None, "trial_lock_digest": None, "checks": {}}
    key, rows = max(complete, key=lambda item: min(row["mtime"] for row in item[1].values()))
    checks = {}
    for name, expected in EXPECTED_REWARDS.items():
        row = rows[name]
        passed = row["reward"] == expected and row["exception"] is None
        checks[name] = {k: v for k, v in row.items() if k != "mtime"} | {
            "expected_reward": expected, "status": "passed" if passed else "failed"
        }
    passed = all(value["status"] == "passed" for value in checks.values())
    return {
        "status": "passed" if passed else "failed",
        "reason": None if passed else "reward_or_exception_mismatch",
        "task_checksum": key[0],
        "trial_lock_digest": key[1],
        "checks": checks,
    }


def _all(values: Any, wanted: Any) -> bool:
    return isinstance(values, list) and len(values) >= 3 and all(value == wanted for value in values)


def _measurement_verdict(value: dict[str, Any]) -> tuple[bool, bool] | None:
    repetitions = value.get("repetitions")
    if isinstance(repetitions, list) and len(repetitions) >= 3:
        base = [row.get("base", {}).get("reward") for row in repetitions if isinstance(row, dict)]
        gold = [row.get("gold", {}).get("reward") for row in repetitions if isinstance(row, dict)]
        return _all(base, 0) or _all(base, 0.0), _all(gold, 1) or _all(gold, 1.0)
    if isinstance(repetitions, dict):
        base = [row.get("reward") for row in repetitions.get("base", []) if isinstance(row, dict)]
        gold = [row.get("reward") for row in repetitions.get("gold", []) if isinstance(row, dict)]
        return _all(base, 0.0), _all(gold, 1.0)
    runs = value.get("runs")
    if isinstance(runs, dict):
        base_rows, gold_rows = runs.get("base"), runs.get("gold")
        if isinstance(base_rows, list) and isinstance(gold_rows, list):
            base = len(base_rows) >= 3 and all(
                isinstance(row, dict) and row.get("fail", 0) > 0 and row.get("error", 0) == 0
                and row.get("skip", 0) == 0 for row in base_rows)
            gold = len(gold_rows) >= 3 and all(
                isinstance(row, dict) and row.get("fail", 0) == 0 and row.get("error", 0) == 0
                and row.get("skip", 0) == 0 for row in gold_rows)
            return base, gold
    base, gold = value.get("base"), value.get("gold")
    if isinstance(base, dict) and isinstance(gold, dict):
        return _all(base.get("reward"), 0.0), _all(gold.get("reward"), 1.0)
    transitions = value.get("test_transitions")
    if isinstance(transitions, list) and transitions and value.get("status") == "measured":
        base_ok = any(row.get("observed_type") == "F2P" for row in transitions if isinstance(row, dict))
        base_ok = base_ok and all(_all(row.get("baseline"), "fail") if row.get("observed_type") == "F2P"
                                  else _all(row.get("baseline"), "pass")
                                  for row in transitions if isinstance(row, dict))
        gold_ok = all(_all(row.get("reference"), "pass") for row in transitions if isinstance(row, dict))
        return base_ok, gold_ok
    if value.get("stable") is True and (value.get("FAIL_TO_PASS") or value.get("transitions")):
        return True, True
    return None


def _measurement(case: Path, workspace: Path) -> dict[str, Any]:
    candidates = []
    for path in list((case / "outputs").rglob("*.json")) + list((case / "meta").rglob("*.json")):
        if any(part.startswith("00_") or part in {"05_controls", "06_freeze", "07_pass5"}
               for part in path.parts):
            continue
        lowered = path.name.lower()
        if not any(token in lowered for token in ("stability_summary", "measurement_summary",
                                                   "transition_summary", "transition_audit",
                                                   "base_gold_summary", "exact_boundary_summary")):
            continue
        value = _load(path)
        verdict = _measurement_verdict(value or {})
        if verdict is not None:
            candidates.append((path, verdict))
    if not candidates:
        return {"base": "missing", "gold": "missing", "evidence": None}
    path, (base, gold) = max(candidates, key=lambda item: item[0].stat().st_mtime)
    return {"base": "passed" if base else "failed", "gold": "passed" if gold else "failed",
            "evidence": _portable(path, workspace)}


def _readiness(case: Path, provider: str, workspace: Path) -> dict[str, Any]:
    root = case / "outputs" / "06_freeze" / provider
    readiness = sorted(root.glob("*technical_readiness*.json"))
    frozen = sorted(root.glob("*frozen*.json"))
    files = readiness + [path for path in frozen if path not in readiness]
    rows = []
    for path in files:
        value = _load(path)
        if value:
            rows.append({"path": _portable(path, workspace),
                         "kind": "technical_readiness" if "technical_readiness" in path.name else "frozen_config",
                         "status": value.get("status"),
                         "formal_admission": value.get("formal_admission"),
                         "human_gates": value.get("human_gates")})
    preflights = []
    for path in sorted(root.glob("*preflight*.json")):
        value = _load(path)
        if value:
            checks = value.get("checks")
            checks_passed = (isinstance(checks, dict) and bool(checks)
                             and all(item is True for item in checks.values()))
            preflights.append({"path": _portable(path, workspace),
                               "status": value.get("status") or value.get("result") or "unknown",
                               "checks_passed": checks_passed})
    status = next((row["status"] for row in reversed(rows)
                   if row["kind"] == "technical_readiness"), None)
    return {"status": status or "missing", "records": rows,
            "preflight_records": preflights}


def _audits(case: Path, provider: str, workspace: Path) -> tuple[dict[str, Any], set[str]]:
    root = case / "outputs" / "07_pass5" / provider
    audit_files = sorted(root.rglob("pass5_audit.json")) if root.is_dir() else []
    audited_paths: set[str] = set()
    trials: dict[str, dict[str, Any]] = {}
    records = []
    for path in audit_files:
        value = _load(path)
        if not value:
            continue
        records.append({"path": _portable(path, workspace), "status": value.get("status")})
        for trial in value.get("trials", []):
            if not isinstance(trial, dict):
                continue
            trial_path = trial.get("path")
            if not isinstance(trial_path, str):
                continue
            canonical = str(Path(trial_path).resolve())
            audited_paths.add(canonical)
            trials[canonical] = trial
    values = list(trials.values())
    valid = sum(trial.get("valid") is True for trial in values)
    infra = sum(trial.get("classification") == "infrastructure_invalid" for trial in values)
    traces = []
    for trial in values:
        for raw in trial.get("trace_files", []):
            path = Path(raw)
            if path.is_file():
                traces.append(_portable(path, workspace))
    complete = any(record["status"] == "complete" for record in records) and valid >= 5
    return ({"status": "complete" if complete else "incomplete", "valid": valid,
             "infrastructure_invalid": infra, "pending": 0, "running": 0,
             "audit_records": records, "trace_files": sorted(set(traces))}, audited_paths)


def _job_pending(case: Path, provider: str, audited: set[str]) -> tuple[int, int]:
    jobs = case / "outputs" / "07_pass5" / provider / "jobs"
    pending = running = 0
    if not jobs.is_dir():
        return pending, running
    for job in jobs.iterdir():
        if not job.is_dir():
            continue
        config = _load(job / "config.json") or {}
        expected = config.get("n_attempts") if isinstance(config.get("n_attempts"), int) else 0
        task_dirs = [path for path in job.iterdir() if path.is_dir() and path.name.startswith("task__")]
        result = _load(job / "result.json") or {}
        stats = result.get("stats") if isinstance(result.get("stats"), dict) else None
        invalidated = (job / "00_invalidation.json").is_file()
        for trial in task_dirs:
            if str(trial.resolve()) in audited:
                continue
            if (trial / "result.json").is_file():
                pending += 1  # Complete on disk but not accepted until audit-case-pass5 binds it.
        if invalidated:
            # A bounded recovery decision has explicitly closed this job. Any
            # scheduler counters in its last snapshot are stale, not live work.
            continue
        if stats is not None:
            # Harbor's live job snapshot is authoritative for work that has not
            # produced a trial result yet.  Inferring from n_attempts would make
            # an already-finished, partially scheduled historical job look live.
            pending += max(0, int(stats.get("n_pending_trials") or 0))
            running += max(0, int(stats.get("n_running_trials") or 0))
        else:
            running += sum(not (trial / "result.json").is_file() for trial in task_dirs)
            pending += max(0, expected - len(task_dirs))
    return pending, running


def _provider(case: Path, provider: str, workspace: Path) -> dict[str, Any]:
    audit, audited = _audits(case, provider, workspace)
    pending, running = _job_pending(case, provider, audited)
    audit["pending"] += pending
    audit["running"] += running
    audit["freeze"] = _readiness(case, provider, workspace)
    return audit


def _case(case: Path, workspace: Path) -> dict[str, Any]:
    if not case.is_dir():
        return {"instance_id": case.name, "status": "missing", "formal_admission": False,
                "human_review": "deferred", "measurement": {"base": "missing", "gold": "missing"},
                "controls": {"status": "missing", "checks": {}},
                "providers": {name: {"status": "incomplete", "valid": 0,
                                      "infrastructure_invalid": 0, "pending": 0, "running": 0,
                                      "trace_files": []} for name in PROVIDERS}}
    controls = _controls(case, workspace)
    measurement = _measurement(case, workspace)
    providers = {provider: _provider(case, provider, workspace) for provider in PROVIDERS}
    runtime_complete = (measurement["base"] == measurement["gold"] == "passed"
                        and controls["status"] == "passed"
                        and all(value["status"] == "complete" for value in providers.values()))
    return {
        "instance_id": case.name,
        "status": "runtime_complete" if runtime_complete else "incomplete",
        "task_checksum": controls.get("task_checksum"),
        "trial_lock_digest": controls.get("trial_lock_digest"),
        "measurement": measurement,
        "controls": controls,
        "providers": providers,
        "human_review": "deferred",
        "formal_admission": False,
    }


def _link(path: str | None, output: Path, workspace: Path, label: str) -> str:
    if not path:
        return "—"
    target = workspace / path if not Path(path).is_absolute() else Path(path)
    relative = os.path.relpath(target.resolve(), output.resolve())
    return f'<a href="{html.escape(relative)}">{html.escape(label)}</a>'


def _render(value: dict[str, Any], output: Path, workspace: Path) -> str:
    rows = []
    for case in value["cases"]:
        controls = case["controls"]
        control_cells = []
        for name in EXPECTED_REWARDS:
            status = controls.get("checks", {}).get(name, {}).get("status", "missing")
            control_cells.append(f"{name}: {status}")
        providers = []
        for name in PROVIDERS:
            record = case["providers"][name]
            freeze = record.get("freeze", {})
            freeze_links = " ".join(
                _link(item["path"], output, workspace, "freeze")
                for item in freeze.get("records", [])
            )
            preflight_links = " ".join(
                _link(item["path"], output, workspace, "preflight")
                for item in freeze.get("preflight_records", [])
            )
            trace_links = " ".join(_link(path, output, workspace, Path(path).name)
                                   for path in record.get("trace_files", [])[:5]) or "—"
            audit_links = " ".join(_link(item["path"], output, workspace, "audit")
                                   for item in record.get("audit_records", [])) or "—"
            providers.append(
                f"<div><b>{html.escape(name)}</b> "
                f"valid={record['valid']} infra={record['infrastructure_invalid']} "
                f"pending={record['pending']} running={record['running']}<br>"
                f"freeze={html.escape(str(freeze.get('status', 'missing')))} "
                f"{freeze_links} {preflight_links}<br>{audit_links} · traces: {trace_links}</div>")
        checksum = case.get("task_checksum") or "missing"
        digest = case.get("trial_lock_digest") or "missing"
        measurement = case["measurement"]
        evidence = _link(measurement.get("evidence"), output, workspace, "measurement")
        rows.append(
            f"<tr class='{html.escape(case['status'])}'><td><b>{html.escape(case['instance_id'])}</b>"
            f"<br><small>human_review_deferred · formal_admission=false</small></td>"
            f"<td>Task.checksum<br><code>{html.escape(checksum)}</code><br>"
            f"TrialLock digest<br><code>{html.escape(digest)}</code></td>"
            f"<td>Base: {measurement['base']}<br>Gold: {measurement['gold']}<br>{evidence}</td>"
            f"<td>{'<br>'.join(html.escape(cell) for cell in control_cells)}</td>"
            f"<td>{''.join(providers)}</td></tr>")
    document = """<!doctype html><html lang=zh-CN><meta charset=utf-8>
<title>七题运行审计</title><style>
body{font:13px system-ui;margin:18px;color:#172033}h1{font-size:20px;margin:0 0 6px}
.summary{display:flex;gap:8px;margin:10px 0}.summary b{padding:7px 10px;background:#eef3ff;border-radius:7px}
table{border-collapse:collapse;width:100%;table-layout:fixed}th,td{border:1px solid #d8dee9;padding:7px;vertical-align:top;word-break:break-word}
th{background:#f5f7fa;text-align:left}.runtime_complete{background:#f1fbf4}.incomplete{background:#fffaf0}
code{font-size:11px}small{color:#687386}td div+div{margin-top:7px;padding-top:7px;border-top:1px solid #e3e6ec}a{color:#2459c4}
</style><h1>七题 Harbor 运行审计</h1>
<p>Fail-closed 快照：Task.checksum 与 TrialLock digest 分列；未审计或正在写入的 trial 不计有效结果。</p>
<div class=summary><b>运行闭环 __COMPLETE__/7</b><b>正式准入 0/7</b><b>人工审核 deferred 7/7</b></div>
<table><thead><tr><th style='width:17%'>题目</th><th style='width:22%'>任务绑定</th><th style='width:12%'>Base / Gold</th><th style='width:18%'>四控制</th><th>Pass@5</th></tr></thead>
<tbody>__ROWS__</tbody></table></html>"""
    return document.replace("__COMPLETE__", str(value["summary"]["runtime_complete_count"])).replace(
        "__ROWS__", "".join(rows))


def run(cases_root: Path, output: Path) -> dict[str, Any]:
    cases_root = cases_root.resolve(strict=True)
    workspace = Path.cwd().resolve()
    cases = [_case(cases_root / instance_id, workspace) for instance_id in CASE_IDS]
    complete = sum(case["status"] == "runtime_complete" for case in cases)
    value = {
        "schema_version": "seven-case-runtime-audit-v1",
        "status": "complete" if complete == len(CASE_IDS) else "incomplete",
        "policy": {
            "fail_closed": True,
            "raw_unreviewed_results_are_valid": False,
            "human_review": "deferred",
            "formal_admission": False,
            "checksum_semantics": {
                "task_checksum": "Harbor result.json task_checksum",
                "trial_lock_digest": "Harbor TrialLock task.digest",
            },
        },
        "summary": {"case_count": len(CASE_IDS), "runtime_complete_count": complete,
                    "formal_admission_count": 0},
        "cases": cases,
    }
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "seven_case_runtime.json", value)
    (output / "seven_case_runtime.html").write_text(_render(value, output, workspace))
    return value
