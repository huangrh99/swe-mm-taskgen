import json
import fcntl
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import jsonschema

from report_pipeline.paths import REPORT_ROOT, RUNS_ROOT, TMP_ROOT, WORKSPACE_ROOT
from report_pipeline.harbor_negative_controls import CONTROL_SPECS
from report_pipeline.workflow import (OFFICIAL_CODEX_COMPOSE_OVERLAY,
                                      OFFICIAL_CODEX_DISABLED_FEATURES,
                                      OFFICIAL_CODEX_TOOL_POLICY,
                                      OFFICIAL_K3_TOOL_POLICY,
                                      REQUIRED_FREEZE_CODE, REQUIRED_FREEZE_SCHEMAS,
                                      _audit_pass5_summary, _classify_harbor_attempt, _classify_harbor_job,
                                      _pass5_output_allowed,
                                      _require_formal_freeze_ready, _sha256, _task_inventory,
                                      _validate_formal_dossier,
                                      _validate_promotion_commit,
                                      _validate_task_tree,
                                      _require_formal_pass5_config,
                                      _require_official_k3_config,
                                      _replay_promotion_evidence,
                                      _validate_formal_job_config,
                                      _validate_official_k3_job_config,
                                      _validate_run_authorization, promote, run_pass5)


class WorkflowTests(unittest.TestCase):
    def test_real_pass5_output_is_case_local_and_agent_scoped(self):
        case_output = (REPORT_ROOT / "cases/owner__repo-7/outputs/07_pass5")
        self.assertTrue(_pass5_output_allowed(
            "owner__repo-7", case_output / "kimi-k3", simulation=False))
        self.assertTrue(_pass5_output_allowed(
            "owner__repo-7", case_output / "codex-luna-max", simulation=False))
        self.assertFalse(_pass5_output_allowed(
            "owner__repo-7", case_output, simulation=False))
        self.assertFalse(_pass5_output_allowed(
            "owner__repo-7", RUNS_ROOT / "run", simulation=False))

    def test_formal_kimi_k3_config_is_machine_bound(self):
        value = {
            "model_id": "ep-20260817150115-9fx8h",
            "agent": "kimi-code",
            "agent_version": "0.29.0",
            "provider_profile": {
                "protocol": "responses",
                "base_url": "https://ark-gateway.invalid/api/v3",
                "allowed_host": "ark-gateway.invalid",
                "credential_env": "ARK_API_KEY",
                "capabilities": ["image_in", "thinking"],
            },
            "agent_runtime": {
                "max_context_size": 1048576,
                "max_completion_tokens": 131072,
                "thinking_effort": "max",
                "max_steps_per_turn": 0,
                "timeout_sec": 7200,
                "setup_timeout_sec": 1800,
            },
            "network_policy": {"environment_hosts": [],
                               "agent_hosts": ["ark-gateway.invalid"]},
            "tool_policy": json.loads(json.dumps(OFFICIAL_K3_TOOL_POLICY)),
        }
        _require_official_k3_config(value)
        value["model_id"] = "mock-model"
        with self.assertRaisesRegex(ValueError, "formal_pass5_config_not_official_kimi_k3"):
            _require_official_k3_config(value)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="workflow-test-", dir=TMP_ROOT))
        self.runs = Path(tempfile.mkdtemp(prefix="workflow-test-", dir=RUNS_ROOT))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.addCleanup(lambda: shutil.rmtree(self.runs, ignore_errors=True))
        self.instance = "owner__repo-7"
        self.candidate = self.tmp / "candidate"
        for directory in ("environment", "solution", "tests"):
            (self.candidate / directory).mkdir(parents=True)
        (self.candidate / "instruction.md").write_text("Edit /testbed.\n")
        (self.candidate / "task.toml").write_text('schema_version = "1.2"\n')
        (self.candidate / "environment/Dockerfile").write_text(
            "FROM example.invalid/base:fixed\n"
            "RUN npm install --global @moonshot-ai/kimi-code@0.29.0 @openai/codex@0.148.0 \\\n"
            "    && npm config set offline true --location=user \\\n"
            "    && printf 'apk is disabled in this frozen glibc image' > /usr/local/bin/apk \\\n"
            "    && command -v kimi && kimi --version \\\n"
            "    && command -v codex && codex --version\n"
        )
        (self.candidate / "solution/solve.sh").write_text("#!/bin/sh\n")
        (self.candidate / "tests/test.sh").write_text("#!/bin/sh\n")
        (self.candidate / "tests/config.json").write_text(json.dumps({
            "FAIL_TO_PASS": ["f"], "PASS_TO_PASS": ["p"]}) + "\n")
        (self.candidate / "tests/test_manifest.json").write_text(json.dumps({
            "tests": [{"test_id": "f", "class": "F2P"},
                      {"test_id": "p", "class": "P2P"}]}) + "\n")
        checksum, _ = _task_inventory(self.candidate)
        export_manifest = self.candidate.parent / f"{self.candidate.name}.export_manifest.json"
        export_manifest.write_text(json.dumps({
            "schema_version": "visual-harbor-export-v1",
            "task_material_sha256": checksum,
        }) + "\n")
        (self.candidate.parent / f"{self.candidate.name}.export_manifest.json.commit.json").write_text(
            json.dumps({
                "schema_version": "visual-harbor-export-commit-v1",
                "task_material_sha256": checksum,
                "sidecar_sha256": _sha256(export_manifest),
                "transaction_sha256": "0" * 64,
            }) + "\n")
        manifest_sha = _sha256(self.candidate / "tests/test_manifest.json")
        measurement_runs = {"baseline": [], "reference": []}
        for side in ("baseline", "reference"):
            for repetition in (1, 2):
                statuses = ["fail", "pass"] if side == "baseline" else ["pass", "pass"]
                path = self.tmp / f"{side}-{repetition}.json"
                path.write_text(json.dumps({
                    "schema_version": "pipeline-test-side-run-v1", "side": side,
                    "repetition": repetition, "test_manifest_sha256": manifest_sha,
                    "results": [{"test_id": "f", "class": "F2P", "status": statuses[0]},
                                {"test_id": "p", "class": "P2P", "status": statuses[1]}],
                    "summary": {"pass": statuses.count("pass"), "fail": statuses.count("fail"),
                                "skip": 0, "missing": 0, "error": 0,
                                "flaky": 0, "unexecuted": 0},
                }) + "\n")
                measurement_runs[side].append(self._binding(path))
        control_runs = []
        for role, agent, reward, statuses in (
                ("baseline_nop", "nop", 0, ["fail", "pass"]),
                ("oracle", "oracle", 1, ["pass", "pass"])):
            trial = self.tmp / f"control-{agent}"
            (trial / "verifier").mkdir(parents=True)
            result_path = trial / "result.json"
            verifier_path = trial / "verifier/test_results.json"
            result_path.write_text(json.dumps({
                "task_checksum": "b" * 64,
                "config": {"task": {"path": self._relative(self.candidate)}},
                "agent_info": {"name": agent, "version": "1.0", "model_info": None},
                "exception_info": None,
                "verifier_result": {"rewards": {"reward": reward}},
            }) + "\n")
            verifier_path.write_text(json.dumps({
                "reward": reward,
                "results": [{"test_id": "f", "class": "F2P", "status": statuses[0]},
                            {"test_id": "p", "class": "P2P", "status": statuses[1]}],
                "summary": {"pass": statuses.count("pass"), "fail": statuses.count("fail"),
                            "skip": 0, "missing": 0, "error": 0},
                "contract_errors": [],
            }) + "\n")
            control_runs.append({"role": role, "agent": agent, "task_checksum": "b" * 64,
                                 "reward": reward, "result": self._binding(result_path),
                                 "verifier_result": self._binding(verifier_path)})
        negative_path = self.tmp / "negative-controls.json"
        negative_path.write_text(json.dumps({
            "schema_version": "visual-harbor-negative-controls-v1",
            "status": "all_controls_passed",
            "canonical_task_material_sha256": checksum,
            "completed_controls": len(CONTROL_SPECS),
            "controls": {kind: {"control_passed": True}
                         for _name, kind, _agent in CONTROL_SPECS},
        }) + "\n")
        self.evidence = {}
        evidence_values = {
            "visual": {"schema_version": "pipeline-human-gate-v1", "mode": "simulation",
                       "instance_id": self.instance, "task_sha256": checksum,
                       "gate": "multimodal_necessity", "decision": "approved", "source": "mock",
                       "reviewer": "mock", "rationale": "test fixture"},
            "measurement": {"schema_version": "pipeline-test-measurement-v1", "mode": "simulation",
                            "instance_id": self.instance, "task_sha256": checksum,
                            "all_transitions_match": True, "FAIL_TO_PASS": ["f"],
                            "PASS_TO_PASS": ["p"],
                            "test_manifest": self._binding(self.candidate / "tests/test_manifest.json"),
                            "baseline_runs": measurement_runs["baseline"],
                            "reference_runs": measurement_runs["reference"],
                            "transitions": [
                                {"test_id": "f", "class": "F2P", "expected": "fail->pass", "actual": "fail->pass", "matches": True},
                                {"test_id": "p", "class": "P2P", "expected": "pass->pass", "actual": "pass->pass", "matches": True}],
                            "rationale": "test fixture"},
            "tests": {"schema_version": "pipeline-human-gate-v1", "mode": "simulation",
                      "instance_id": self.instance, "task_sha256": checksum,
                      "gate": "f2p_p2p_semantic_validity", "decision": "approved", "source": "mock",
                      "reviewer": "mock", "rationale": "test fixture"},
            "controls": {"schema_version": "pipeline-harbor-controls-v1", "mode": "simulation",
                         "instance_id": self.instance, "task_sha256": checksum,
                         "harbor_task_checksum": "b" * 64,
                         "empty_reward": 0, "gold_reward": 1, "exception_count": 0,
                         "negative_controls": self._binding(negative_path),
                         "runs": control_runs},
        }
        for name, value in evidence_values.items():
            path = self.tmp / f"{name}.json"
            path.write_text(json.dumps(value) + "\n")
            self.evidence[name] = self._binding(path)
        self.job_config = self.tmp / "harbor_job.json"
        self.job_config.write_text(json.dumps({"task": self._relative(self.candidate),
                                               "agent": "mock-agent", "model": "mock-model"}) + "\n")
        self.config = self.tmp / "pass5_config.json"
        self.config.write_text(json.dumps({
            "schema_version": "frozen-pass5-config-v1",
            "model_id": "mock-model", "agent": "mock-agent", "agent_version": "1.0",
            "provider_profile": {
                "protocol": "responses",
                "base_url": "https://ark-gateway.invalid/api/v3",
                "allowed_host": "ark-gateway.invalid",
                "credential_env": "ARK_API_KEY",
                "capabilities": ["image_in", "thinking"],
            },
            "agent_runtime": {
                "max_context_size": 1048576, "max_completion_tokens": 131072,
                "thinking_effort": "max", "max_steps_per_turn": 0,
                "timeout_sec": 7200, "setup_timeout_sec": 1800,
            },
            "valid_trials": 5, "trial_concurrency": 2, "max_invalid_replacements": 2,
            "expected_test_ids": ["f", "p"],
            "harbor_executable": ".runtime/venv/bin/harbor",
            "harbor_executable_sha256": "9d952929a70786bb350c0a20f1bfb0447eda591ccd894ad2ba4db7ec44378eae",
            "harbor_version": "0.22.0",
            "harbor_job_config": self._binding(self.job_config),
            "network_policy": {"environment_hosts": [], "agent_hosts": []},
            "tool_policy": {
                "enforcement": "runtime_network_deny_and_trace_rejection",
                "hosted_tools": "disabled", "mcp_servers": [], "skills": [],
                "forbidden_tools": ["web_search"],
            },
        }) + "\n")
        self.freeze = self.tmp / "pipeline_freeze.json"
        freeze_entries = lambda paths: [
            {"path": path, "sha256": _sha256(WORKSPACE_ROOT / path)} for path in sorted(paths)
        ]
        harbor_lock = REPORT_ROOT / "reproducibility/03_harbor_python.lock.txt"
        verifier_lock = REPORT_ROOT / "reproducibility/04_verifier_python.lock.txt"
        self.freeze.write_text(json.dumps({
            "schema_version": "pipeline-freeze-manifest-v1",
            "formal_promotion_ready": {
                "status": "blocked", "clean_hash_locked_resolution": True,
                "blocking_limitations": ["simulation fixture"],
                "runtime_bindings": {
                    "harbor_runtime_snapshot_sha256": _sha256(harbor_lock),
                    "verifier_runtime_snapshot_sha256": _sha256(verifier_lock),
                },
                "docker_binding": {"client_version": "29.6.1",
                    "compose_version": "5.2.0", "daemon_version": "fixture-daemon",
                    "daemon_observed_at": "2026-09-02T00:00:00Z"},
                "harbor_binding": {"version": "0.22.0", "task_schema": "1.2"},
            },
            "code": freeze_entries(REQUIRED_FREEZE_CODE),
            "schemas": freeze_entries(REQUIRED_FREEZE_SCHEMAS),
            "dependencies": {
                "harbor_runtime": {"python": "3.12.13", "harbor": "0.22.0",
                                   "installed_snapshot": self._binding(harbor_lock)},
                "verifier_runtime": {"python": "3.12.13",
                                     "installed_snapshot": self._binding(verifier_lock)},
                "clean_hash_locked_resolution": True,
            },
            "docker": {"client_version": "29.6.1", "compose_version": "5.2.0",
                       "daemon_version": "fixture-daemon",
                       "daemon_observed_at": "2026-09-02T00:00:00Z"},
            "harbor": {"version": "0.22.0", "task_schema": "1.2"},
            "limitations": [],
        }) + "\n")
        self.packet = {
            "schema_version": "pipeline-promotion-packet-v1",
            "instance_id": self.instance,
            "pipeline_freeze": self._binding(self.freeze),
            "candidate_task": {"path": self._relative(self.candidate), "sha256": checksum},
            "review_context": {
                "dossier": self.evidence["visual"],
                "test_manifest": self._binding(self.candidate / "tests/test_manifest.json"),
                "test_review_context": self.evidence["tests"],
            },
            "visual_gate": {"status": "approved", "source": "mock", "evidence": self.evidence["visual"]},
            "measurement": {"status": "measured", "all_transitions_match": True,
                            "f2p_ids": ["f"], "p2p_ids": ["p"],
                            "evidence": self.evidence["measurement"]},
            "tests_gate": {"status": "approved", "source": "mock", "evidence": self.evidence["tests"]},
            "controls": {"status": "passed", "task_sha256": checksum,
                         "empty_reward": 0, "gold_reward": 1, "exception_count": 0,
                         "evidence": self.evidence["controls"]},
            "image": {"mode": "simulation", "reference": "simulation-only",
                      "simulated_image_id": "sha256:" + "a" * 64},
            "pass5_config": self._binding(self.config),
        }

    def _official_config(self, task_path):
        job = self.tmp / "official-k3-job.json"
        job.write_text(json.dumps({
            "n_concurrent_trials": 2,
            "retry": {"max_retries": 0},
            "load_trajectory": None,
            "resume": None,
            "environment": {"type": "docker", "delete": True,
                            "kwargs": {"keep_containers": False},
                            "extra_allowed_hosts": []},
            "agents": [{
                "name": "kimi-code", "model_name": "ep-20260817150115-9fx8h",
                "n_concurrent": 2, "override_timeout_sec": 7200,
                "override_setup_timeout_sec": 1800,
                "extra_allowed_hosts": ["ark-gateway.invalid"],
                "kwargs": {"version": "0.29.0"},
                "env": {
                    "KIMI_MODEL_API_KEY": "${ARK_API_KEY}",
                    "KIMI_MODEL_BASE_URL": "https://ark-gateway.invalid/api/v3",
                    "KIMI_MODEL_MAX_CONTEXT_SIZE": "1048576",
                    "KIMI_MODEL_MAX_COMPLETION_TOKENS": "+131072",
                    "KIMI_MODEL_CAPABILITIES": "image_in,thinking",
                    "KIMI_MODEL_THINKING_EFFORT": "max",
                    "KIMI_LOOP_MAX_STEPS_PER_TURN": "0",
                },
                "mcp_servers": [], "skills": [],
            }],
            "tasks": [{"path": task_path}],
        }) + "\n")
        config = json.loads(self.config.read_text())
        config.update({
            "model_id": "ep-20260817150115-9fx8h",
            "agent": "kimi-code", "agent_version": "0.29.0",
            "harbor_job_config": self._binding(job),
            "provider_profile": {
                "protocol": "responses",
                "base_url": "https://ark-gateway.invalid/api/v3",
                "allowed_host": "ark-gateway.invalid",
                "credential_env": "ARK_API_KEY",
                "capabilities": ["image_in", "thinking"],
            },
            "agent_runtime": {
                "max_context_size": 1048576, "max_completion_tokens": 131072,
                "thinking_effort": "max", "max_steps_per_turn": 0,
                "timeout_sec": 7200, "setup_timeout_sec": 1800,
            },
            "network_policy": {"environment_hosts": [],
                               "agent_hosts": ["ark-gateway.invalid"]},
            "tool_policy": json.loads(json.dumps(OFFICIAL_K3_TOOL_POLICY)),
        })
        return config, job

    def test_official_k3_job_config_binds_provider_runtime(self):
        config, job = self._official_config(self._relative(self.candidate))
        frozen = {"task": {"path": self._relative(self.candidate)}}
        _validate_official_k3_job_config(config, frozen)
        original = json.loads(job.read_text())
        mutations = (
            lambda value: value["agents"][0]["env"].update(
                KIMI_MODEL_BASE_URL="https://example.invalid/v1"),
            lambda value: value["agents"][0].update(override_setup_timeout_sec=1),
            lambda value: value["agents"][0].update(n_concurrent=1),
            lambda value: value["agents"][0].update(override_memory_mb=1),
            lambda value: value["environment"].update(extra_allowed_hosts=["example.invalid"]),
            lambda value: value["environment"]["kwargs"].update(force_build=True),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                value = json.loads(json.dumps(original))
                mutate(value)
                job.write_text(json.dumps(value) + "\n")
                config["harbor_job_config"] = self._binding(job)
                with self.assertRaisesRegex(
                        ValueError, "harbor_job_config_(official_kimi_k3_runtime_mismatch|unapproved_extension|network_policy_mismatch|budget_invalid)"):
                    _validate_official_k3_job_config(config, frozen)

    def test_official_k3_job_rejects_redaction_unsafe_completion_encoding(self):
        config, job = self._official_config(self._relative(self.candidate))
        frozen = {"task": {"path": self._relative(self.candidate)}}
        unsafe = json.loads(job.read_text())
        unsafe["agents"][0]["env"]["KIMI_MODEL_MAX_COMPLETION_TOKENS"] = "131072"
        job.write_text(json.dumps(unsafe) + "\n")
        config["harbor_job_config"] = self._binding(job)
        with self.assertRaisesRegex(
                ValueError, "harbor_job_config_official_kimi_k3_runtime_mismatch"):
            _validate_official_k3_job_config(config, frozen)

    def test_formal_jobs_require_explicit_empty_mcp_and_skills(self):
        frozen = {"task": {"path": self._relative(self.candidate)}}
        for factory in (self._official_config, self._official_codex_config):
            config, job = factory(self._relative(self.candidate))
            original = json.loads(job.read_text())
            for field, unsafe in (("mcp_servers", [{"name": "remote"}]),
                                  ("skills", ["github:owner/repo"])):
                with self.subTest(agent=config["agent"], field=field):
                    value = json.loads(json.dumps(original))
                    value["agents"][0][field] = unsafe
                    job.write_text(json.dumps(value) + "\n")
                    config["harbor_job_config"] = self._binding(job)
                    with self.assertRaisesRegex(
                            ValueError,
                            f"harbor_job_config_official_.*_runtime_mismatch"):
                        _validate_formal_job_config(config, frozen)

    def test_formal_profiles_reject_tool_policy_drift(self):
        for factory in (self._official_config, self._official_codex_config):
            config, _job = factory(self._relative(self.candidate))
            config["tool_policy"]["mcp_servers"] = [{"name": "remote"}]
            with self.subTest(agent=config["agent"]), self.assertRaisesRegex(
                    ValueError, "formal_pass5_config_not_official"):
                _require_formal_pass5_config(config)

    def test_formal_job_requires_pinned_agent_in_task_image(self):
        frozen = {"task": {"path": self._relative(self.candidate)}}
        original = (self.candidate / "environment/Dockerfile").read_text()
        cases = (
            (self._official_config, "@moonshot-ai/kimi-code@0.29.0", "kimi_k3"),
            (self._official_codex_config, "@openai/codex@0.148.0", "codex_luna_max"),
        )
        for factory, required, provider in cases:
            with self.subTest(provider=provider):
                config, _job = factory(self._relative(self.candidate))
                (self.candidate / "environment/Dockerfile").write_text(
                    original.replace(required, required.rsplit("@", 1)[0] + "@latest"))
                with self.assertRaisesRegex(
                        ValueError,
                        f"formal_{provider}_offline_agent_image_prerequisite_missing"):
                    _validate_formal_job_config(config, frozen)
                (self.candidate / "environment/Dockerfile").write_text(
                    "FROM example.invalid/base:fixed\n"
                    + "\n".join(f"# {line}" for line in original.splitlines()[1:])
                    + "\n")
                with self.assertRaisesRegex(
                        ValueError,
                        f"formal_{provider}_offline_agent_image_prerequisite_missing"):
                    _validate_formal_job_config(config, frozen)
                (self.candidate / "environment/Dockerfile").write_text(original)

        config, _job = self._official_config(self._relative(self.candidate))
        (self.candidate / "environment/Dockerfile").write_text(
            original.replace("npm config set offline true", "npm config set offline false")
        )
        with self.assertRaisesRegex(
                ValueError,
                "formal_kimi_k3_offline_agent_image_prerequisite_missing"):
            _validate_formal_job_config(config, frozen)
        (self.candidate / "environment/Dockerfile").write_text(original)

    def _official_codex_config(self, task_path):
        job = self.tmp / "official-codex-job.json"
        job.write_text(json.dumps({
            "n_concurrent_trials": 2,
            "retry": {"max_retries": 0},
            "load_trajectory": None,
            "resume": None,
            "environment": {"type": "docker", "delete": True,
                            "kwargs": {"keep_containers": False},
                            "extra_docker_compose": [
                                OFFICIAL_CODEX_COMPOSE_OVERLAY["path"],
                            ],
                            "extra_allowed_hosts": []},
            "agents": [{
                "name": "codex", "model_name": "gpt-5.6-luna",
                "n_concurrent": 2, "override_timeout_sec": 7200,
                "override_setup_timeout_sec": 1800,
                "extra_allowed_hosts": [
                    "api.openai.com", "auth.openai.com", "chatgpt.com",
                ],
                "kwargs": {"version": "0.148.0", "reasoning_effort": "max",
                           "web_search": "disabled",
                           "config": {
                               "cli_auth_credentials_store": "file",
                               "mcp_servers": {},
                               "features": dict(OFFICIAL_CODEX_DISABLED_FEATURES),
                           }},
                "env": {"CODEX_FORCE_AUTH_JSON": "YES"},
                "mcp_servers": [], "skills": [],
            }],
            "tasks": [{"path": task_path}],
        }) + "\n")
        config = json.loads(self.config.read_text())
        config.update({
            "model_id": "gpt-5.6-luna",
            "agent": "codex", "agent_version": "0.148.0",
            "harbor_job_config": self._binding(job),
            "provider_profile": {
                "protocol": "codex_cli_chatgpt_auth",
                "allowed_hosts": [
                    "api.openai.com", "auth.openai.com", "chatgpt.com",
                ],
                "credential_source": "harbor_auth_json",
                "capabilities": ["image_in", "thinking"],
                "compose_overlay": dict(OFFICIAL_CODEX_COMPOSE_OVERLAY),
            },
            "agent_runtime": {
                "max_context_size": 0, "max_completion_tokens": 0,
                "thinking_effort": "max", "max_steps_per_turn": 0,
                "timeout_sec": 7200, "setup_timeout_sec": 1800,
            },
            "network_policy": {
                "environment_hosts": [],
                "agent_hosts": [
                    "api.openai.com", "auth.openai.com", "chatgpt.com",
                ],
            },
            "tool_policy": json.loads(json.dumps(OFFICIAL_CODEX_TOOL_POLICY)),
        })
        return config, job

    def test_official_codex_job_config_binds_luna_max_and_auth_transport(self):
        config, job = self._official_codex_config(self._relative(self.candidate))
        frozen = {"task": {"path": self._relative(self.candidate)}}
        self.assertEqual("codex_luna_max", _require_formal_pass5_config(config))
        _validate_formal_job_config(config, frozen)
        schema = json.loads((REPORT_ROOT / "schemas/frozen_pass5_config_v1.schema.json").read_text())
        jsonschema.Draft202012Validator(schema).validate(config)

        original = json.loads(job.read_text())
        mutations = (
            lambda value: value["agents"][0]["env"].update(CODEX_FORCE_AUTH_JSON="0"),
            lambda value: value["agents"][0]["kwargs"].update(reasoning_effort="high"),
            lambda value: value["agents"][0]["kwargs"].update(version="latest"),
            lambda value: value["agents"][0]["kwargs"]["config"]["features"].update(
                browser_use=True),
            lambda value: value["agents"][0]["kwargs"].update(web_search="live"),
            lambda value: value["agents"][0]["extra_allowed_hosts"].append("example.invalid"),
            lambda value: value["environment"]["extra_docker_compose"].append(
                "reproducibility/09_pipeline_freeze_manifest.json"),
            lambda value: value["environment"].update(cap_add=["SYS_ADMIN"]),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                value = json.loads(json.dumps(original))
                mutate(value)
                job.write_text(json.dumps(value) + "\n")
                config["harbor_job_config"] = self._binding(job)
                with self.assertRaisesRegex(
                        ValueError,
                        "harbor_job_config_(official_codex_luna_max_runtime_mismatch|binding_mismatch|network_policy_mismatch)"):
                    _validate_formal_job_config(config, frozen)

    def test_formal_provider_rejects_unlisted_agent_model_pair(self):
        config, _job = self._official_codex_config(self._relative(self.candidate))
        config["model_id"] = "gpt-5.6-sol"
        with self.assertRaisesRegex(ValueError, "formal_pass5_provider_profile_not_supported"):
            _require_formal_pass5_config(config)

    def test_official_codex_profile_rejects_version_and_profile_drift(self):
        config, _job = self._official_codex_config(self._relative(self.candidate))
        for mutation in (
                lambda value: value.update(agent_version="0.151.0-alpha.7.2"),
                lambda value: value["provider_profile"].update(
                    allowed_hosts=["api.openai.com"]),
                lambda value: value["provider_profile"]["compose_overlay"].update(
                    sha256="0" * 64),
                lambda value: value["provider_profile"]["compose_overlay"].update(
                    path="reproducibility/09_pipeline_freeze_manifest.json"),
                lambda value: value["agent_runtime"].update(thinking_effort="high")):
            with self.subTest(mutation=mutation):
                changed = json.loads(json.dumps(config))
                mutation(changed)
                with self.assertRaisesRegex(
                        ValueError, "formal_pass5_config_not_official_codex_luna_max"):
                    _require_formal_pass5_config(changed)

    def test_formal_profiles_reject_source_network_hosts(self):
        forbidden = (
            "github.com", "raw.githubusercontent.com",
            "nodejs.org", "registry.npmjs.org",
        )
        schema = json.loads((
            REPORT_ROOT / "schemas/frozen_pass5_config_v1.schema.json"
        ).read_text())
        for factory in (self._official_config, self._official_codex_config):
            config, job = factory(self._relative(self.candidate))
            frozen = {"task": {"path": self._relative(self.candidate)}}
            original_job = json.loads(job.read_text())
            for host in forbidden:
                with self.subTest(agent=config["agent"], host=host):
                    changed = json.loads(json.dumps(config))
                    changed["network_policy"]["environment_hosts"] = [host]
                    with self.assertRaisesRegex(ValueError, "formal_pass5_config_not_official"):
                        _require_formal_pass5_config(changed)
                    self.assertTrue(list(
                        jsonschema.Draft202012Validator(schema).iter_errors(changed)))

                    changed = json.loads(json.dumps(config))
                    changed["network_policy"]["agent_hosts"].append(host)
                    with self.assertRaisesRegex(ValueError, "formal_pass5_config_not_official"):
                        _require_formal_pass5_config(changed)
                    self.assertTrue(list(
                        jsonschema.Draft202012Validator(schema).iter_errors(changed)))

                    leaked_job = json.loads(json.dumps(original_job))
                    leaked_job["environment"]["extra_allowed_hosts"] = [host]
                    job.write_text(json.dumps(leaked_job) + "\n")
                    config["harbor_job_config"] = self._binding(job)
                    with self.assertRaisesRegex(
                            ValueError, "harbor_job_config_network_policy_mismatch"):
                        _validate_formal_job_config(config, frozen)
                    job.write_text(json.dumps(original_job) + "\n")
                    config["harbor_job_config"] = self._binding(job)

                    leaked_job = json.loads(json.dumps(original_job))
                    leaked_job["agents"][0]["extra_allowed_hosts"].append(host)
                    job.write_text(json.dumps(leaked_job) + "\n")
                    config["harbor_job_config"] = self._binding(job)
                    with self.assertRaisesRegex(
                            ValueError, "harbor_job_config_official_.*_runtime_mismatch"):
                        _validate_formal_job_config(config, frozen)
                    job.write_text(json.dumps(original_job) + "\n")
                    config["harbor_job_config"] = self._binding(job)

    def _relative(self, path: Path) -> str:
        return path.resolve().relative_to(WORKSPACE_ROOT.resolve()).as_posix()

    def _binding(self, path: Path) -> dict:
        return {"path": self._relative(path), "sha256": _sha256(path)}

    def _packet_path(self, name="packet.json") -> Path:
        path = self.tmp / name
        path.write_text(json.dumps(self.packet, indent=2) + "\n")
        return path

    def _promote(self):
        output_root = self.tmp / "promoted"
        record = self.runs / "promotion.json"
        result = promote(self._packet_path(), output_root, record, simulation=True)
        return result, record, self.runs / f"{self.instance}.frozen.json"

    def test_simulation_walks_every_promotion_state_without_formal_admission(self):
        result, record, frozen_path = self._promote()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["current_state"], "frozen")
        self.assertEqual([event["to"] for event in result["events"]], [
            "visual_approved", "tests_measured", "tests_approved",
            "harbor_controls_passed", "frozen",
        ])
        frozen = json.loads(frozen_path.read_text())
        self.assertEqual(frozen["mode"], "simulation")
        _replay_promotion_evidence(result, frozen)
        self.assertEqual(frozen["harbor_task_checksum"], "b" * 64)
        self.assertTrue(Path(WORKSPACE_ROOT / frozen["task"]["path"]).is_relative_to(TMP_ROOT))
        self.assertFalse((REPORT_ROOT / self.instance).exists())
        for schema_name, artifact in (
            ("pipeline_state_ledger_v1.schema.json", json.loads(record.read_text())),
            ("frozen_harbor_task_v1.schema.json", frozen),
        ):
            schema = json.loads((REPORT_ROOT / "schemas" / schema_name).read_text())
            jsonschema.Draft202012Validator(schema).validate(artifact)

    def test_incomplete_pipeline_freeze_is_rejected_before_promotion(self):
        freeze = json.loads(self.freeze.read_text())
        freeze["code"] = [item for item in freeze["code"]
                          if item["path"] != "code/report_pipeline/calibration.py"]
        self.freeze.write_text(json.dumps(freeze) + "\n")
        self.packet["pipeline_freeze"] = self._binding(self.freeze)
        result, record, frozen = self._promote()
        self.assertEqual("pipeline_freeze_code_incomplete:code/report_pipeline/calibration.py",
                         result["rejection"]["code"])
        self.assertEqual("preflight", result["rejection"]["stage"])
        self.assertTrue(record.is_file())
        self.assertFalse(frozen.exists())

    def test_formal_readiness_rejects_bool_dirty_locks_limitations_and_daemon_gap(self):
        freeze = json.loads(self.freeze.read_text())
        for mutate in (
                lambda value: value.update(formal_promotion_ready=True),
                lambda value: value["dependencies"].update(clean_hash_locked_resolution=False),
                lambda value: value.update(limitations=["blocking"]),
                lambda value: value["docker"].update(daemon_observed_at=None)):
            with self.subTest(mutate=mutate):
                candidate = json.loads(json.dumps(freeze))
                mutate(candidate)
                if isinstance(candidate["formal_promotion_ready"], bool):
                    self.assertNotIsInstance(candidate["formal_promotion_ready"], dict)
                else:
                    candidate["formal_promotion_ready"]["status"] = "ready"
                    candidate["formal_promotion_ready"]["blocking_limitations"] = []
                    with self.assertRaisesRegex(ValueError, "not_formal_ready"):
                        _require_formal_freeze_ready(candidate)

    def test_large_candidate_file_is_still_secret_scanned_and_size_bounded(self):
        payload = self.candidate / "environment/large.bin"
        payload.write_bytes(b"x" * (3 * 1024 * 1024) + b"\napi_key=abcdefghijklmnop\n")
        with self.assertRaisesRegex(ValueError, "secret_marker"):
            _validate_task_tree(self.candidate)
        payload.unlink()
        with payload.open("wb") as stream:
            stream.truncate(65 * 1024 * 1024)
        with self.assertRaisesRegex(ValueError, "size_budget"):
            _validate_task_tree(self.candidate)

    def test_secret_scan_crosses_chunk_boundary_and_unbounded_whitespace(self):
        payload = self.candidate / "environment/chunked.bin"
        payload.write_bytes(
            b"x" * (1024 * 1024 - len(b"api_key"))
            + b"api_key" + b" " * 4096 + b"=abcdefghijklmnop")
        with self.assertRaisesRegex(ValueError, "secret_marker"):
            _validate_task_tree(self.candidate)

    def test_common_standalone_service_tokens_are_rejected(self):
        examples = (
            "xoxb-12345678901234567890",
            "npm_12345678901234567890",
            "glpat-12345678901234567890",
            "AIza123456789012345678901234567890",
            "sk_live_1234567890123456",
            "sk-proj-1234567890123456",
            "sk-svcacct-1234567890123456",
            "sk-ant-1234567890123456",
            "sk-12345678901234567890",
        )
        for index, secret in enumerate(examples):
            with self.subTest(index=index):
                payload = self.candidate / f"environment/credential-{index}.txt"
                payload.write_text(secret)
                try:
                    with self.assertRaisesRegex(ValueError, "secret_marker"):
                        _validate_task_tree(self.candidate)
                finally:
                    payload.unlink()

    def test_formal_dossier_admission_is_rebuilt_and_human_review_is_rejected(self):
        bindings = {
            "verifier_path": "verifier.json", "verifier_sha256": "a" * 64,
            "archive_path": "archive.json", "archive_sha256": "b" * 64,
            "classification_path": "classification.json",
            "classification_sha256": "c" * 64,
        }
        rebuilt = {
            "candidate_id": self.instance, "status": "admitted_to_test_construction",
            "repository": "owner/repo", "pr_number": 7, "url": "https://example.test",
            "title": "fixture", "source_bindings": bindings, "git": {},
            "changed_files": [], "author_test_change_detected": True,
            "leakage_policy": {},
            "visual_admission": {"admission_route": "v3_strict_nontext_visual",
                "v3_classification": {"status": "complete",
                    "strict_multimodal_admission": "非文字视觉信息候选不可替代",
                    "human_review_required": False}},
        }
        dossier = self.tmp / "dossier.json"
        dossier.write_text(json.dumps(rebuilt))
        with mock.patch("report_pipeline.candidate.build", return_value=rebuilt):
            self.assertEqual(
                _validate_formal_dossier(dossier, self.instance)["candidate_id"],
                self.instance)
        unresolved = json.loads(json.dumps(rebuilt))
        unresolved["visual_admission"]["v3_classification"]["human_review_required"] = True
        with mock.patch("report_pipeline.candidate.build", return_value=unresolved):
            with self.assertRaisesRegex(ValueError, "formal_dossier_v3_admission_invalid"):
                _validate_formal_dossier(dossier, self.instance)

    def test_real_promotion_rejects_mock_human_gate_before_copy(self):
        record = REPORT_ROOT / "evidence" / f"{self.instance}.promotion_ledger.json"
        self.addCleanup(lambda: record.unlink(missing_ok=True))
        formal_freeze = REPORT_ROOT / "reproducibility/09_pipeline_freeze_manifest.json"
        self.packet["pipeline_freeze"] = self._binding(formal_freeze)
        with mock.patch("report_pipeline.workflow._validate_pipeline_freeze",
                        return_value=(formal_freeze.resolve(), json.loads(
                            self.freeze.read_text()))):
            result = promote(self._packet_path("real-reject.json"), REPORT_ROOT / "cases",
                             record, simulation=False)
        self.assertEqual("formal_dossier_temporary_evidence_not_allowed",
                         result["rejection"]["code"])
        self.assertTrue(record.is_file())
        self.assertFalse((REPORT_ROOT / "cases" / self.instance).exists())

    def test_real_promotion_cannot_relabel_mock_evidence_as_human(self):
        record = REPORT_ROOT / "evidence" / f"{self.instance}.promotion_ledger.json"
        self.addCleanup(lambda: record.unlink(missing_ok=True))
        formal_freeze = REPORT_ROOT / "reproducibility/09_pipeline_freeze_manifest.json"
        self.packet["pipeline_freeze"] = self._binding(formal_freeze)
        self.packet["visual_gate"]["source"] = "human"
        with mock.patch("report_pipeline.workflow._validate_pipeline_freeze",
                        return_value=(formal_freeze.resolve(), json.loads(
                            self.freeze.read_text()))):
            result = promote(self._packet_path("real-relabel.json"), REPORT_ROOT / "cases",
                             record, simulation=False)
        self.assertEqual("formal_dossier_temporary_evidence_not_allowed",
                         result["rejection"]["code"])

    def test_candidate_symlink_is_rejected_before_copy(self):
        target = self.tmp / "outside.txt"
        target.write_text("secret\n")
        (self.candidate / "environment/link.txt").symlink_to(target)
        packet = self._packet_path("symlink.json")
        record = self.runs / "symlink.json"
        result = promote(packet, self.tmp / "symlink-output", record, simulation=True)
        self.assertEqual("candidate_task_symlink_or_special_file",
                         result["rejection"]["code"])
        self.assertTrue(record.is_file())

    def test_copy_failure_is_durably_rejected_and_staging_is_removed(self):
        output_root = self.tmp / "copy-failure-output"
        record = self.runs / "copy-failure.json"
        with mock.patch("report_pipeline.workflow.shutil.copytree",
                        side_effect=OSError("fixture copy failure")):
            result = promote(self._packet_path("copy-failure-packet.json"),
                             output_root, record, simulation=True)
        self.assertEqual("promotion_copy_failed", result["rejection"]["code"])
        self.assertTrue(record.is_file())
        self.assertFalse((output_root / f".{self.instance}.promotion-staging").exists())

    def test_interrupted_three_artifact_publication_recovers_on_retry(self):
        output_root = self.tmp / "transaction-output"
        output_root.mkdir()
        record = self.runs / f"{self.instance}.promotion_ledger.json"
        packet = self._packet_path("transaction-packet.json")
        commit = self.runs / f"{self.instance}.promotion.commit.json"
        import report_pipeline.workflow as workflow_module
        original_write = workflow_module._write

        def interrupt_commit(path, value):
            if path == commit:
                raise KeyboardInterrupt("injected before promotion commit")
            return original_write(path, value)

        with mock.patch("report_pipeline.workflow._write", side_effect=interrupt_commit):
            with self.assertRaises(KeyboardInterrupt):
                promote(packet, output_root, record, simulation=True)
        transaction = self.runs / f".{self.instance}.promotion.transaction.json"
        self.assertTrue(transaction.is_file())
        self.assertTrue((output_root / self.instance).is_dir())

        result = promote(packet, output_root, record, simulation=True)
        self.assertEqual(result["current_state"], "frozen")
        self.assertTrue(commit.is_file())
        self.assertFalse(transaction.exists())

        frozen_path = self.runs / f"{self.instance}.frozen.json"
        _validate_promotion_commit(output_root / self.instance, record, frozen_path,
                                   self.instance)
        commit.unlink()
        with self.assertRaisesRegex(ValueError, "promotion_commit_missing_or_incomplete"):
            _validate_promotion_commit(output_root / self.instance, record, frozen_path,
                                       self.instance)

    def test_concurrent_promotion_for_instance_is_rejected(self):
        record = self.runs / f"{self.instance}.promotion_ledger.json"
        lock = self.runs / f".{self.instance}.promotion.lock"
        descriptor = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, "promotion_in_progress"):
                promote(self._packet_path("locked-packet.json"),
                        self.tmp / "locked-output", record, simulation=True)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def test_each_gate_has_a_stable_rejection_reason(self):
        cases = [
            ("visual", lambda p: p["visual_gate"].update(status="pending"), "visual_gate_not_approved"),
            ("measurement", lambda p: p["measurement"].update(all_transitions_match=False), "test_measurement_invalid"),
            ("tests", lambda p: p["tests_gate"].update(status="pending"), "tests_gate_not_approved"),
            ("controls", lambda p: p["controls"].update(gold_reward=0), "harbor_controls_invalid"),
        ]
        for name, mutate, expected in cases:
            with self.subTest(name=name):
                original = json.loads(json.dumps(self.packet))
                mutate(self.packet)
                packet = self._packet_path(f"packet-{name}.json")
                record = self.runs / f"{name}.json"
                result = promote(packet, self.tmp / f"out-{name}", record, simulation=True)
                self.assertEqual(result["rejection"]["code"], expected)
                self.packet = original

    def test_pass5_replaces_infrastructure_attempt_and_indexes_trajectories(self):
        _, _, frozen_path = self._promote()
        attempts = [{"classification": "infrastructure_invalid", "reason": "api_error"}]
        for index, reward in enumerate((0, 0, 1, 0, 0), 1):
            trajectory = self.tmp / f"trajectory-{index}.jsonl"
            trajectory.write_text(json.dumps({"attempt": index}) + "\n")
            attempts.append({"classification": "valid", "reward": reward,
                             "trajectory": self._binding(trajectory)})
        mock = self.tmp / "mock_trials.json"
        mock.write_text(json.dumps({"schema_version": "pass5-mock-trials-v1",
                                    "attempts": attempts}) + "\n")
        summary = run_pass5(frozen_path, self.runs / "pass5", simulation=True,
                            mock_trials_path=mock)
        self.assertEqual(summary["valid_trial_count"], 5)
        self.assertEqual(summary["infrastructure_invalid_count"], 1)
        self.assertEqual(summary["success_count"], 1)
        self.assertEqual(summary["pass_at_5"], 1)
        self.assertEqual(json.loads((self.runs / "pass5/attempts.json").read_text())["status"],
                         "completed")
        self.assertEqual(summary["state_ledger"]["path"].split("/")[-1],
                         "pipeline_state_ledger.json")
        completed = json.loads((self.runs / "pass5/pipeline_state_ledger.json").read_text())
        self.assertEqual(completed["current_state"], "pass5_completed")
        self.assertEqual(completed["events"][-1]["code"], "five_valid_trials_completed")
        audit = (self.runs / "pass5/pipeline_audit.html").read_text()
        self.assertIn("SIMULATION ONLY", audit)
        self.assertIn("api_error", audit)

    def test_pass5_replaces_answer_leakage_without_counting_model_failure(self):
        _, _, frozen_path = self._promote()
        attempts = [{
            "classification": "invalid_answer_leakage",
            "reason": "runtime_answer_source_or_unapproved_tool_access",
            "answer_leakage_hits": [{"rule": "git_history_access"}],
        }]
        for index, reward in enumerate((0, 0, 0, 0, 0), 1):
            trajectory = self.tmp / f"safe-trajectory-{index}.jsonl"
            trajectory.write_text(json.dumps({"attempt": index}) + "\n")
            attempts.append({"classification": "valid", "reward": reward,
                             "trajectory": self._binding(trajectory)})
        mock = self.tmp / "mock-leakage-trials.json"
        mock.write_text(json.dumps({"schema_version": "pass5-mock-trials-v1",
                                    "attempts": attempts}) + "\n")
        summary = run_pass5(frozen_path, self.runs / "pass5-leakage",
                            simulation=True, mock_trials_path=mock)
        self.assertEqual(5, summary["valid_trial_count"])
        self.assertEqual(0, summary["infrastructure_invalid_count"])
        self.assertEqual(1, summary["answer_leakage_invalid_count"])
        self.assertEqual(0, summary["success_count"])
        self.assertEqual([item.get("valid_trial_index") for item in summary["attempts"]],
                         [None, 1, 2, 3, 4, 5])
        schema = json.loads((REPORT_ROOT / "schemas/frozen_pass5_summary_v1.schema.json").read_text())
        jsonschema.Draft202012Validator(schema).validate(summary)

    def test_real_pass5_requires_exact_authorization_before_harbor(self):
        _, _, frozen_path = self._promote()
        frozen = json.loads(frozen_path.read_text())
        config = json.loads(self.config.read_text())
        with self.assertRaisesRegex(ValueError, "authorization_required"):
            _validate_run_authorization(None, frozen_path, frozen, config, self.runs / "pass5")

    def test_run_authorization_binds_one_output_and_run_identity(self):
        _, _, frozen_path = self._promote()
        frozen = json.loads(frozen_path.read_text())
        config = json.loads(self.config.read_text())
        output = self.runs / "authorized-output"
        authorization_path = self.runs / "authorization.json"
        authorization_path.write_text(json.dumps({
            "schema_version": "pass5-run-authorization-v1", "authorized": True,
            "run_id": "pass5-test-run-001", "output_path": self._relative(output),
            "nonce": "0123456789abcdef0123456789abcdef",
            "pipeline_freeze_sha256": frozen["pipeline_freeze"]["sha256"],
            "frozen_manifest_sha256": _sha256(frozen_path),
            "task_sha256": frozen["task"]["sha256"],
            "harbor_task_checksum": frozen["harbor_task_checksum"],
            "image_id": frozen["image"]["image_id"],
            "model_id": config["model_id"], "agent": config["agent"],
            "agent_version": config["agent_version"], "valid_trials": 5,
            "harbor_job_config_sha256": config["harbor_job_config"]["sha256"],
            "maximum_harbor_attempts": 7,
        }) + "\n")
        accepted = _validate_run_authorization(
            authorization_path, frozen_path, frozen, config, output)
        self.assertEqual(accepted["run_id"], "pass5-test-run-001")
        with self.assertRaisesRegex(ValueError, "output_path_mismatch"):
            _validate_run_authorization(
                authorization_path, frozen_path, frozen, config, self.runs / "other-output")

    def test_pass5_revalidates_frozen_task_and_mode(self):
        _, _, frozen_path = self._promote()
        frozen = json.loads(frozen_path.read_text())
        with self.assertRaisesRegex(ValueError, "mode_mismatch"):
            run_pass5(frozen_path, self.runs / "wrong-mode", simulation=False)
        task = WORKSPACE_ROOT / frozen["task"]["path"]
        (task / "instruction.md").write_text("changed\n")
        mock = self.tmp / "unused.json"
        mock.write_text('{"schema_version":"pass5-mock-trials-v1","attempts":[]}\n')
        with self.assertRaisesRegex(ValueError, "task_binding_changed"):
            run_pass5(frozen_path, self.runs / "changed-task", simulation=True,
                      mock_trials_path=mock)

    def test_harbor_022_job_and_trial_results_are_distinguished(self):
        _, _, frozen_path = self._promote()
        frozen = json.loads(frozen_path.read_text())
        config = json.loads(self.config.read_text())
        job = self.runs / "harbor-job"
        trial = job / "trial-1"
        (trial / "verifier").mkdir(parents=True)
        (trial / "agent").mkdir()
        (job / "result.json").write_text(json.dumps({
            "id": "job-1", "finished_at": "2026-09-01T00:01:00Z",
            "n_total_trials": 1,
            "stats": {"n_completed_trials": 1, "n_errored_trials": 0,
                      "n_running_trials": 0, "n_pending_trials": 0,
                      "n_cancelled_trials": 0, "n_retries": 0}}) + "\n")
        (trial / "result.json").write_text(json.dumps({
            "id": "trial-native-1", "trial_name": trial.name,
            "started_at": "2026-09-01T00:00:00Z", "finished_at": "2026-09-01T00:01:00Z",
            "task_checksum": frozen["harbor_task_checksum"],
            "config": {"task": {"path": frozen["task"]["path"]}, "job_id": "job-1",
                       "trial_name": trial.name, "source_trial": None},
            "agent_info": {"name": "mock-agent", "version": "1.0",
                           "model_info": {"name": "mock-model"}},
            "exception_info": None, "verifier_result": {"rewards": {"reward": 1}}}) + "\n")
        (trial / "verifier/test_results.json").write_text(json.dumps({
            "reward": 1, "results": [
                {"test_id": "f", "class": "F2P", "status": "pass"},
                {"test_id": "p", "class": "P2P", "status": "pass"}],
            "summary": {"pass": 2, "fail": 0, "skip": 0, "missing": 0, "error": 0},
            "contract_errors": []}) + "\n")
        (trial / "agent/trajectory.jsonl").write_text("{}\n")
        result = _classify_harbor_attempt(job, ["f", "p"], frozen, config)
        self.assertEqual(result["classification"], "valid")
        self.assertEqual(result["reward"], 1)
        trajectory = trial / "agent/trajectory.jsonl"
        trajectory.write_bytes(b"x" * (9 * 1024 * 1024) + b"\ntoken=abcdefghijklmnop\n")
        secret = _classify_harbor_attempt(job, ["f", "p"], frozen, config)
        self.assertEqual(secret["reason"], "trajectory_secret_detected")
        with trajectory.open("wb") as stream:
            stream.truncate(33 * 1024 * 1024)
        oversized = _classify_harbor_attempt(job, ["f", "p"], frozen, config)
        self.assertEqual(oversized["reason"], "trajectory_size_budget_exceeded")
        trajectory.write_text("{}\n")
        fake_attempts = []
        for index in range(1, 6):
            item = dict(result)
            item.update(attempt_ordinal=index, valid_trial_index=index,
                        trial_id=f"audit-trial-{index}", trial_name=f"audit-name-{index}",
                        trajectory_digest=f"{index:064x}")
            fake_attempts.append(item)
        fake_attempts[0].update(
            reward=0, trial_id=result["trial_id"], trial_name=result["trial_name"],
            trajectory_digest=result["trajectory_digest"])
        tampered_summary = {
            "mode": "real", "instance_id": self.instance, "attempts": fake_attempts,
            "valid_trial_count": 5, "infrastructure_invalid_count": 0,
            "success_count": 4, "pass_at_5": 1,
        }
        audit_config, _ = self._official_config(frozen["task"]["path"])
        with self.assertRaisesRegex(ValueError, "frozen_manifest_binding_missing"):
            _audit_pass5_summary(tampered_summary, frozen, audit_config)
        trial_result = json.loads((trial / "result.json").read_text())
        trial_result["task_checksum"] = "c" * 64
        (trial / "result.json").write_text(json.dumps(trial_result) + "\n")
        mismatch = _classify_harbor_attempt(job, ["f", "p"], frozen, config)
        self.assertEqual(mismatch["classification"], "infrastructure_invalid")
        self.assertEqual(mismatch["reason"], "trial_identity_binding_mismatch")

    def test_harbor_batch_classifies_trials_independently(self):
        _, _, frozen_path = self._promote()
        frozen = json.loads(frozen_path.read_text())
        config = json.loads(self.config.read_text())
        job = self.runs / "harbor-batch"
        (job / "result.json").parent.mkdir(parents=True)
        (job / "result.json").write_text(json.dumps({
            "id": "job-1", "finished_at": "2026-09-01T00:01:00Z",
            "n_total_trials": 2,
            "stats": {"n_completed_trials": 2, "n_errored_trials": 1,
                      "n_running_trials": 0, "n_pending_trials": 0,
                      "n_cancelled_trials": 0, "n_retries": 0}}) + "\n")
        for index, exception in enumerate((None, {"type": "api_error"}), 1):
            trial = job / f"trial-{index}"
            (trial / "verifier").mkdir(parents=True)
            (trial / "agent").mkdir()
            (trial / "result.json").write_text(json.dumps({
                "id": f"trial-native-{index}", "trial_name": trial.name,
                "started_at": "2026-09-01T00:00:00Z", "finished_at": "2026-09-01T00:01:00Z",
                "task_checksum": frozen["harbor_task_checksum"],
                "config": {"task": {"path": frozen["task"]["path"]}, "job_id": "job-1",
                           "trial_name": trial.name, "source_trial": None},
                "agent_info": {"name": "mock-agent", "version": "1.0",
                               "model_info": {"name": "mock-model"}},
                "exception_info": exception,
                "verifier_result": {"rewards": {"reward": 1}}}) + "\n")
            (trial / "verifier/test_results.json").write_text(json.dumps({
                "reward": 1, "results": [
                    {"test_id": "f", "class": "F2P", "status": "pass"},
                    {"test_id": "p", "class": "P2P", "status": "pass"}],
                "summary": {"pass": 2, "fail": 0, "skip": 0, "missing": 0, "error": 0},
                "contract_errors": []}) + "\n")
            (trial / "agent/trajectory.jsonl").write_text("{}\n")
        results = _classify_harbor_job(job, ["f", "p"], frozen, config, 2)
        self.assertEqual([item["classification"] for item in results],
                         ["valid", "infrastructure_invalid"])
        summary = json.loads((job / "result.json").read_text())
        summary["stats"]["n_errored_trials"] = 0
        (job / "result.json").write_text(json.dumps(summary) + "\n")
        contradictory = _classify_harbor_job(job, ["f", "p"], frozen, config, 2)
        self.assertTrue(all(item["classification"] == "infrastructure_invalid"
                            for item in contradictory))

    def test_harbor_batch_preserves_good_siblings_when_one_json_is_malformed(self):
        _, _, frozen_path = self._promote()
        frozen = json.loads(frozen_path.read_text())
        config = json.loads(self.config.read_text())
        job = self.runs / "harbor-malformed-batch"
        (job / "result.json").parent.mkdir(parents=True)
        (job / "result.json").write_text(json.dumps({
            "id": "job-1", "finished_at": "2026-09-01T00:01:00Z", "n_total_trials": 2,
            "stats": {"n_completed_trials": 2, "n_errored_trials": 1,
                      "n_running_trials": 0, "n_pending_trials": 0,
                      "n_cancelled_trials": 0, "n_retries": 0}}) + "\n")
        for index in (1, 2):
            trial = job / f"trial-{index}"
            (trial / "verifier").mkdir(parents=True)
            (trial / "agent").mkdir()
            (trial / "result.json").write_text("{bad json\n" if index == 2 else json.dumps({
                "id": f"trial-native-{index}", "trial_name": trial.name,
                "started_at": "2026-09-01T00:00:00Z", "finished_at": "2026-09-01T00:01:00Z",
                "task_checksum": frozen["harbor_task_checksum"],
                "config": {"task": {"path": frozen["task"]["path"]}, "job_id": "job-1",
                           "trial_name": trial.name, "source_trial": None},
                "agent_info": {"name": "mock-agent", "version": "1.0",
                               "model_info": {"name": "mock-model"}},
                "exception_info": None, "verifier_result": {"rewards": {"reward": 1}}}) + "\n")
            (trial / "verifier/test_results.json").write_text(json.dumps({
                "reward": 1, "results": [
                    {"test_id": "f", "class": "F2P", "status": "pass"},
                    {"test_id": "p", "class": "P2P", "status": "pass"}],
                "summary": {"pass": 2, "fail": 0, "skip": 0, "missing": 0, "error": 0},
                "contract_errors": []}) + "\n")
            (trial / "agent/trajectory.jsonl").write_text("{}\n")
        results = _classify_harbor_job(job, ["f", "p"], frozen, config, 2)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(item["classification"] == "infrastructure_invalid" for item in results))
        self.assertEqual(results[1]["reason"], "malformed_harbor_trial_artifact")

    def test_harbor_job_summary_mismatch_invalidates_otherwise_valid_trials(self):
        _, _, frozen_path = self._promote()
        frozen = json.loads(frozen_path.read_text())
        config = json.loads(self.config.read_text())
        job = self.runs / "harbor-summary-mismatch"
        trial = job / "trial-1"
        (trial / "verifier").mkdir(parents=True)
        (trial / "agent").mkdir()
        (job / "result.json").write_text(json.dumps({
            "id": "job-1", "finished_at": "2026-09-01T00:01:00Z", "n_total_trials": 9,
            "stats": {"n_completed_trials": 1, "n_errored_trials": 0,
                      "n_running_trials": 0, "n_pending_trials": 0,
                      "n_cancelled_trials": 0, "n_retries": 0}}) + "\n")
        (trial / "result.json").write_text(json.dumps({
            "id": "trial-native-1", "trial_name": trial.name,
            "started_at": "2026-09-01T00:00:00Z", "finished_at": "2026-09-01T00:01:00Z",
            "task_checksum": frozen["harbor_task_checksum"],
            "config": {"task": {"path": frozen["task"]["path"]}, "job_id": "job-1",
                       "trial_name": trial.name, "source_trial": None},
            "agent_info": {"name": "mock-agent", "version": "1.0",
                           "model_info": {"name": "mock-model"}},
            "exception_info": None, "verifier_result": {"rewards": {"reward": 1}}}) + "\n")
        (trial / "verifier/test_results.json").write_text(json.dumps({
            "reward": 1, "results": [
                {"test_id": "f", "class": "F2P", "status": "pass"},
                {"test_id": "p", "class": "P2P", "status": "pass"}],
            "summary": {"pass": 2, "fail": 0, "skip": 0, "missing": 0, "error": 0},
            "contract_errors": []}) + "\n")
        (trial / "agent/trajectory.jsonl").write_text("{}\n")
        result = _classify_harbor_job(job, ["f", "p"], frozen, config, 1)[0]
        self.assertEqual(result["classification"], "infrastructure_invalid")
        self.assertEqual(result["reason"], "job_summary_contract_mismatch")

    def test_harbor_trial_recomputes_reward_from_detailed_statuses(self):
        _, _, frozen_path = self._promote()
        frozen = json.loads(frozen_path.read_text())
        config = json.loads(self.config.read_text())
        job = self.runs / "dishonest-reward"
        trial = job / "trial-1"
        (trial / "verifier").mkdir(parents=True)
        (trial / "agent").mkdir()
        (job / "result.json").write_text(json.dumps({
            "id": "job-1", "finished_at": "2026-09-01T00:01:00Z", "n_total_trials": 1,
            "stats": {"n_completed_trials": 1, "n_errored_trials": 0,
                      "n_running_trials": 0, "n_pending_trials": 0,
                      "n_cancelled_trials": 0, "n_retries": 0}}) + "\n")
        (trial / "result.json").write_text(json.dumps({
            "id": "trial-native-1", "trial_name": trial.name,
            "started_at": "2026-09-01T00:00:00Z", "finished_at": "2026-09-01T00:01:00Z",
            "task_checksum": frozen["harbor_task_checksum"],
            "config": {"task": {"path": frozen["task"]["path"]}, "job_id": "job-1",
                       "trial_name": trial.name, "source_trial": None},
            "agent_info": {"name": "mock-agent", "version": "1.0",
                           "model_info": {"name": "mock-model"}},
            "exception_info": None, "verifier_result": {"rewards": {"reward": 1}}}) + "\n")
        (trial / "verifier/test_results.json").write_text(json.dumps({
            "reward": 1,
            "results": [{"test_id": "f", "class": "F2P", "status": "fail"},
                        {"test_id": "p", "class": "P2P", "status": "pass"}],
            "summary": {"pass": 1, "fail": 1, "skip": 0, "missing": 0, "error": 0},
            "contract_errors": []}) + "\n")
        (trial / "agent/trajectory.jsonl").write_text("{}\n")
        result = _classify_harbor_attempt(job, ["f", "p"], frozen, config)
        self.assertEqual(result["classification"], "infrastructure_invalid")
        self.assertEqual(result["reason"], "verifier_reward_mismatch")

    def test_pass5_rejection_checkpoints_triggering_invalid_attempt(self):
        _, _, frozen_path = self._promote()
        attempts = [{"classification": "infrastructure_invalid", "reason": f"infra-{i}"}
                    for i in range(3)]
        mock = self.tmp / "invalid_trials.json"
        mock.write_text(json.dumps({"schema_version": "pass5-mock-trials-v1",
                                    "attempts": attempts}) + "\n")
        output = self.runs / "rejected-pass5"
        with self.assertRaisesRegex(ValueError, "replacement_budget_exhausted"):
            run_pass5(frozen_path, output, simulation=True, mock_trials_path=mock)
        rejection = json.loads((output / "pass5_rejection.json").read_text())
        self.assertEqual(rejection["infrastructure_invalid_count"], 3)
        self.assertEqual(len(rejection["attempts"]), 3)


if __name__ == "__main__":
    unittest.main()
