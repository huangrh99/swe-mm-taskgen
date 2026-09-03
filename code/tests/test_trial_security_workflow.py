from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from report_pipeline.paths import TMP_ROOT, WORKSPACE_ROOT
from report_pipeline.workflow import (_classify_harbor_trial,
                                      OFFICIAL_K3_TOOL_POLICY,
                                      _sha256,
                                      _validate_trial_runtime_binding,
                                      OFFICIAL_CODEX_DISABLED_FEATURES)


class TrialSecurityWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="trial-security-", dir=TMP_ROOT))
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.task_path = "cases/owner__repo-7"
        self.frozen = {"task": {"path": self.task_path},
                       "harbor_task_checksum": "b" * 64}

    def _binding(self, path: Path) -> dict:
        return {"path": path.resolve().relative_to(WORKSPACE_ROOT.resolve()).as_posix(),
                "sha256": _sha256(path)}

    def _kimi(self) -> tuple[dict, dict]:
        agent = {
            "name": "kimi-code", "model_name": "ep-20260817150115-9fx8h",
            "n_concurrent": 1, "override_timeout_sec": 7200,
            "override_setup_timeout_sec": 1800,
            "extra_allowed_hosts": ["ark-cn-beijing.bytedance.net"],
            "kwargs": {"version": "0.29.0"},
            "env": {
                "KIMI_MODEL_API_KEY": "${ARK_API_KEY}",
                "KIMI_MODEL_BASE_URL": "https://ark-cn-beijing.bytedance.net/api/v3",
                "KIMI_MODEL_MAX_CONTEXT_SIZE": "1048576",
                "KIMI_MODEL_MAX_COMPLETION_TOKENS": "+131072",
                "KIMI_MODEL_CAPABILITIES": "image_in,thinking",
                "KIMI_MODEL_THINKING_EFFORT": "max",
                "KIMI_LOOP_MAX_STEPS_PER_TURN": "0",
            },
            "mcp_servers": [], "skills": [],
        }
        source = {
            "n_concurrent_trials": 1, "n_attempts": 1,
            "retry": {"max_retries": 0}, "load_trajectory": None, "resume": None,
            "environment": {"type": "docker", "delete": True,
                            "kwargs": {"keep_containers": False},
                            "extra_allowed_hosts": []},
            "agents": [agent], "tasks": [{"path": self.task_path}],
        }
        path = self.root / "frozen-job.json"
        path.write_text(json.dumps(source) + "\n")
        config = {
            "agent": "kimi-code", "agent_version": "0.29.0",
            "model_id": "ep-20260817150115-9fx8h",
            "expected_test_ids": ["f", "p"],
            "network_policy": {"environment_hosts": [],
                               "agent_hosts": ["ark-cn-beijing.bytedance.net"]},
            "harbor_job_config": self._binding(path),
        }
        return config, source

    def _codex(self) -> tuple[dict, dict]:
        agent = {
            "name": "codex", "model_name": "gpt-5.6-luna", "n_concurrent": 1,
            "override_timeout_sec": 7200, "override_setup_timeout_sec": 1800,
            "extra_allowed_hosts": ["api.openai.com", "auth.openai.com", "chatgpt.com"],
            "kwargs": {"version": "0.148.0", "reasoning_effort": "max",
                       "web_search": "disabled", "config": {
                           "cli_auth_credentials_store": "file", "mcp_servers": {},
                           "features": dict(OFFICIAL_CODEX_DISABLED_FEATURES)}},
            "env": {"CODEX_FORCE_AUTH_JSON": "YES"}, "mcp_servers": [], "skills": [],
        }
        source = {
            "n_concurrent_trials": 1, "n_attempts": 1,
            "retry": {"max_retries": 0}, "load_trajectory": None, "resume": None,
            "environment": {"type": "docker", "delete": True,
                            "kwargs": {"keep_containers": False},
                            "extra_docker_compose": [
                                "reproducibility/11_codex_auth_read_search.compose.yaml"],
                            "extra_allowed_hosts": []},
            "agents": [agent], "tasks": [{"path": self.task_path}],
        }
        path = self.root / "frozen-codex-job.json"
        path.write_text(json.dumps(source) + "\n")
        config = {
            "agent": "codex", "agent_version": "0.148.0", "model_id": "gpt-5.6-luna",
            "expected_test_ids": ["f", "p"],
            "network_policy": {"environment_hosts": [], "agent_hosts": [
                "api.openai.com", "auth.openai.com", "chatgpt.com"]},
            "harbor_job_config": self._binding(path),
        }
        return config, source

    def _trial(self, config: dict, source: dict) -> tuple[Path, dict]:
        job = self.root / "job"; trial = job / "task__trial"
        (trial / "agent").mkdir(parents=True)
        (trial / "verifier").mkdir()
        resolved_job = dict(source)
        resolved_job.update(job_name="job", jobs_dir=str(job.parent))
        (job / "config.json").write_text(json.dumps(resolved_job) + "\n")
        agent = json.loads(json.dumps(source["agents"][0]))
        agent.update(load_trajectory=None, resume_trajectory=False)
        environment = json.loads(json.dumps(source["environment"]))
        environment["mounts"] = None
        result = {
            "id": "trial-id", "trial_name": trial.name,
            "started_at": "2026-09-03T00:00:00Z", "finished_at": "2026-09-03T00:01:00Z",
            "task_checksum": self.frozen["harbor_task_checksum"],
            "config": {"task": {"path": self.task_path}, "trial_name": trial.name,
                       "job_id": "job-id", "source_trial": None, "user_agent": None,
                       "extra_instruction_paths": [], "extra_instructions": [],
                       "agent": agent, "environment": environment},
            "agent_info": {"name": config["agent"], "version": config["agent_version"],
                           "model_info": {"name": config["model_id"]}},
            "exception_info": None, "verifier_result": {"rewards": {"reward": 1}},
        }
        (trial / "result.json").write_text(json.dumps(result) + "\n")
        (trial / "verifier/test_results.json").write_text(json.dumps({
            "reward": 1, "results": [
                {"test_id": "f", "class": "F2P", "status": "pass"},
                {"test_id": "p", "class": "P2P", "status": "pass"}],
            "summary": {"pass": 2, "fail": 0, "skip": 0, "missing": 0, "error": 0},
            "contract_errors": [],
        }) + "\n")
        return trial, result

    def test_kimi_trial_leakage_is_rejected_before_valid_and_binds_configs(self) -> None:
        config, source = self._kimi(); trial, result = self._trial(config, source)
        (trial / "agent/wire.jsonl").write_text(json.dumps({
            "type": "context.append_loop_event", "event": {"type": "tool.call",
            "name": "Bash", "args": {"command": "git show HEAD~1:src/fix.js"}},
        }) + "\n")
        with patch("report_pipeline.workflow._task_test_ids", return_value=(["f"], ["p"])):
            classified = _classify_harbor_trial(
                trial / "result.json", ["f", "p"], self.frozen, config)
        self.assertEqual("invalid_answer_leakage", classified["classification"])
        self.assertEqual(config["harbor_job_config"], classified["frozen_job_config"])
        self.assertEqual(_sha256(trial.parent / "config.json"),
                         classified["resolved_job_config"]["sha256"])

        result["config"]["environment"]["extra_allowed_hosts"] = ["github.com"]
        (trial / "result.json").write_text(json.dumps(result) + "\n")
        with self.assertRaisesRegex(ValueError, "result_resolved_config_frozen_mismatch"):
            _validate_trial_runtime_binding(
                trial / "result.json", result, self.frozen, config)

    def test_frozen_config_requires_provider_runtime_and_truthful_kimi_tool_status(self) -> None:
        schema = json.loads((WORKSPACE_ROOT /
            "schemas/frozen_pass5_config_v1.schema.json").read_text())
        self.assertTrue({"provider_profile", "agent_runtime"}.issubset(
            set(schema["required"])))
        self.assertEqual("registered_but_runtime_denied_and_trace_rejected",
                         OFFICIAL_K3_TOOL_POLICY["hosted_tools"])

    def test_codex_incomplete_kwargs_and_missing_raw_rollout_fail_closed(self) -> None:
        config, source = self._codex(); trial, result = self._trial(config, source)
        (trial / "agent/trajectory.json").write_text(json.dumps({
            "schema_version": "ATIF-v1.7", "steps": [{
                "step_id": 1, "source": "agent", "message": "done", "tool_calls": []}]}) + "\n")
        with patch("report_pipeline.workflow._task_test_ids", return_value=(["f"], ["p"])):
            classified = _classify_harbor_trial(
                trial / "result.json", ["f", "p"], self.frozen, config)
        self.assertEqual("infrastructure_invalid", classified["classification"])
        self.assertEqual("missing_codex_raw_rollout", classified["reason"])

        result["config"]["agent"]["kwargs"].pop("web_search")
        (trial / "result.json").write_text(json.dumps(result) + "\n")
        with self.assertRaisesRegex(ValueError, "result_resolved_config_frozen_mismatch"):
            _validate_trial_runtime_binding(
                trial / "result.json", result, self.frozen, config)


if __name__ == "__main__":
    unittest.main()
