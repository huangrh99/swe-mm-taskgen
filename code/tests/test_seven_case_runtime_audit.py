import json
from pathlib import Path

from report_pipeline import cli
from report_pipeline.seven_case_runtime_audit import CASE_IDS, run


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def _control(case: Path, number: int, name: str, reward: float, checksum: str, digest: str) -> None:
    root = case / "outputs" / "05_controls" / f"{number:02}_{name}"
    _write(root / "task__abc" / "result.json", {
        "task_checksum": checksum, "exception_info": None,
        "verifier_result": {"rewards": {"reward": reward}},
    })
    _write(root / "lock.json", {"trials": [{"task": {"digest": digest}}]})


def _case(root: Path, instance_id: str) -> None:
    case = root / instance_id
    _write(case / "outputs" / "04_measurements" / "04_measurement_summary.json", {
        "status": "stable", "base": {"reward": [0.0, 0.0, 0.0]},
        "gold": {"reward": [1.0, 1.0, 1.0]},
    })
    checksum, digest = "a" * 64, "sha256:" + "b" * 64
    for number, (name, reward) in enumerate((
        ("gold_oracle", 1.0), ("empty_patch", 0.0),
        ("nop", 0.0), ("empty_reply", 0.0)), 1):
        _control(case, number, name, reward, checksum, digest)


def test_missing_pass5_is_fail_closed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    cases = tmp_path / "report" / "cases"
    for instance_id in CASE_IDS:
        _case(cases, instance_id)
    result = run(cases, tmp_path / "report" / "evidence" / "runtime")
    assert result["status"] == "incomplete"
    assert result["summary"]["runtime_complete_count"] == 0
    assert result["cases"][0]["measurement"] == {
        "base": "passed", "gold": "passed",
        "evidence": f"report/cases/{CASE_IDS[0]}/outputs/04_measurements/04_measurement_summary.json",
    }
    assert result["cases"][0]["controls"]["status"] == "passed"
    assert result["cases"][0]["formal_admission"] is False
    assert (tmp_path / "report/evidence/runtime/seven_case_runtime.html").is_file()


def test_controls_do_not_mix_checksums(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    case = tmp_path / "report" / "cases" / CASE_IDS[0]
    _write(case / "outputs/04_measurements/summary_measurement_summary.json", {
        "base": {"reward": [0.0] * 3}, "gold": {"reward": [1.0] * 3}})
    for number, (name, reward) in enumerate((
        ("gold_oracle", 1.0), ("empty_patch", 0.0),
        ("nop", 0.0), ("empty_reply", 0.0)), 1):
        checksum = ("a" if name != "nop" else "c") * 64
        _control(case, number, name, reward, checksum, "sha256:" + "b" * 64)
    result = run(tmp_path / "report/cases", tmp_path / "out")
    assert result["cases"][0]["controls"]["status"] == "missing"
    assert result["cases"][0]["task_checksum"] is None


def test_unaudited_result_is_pending_not_valid(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    cases = tmp_path / "report/cases"
    _case(cases, CASE_IDS[0])
    job = cases / CASE_IDS[0] / "outputs/07_pass5/kimi-k3/jobs/job"
    _write(job / "config.json", {"n_attempts": 2})
    _write(job / "task__done/result.json", {"verifier_result": {"rewards": {"reward": 1.0}}})
    result = run(cases, tmp_path / "out")
    provider = result["cases"][0]["providers"]["kimi-k3"]
    assert provider["valid"] == 0
    assert provider["pending"] == 2


def test_finished_partial_job_does_not_invent_pending_trials(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    cases = tmp_path / "report/cases"
    _case(cases, CASE_IDS[0])
    job = cases / CASE_IDS[0] / "outputs/07_pass5/kimi-k3/jobs/job"
    _write(job / "config.json", {"n_attempts": 5})
    _write(job / "result.json", {"stats": {
        "n_completed_trials": 1, "n_pending_trials": 0, "n_running_trials": 0,
    }})
    _write(job / "task__done/result.json", {"verifier_result": {"rewards": {"reward": 1.0}}})
    result = run(cases, tmp_path / "out")
    provider = result["cases"][0]["providers"]["kimi-k3"]
    assert provider["pending"] == 1
    assert provider["running"] == 0


def test_invalidated_job_ignores_stale_scheduler_counters(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    cases = tmp_path / "report/cases"
    _case(cases, CASE_IDS[0])
    job = cases / CASE_IDS[0] / "outputs/07_pass5/kimi-k3/jobs/job"
    _write(job / "config.json", {"n_attempts": 5})
    _write(job / "result.json", {"stats": {
        "n_completed_trials": 3, "n_pending_trials": 2, "n_running_trials": 0,
    }})
    _write(job / "00_invalidation.json", {"status": "infrastructure_invalid"})
    result = run(cases, tmp_path / "out")
    provider = result["cases"][0]["providers"]["kimi-k3"]
    assert provider["pending"] == 0
    assert provider["running"] == 0


def test_public_cli_writes_fail_closed_snapshot(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    cases = tmp_path / "report/cases"
    for instance_id in CASE_IDS:
        _case(cases, instance_id)
    output = tmp_path / "report/evidence/runtime"
    code = cli.main(["audit-seven-case-runtime", "--cases-root", str(cases),
                     "--output", str(output)])
    assert code == 2
    assert (output / "seven_case_runtime.json").is_file()
