from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from report_pipeline.pass5_audit import run


CHECKSUM = "a" * 64


class Pass5AuditTest(unittest.TestCase):
    def _codex_rollout(self, trial: Path,
                       calls: list[tuple[str, dict]]) -> None:
        (trial / "agent/sessions").mkdir(parents=True, exist_ok=True)
        records = [
            {"type": "session_meta", "payload": {"id": "session-test"}},
            {"type": "turn_context", "payload": {"turn_id": "turn-test"}},
        ]
        records.extend({
            "type": "response_item",
            "payload": {"type": "function_call", "name": name,
                        "arguments": arguments},
        } for name, arguments in calls)
        (trial / "agent/sessions/rollout-test.jsonl").write_text(
            "".join(json.dumps(record) + "\n" for record in records)
        )

    def _trial(self, job: Path, name: str, *, reward: float | None = 0.0,
               exception=None, checksum: str = CHECKSUM,
               tool_calls: list[tuple[str, dict]] | None = None) -> None:
        trial = job / name
        (trial / "agent").mkdir(parents=True)
        (trial / "agent" / "kimi-code.txt").write_text("trace\n")
        records = [{"type": "turn.started"}]
        for index, (tool_name, arguments) in enumerate(tool_calls or []):
            records.append({
                "type": "context.append_loop_event",
                "event": {
                    "type": "tool.call", "uuid": f"call-{index}",
                    "name": tool_name, "args": arguments,
                },
            })
        (trial / "agent" / "wire.jsonl").write_text(
            "".join(json.dumps(record) + "\n" for record in records)
        )
        result = {
            "task_name": "swe-bench-multimodal/bpmn-io__bpmn-js-2396",
            "task_checksum": checksum,
            "config": {"agent": {"name": "kimi-code", "model_name": "k3"}},
            "agent_info": {"name": "kimi-code", "model_info": None},
            "exception_info": exception,
            "verifier_result": None if reward is None else {"rewards": {"reward": reward}},
        }
        (trial / "result.json").write_text(json.dumps(result))

    def test_selects_five_valid_and_excludes_infrastructure_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            first, replacement = root / "first", root / "replacement"
            first.mkdir(); replacement.mkdir()
            for index in range(4):
                self._trial(first, f"task__valid{index}", reward=1.0 if index == 0 else 0.0)
            self._trial(first, "task__rate", reward=None, exception={"type": "ApiRateLimitError"})
            self._trial(replacement, "task__replacement", reward=0.0)
            value = run([first, replacement], root / "audit",
                        instance_id="bpmn-io__bpmn-js-2396", task_checksum=CHECKSUM,
                        agent="kimi-code", model="k3")
            self.assertEqual("complete", value["status"])
            self.assertEqual(5, value["valid_trial_count"])
            self.assertEqual(1, value["success_count"])
            self.assertEqual(4, value["model_failure_count"])
            self.assertEqual(1, value["infrastructure_invalid_count"])
            self.assertTrue((root / "audit" / "pass5_audit.html").is_file())

    def test_pending_trials_require_replacements(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            job = root / "job"; (job / "task__pending").mkdir(parents=True)
            value = run([job], root / "audit", instance_id="bpmn-io__bpmn-js-2396",
                        task_checksum=CHECKSUM, agent="kimi-code", model="k3")
            self.assertEqual("needs_replacement_trials", value["status"])
            self.assertEqual(5, value["replacement_trials_needed"])
            self.assertEqual(1, value["pending_count"])

    def test_malformed_harbor_result_keeps_known_infrastructure_reason(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); job = root / "job"; trial = job / "task__bad"
            trial.mkdir(parents=True)
            (trial / "result.json").write_text("{redacted invalid json")
            (trial / "exception.txt").write_text("harbor.NonZeroAgentExitCodeError: setup failed")
            value = run([job], root / "audit", instance_id="bpmn-io__bpmn-js-2396",
                        task_checksum=CHECKSUM, agent="codex", model="gpt-5.6-luna")
            self.assertIn("NonZeroAgentExitCodeError", value["trials"][0]["reason"])

    def test_corrupted_kimi_wire_is_infrastructure_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); job = root / "job"
            self._trial(job, "task__bad_trace", reward=1.0)
            (job / "task__bad_trace/agent/wire.jsonl").write_text(
                '{"type":"usage","max_completion_tokens":[REDACTED]}\n'
            )
            value = run([job], root / "audit",
                        instance_id="bpmn-io__bpmn-js-2396", task_checksum=CHECKSUM,
                        agent="kimi-code", model="k3")
            self.assertEqual(0, value["valid_trial_count"])
            self.assertEqual(1, value["infrastructure_invalid_count"])
            self.assertEqual("invalid_kimi_wire_trace", value["trials"][0]["reason"])

    def test_bpmn_upstream_trace_is_invalid_answer_leakage(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); job = root / "job"
            self._trial(job, "task__leaked", reward=1.0, tool_calls=[
                ("FetchURL", {
                    "url": "https://raw.githubusercontent.com/bpmn-io/bpmn-js/develop/CHANGELOG.md",
                }),
                ("Bash", {
                    "command": "curl -sL https://github.com/bpmn-io/bpmn-js/compare/v18.13.0...v18.13.1.diff -o /tmp/fix.diff",
                }),
                ("Bash", {
                    "command": "git show HEAD:lib/features/modeling/BpmnLayouter.js",
                }),
                ("Bash", {
                    "command": "wget https://downloads.example.test/releases/v18.13.1/fix.diff",
                }),
                ("FetchURL", {
                    "url": "https://downloads.example.test/tags/v18.13.1/reference-solution.patch",
                }),
            ])
            value = run([job], root / "audit",
                        instance_id="bpmn-io__bpmn-js-2396", task_checksum=CHECKSUM,
                        agent="kimi-code", model="k3")
            trial = value["trials"][0]
            self.assertEqual("invalid_answer_leakage", trial["classification"])
            self.assertFalse(trial["valid"])
            self.assertEqual(0, value["success_count"])
            self.assertEqual(1, value["answer_leakage_invalid_count"])
            self.assertEqual(5, value["replacement_trials_needed"])
            self.assertTrue({
                "source_host_runtime_access", "git_history_access",
                "upstream_artifact_access", "reference_patch_access",
                "remote_url_fetch_call", "unapproved_network_host_access",
            }.issubset({hit["rule"] for hit in trial["answer_leakage_hits"]}))
            self.assertTrue(all("arguments" not in hit for hit in trial["answer_leakage_hits"]))

    def test_model_api_network_call_is_not_answer_leakage(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); job = root / "job"
            self._trial(job, "task__safe", reward=1.0, tool_calls=[
                ("Bash", {
                    "command": "curl https://ark-cn-beijing.bytedance.net/api/v3/chat/completions",
                }),
            ])
            value = run([job], root / "audit",
                        instance_id="bpmn-io__bpmn-js-2396", task_checksum=CHECKSUM,
                        agent="kimi-code", model="k3")
            self.assertEqual("model_success", value["trials"][0]["classification"])
            self.assertEqual(0, value["answer_leakage_invalid_count"])

    def test_kimi_remote_tool_calls_are_leakage_but_definitions_are_not(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); job = root / "job"
            self._trial(job, "task__remote", reward=1.0, tool_calls=[
                ("WebSearch", {"query": "layout algorithm"}),
                ("FetchURL", {"url": "https://example.test/docs"}),
            ])
            self._trial(job, "task__local", reward=1.0, tool_calls=[
                ("Bash", {"command": "npm test"}),
                ("Read", {"path": "/app/src/index.js"}),
                ("ReadMediaFile", {"path": "/app/task/image.png"}),
            ])
            safe_wire = job / "task__local/agent/wire.jsonl"
            safe_wire.write_text(
                json.dumps({
                    "type": "llm.tools_snapshot",
                    "tools": [{"name": "WebSearch"}, {"name": "FetchURL"}],
                }) + "\n" + safe_wire.read_text()
            )
            value = run([job], root / "audit",
                        instance_id="bpmn-io__bpmn-js-2396", task_checksum=CHECKSUM,
                        agent="kimi-code", model="k3")
            records = {record["trial"]: record for record in value["trials"]}
            remote, local = records["task__remote"], records["task__local"]
            self.assertEqual("invalid_answer_leakage", remote["classification"])
            self.assertTrue(
                {"remote_web_search_call", "remote_url_fetch_call"}.issubset(
                    {hit["rule"] for hit in remote["answer_leakage_hits"]}))
            self.assertEqual("model_success", local["classification"])

    def test_codex_atif_remote_tools_are_answer_leakage(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); job = root / "job"; trial = job / "task__codex_remote"
            (trial / "agent").mkdir(parents=True)
            (trial / "agent" / "trajectory.json").write_text(json.dumps({
                "schema_version": "ATIF-v1.7",
                "agent": {"tool_definitions": [{"name": "web_search"}]},
                "steps": [{
                    "step_id": 1, "source": "agent", "message": "remote calls",
                    "tool_calls": [
                        {"function_name": "browser_use", "arguments": {"url": "https://example.test"}},
                        {"function_name": "file_search", "arguments": {"query": "answer"}},
                        {"function_name": "mcp__github__get_file", "arguments": {"path": "fix.js"}},
                        {"function_name": "connector.search", "arguments": {"query": "fix"}},
                    ],
                }],
            }))
            calls = [
                ("browser_use", {"url": "https://example.test"}),
                ("file_search", {"query": "answer"}),
                ("mcp__github__get_file", {"path": "fix.js"}),
                ("connector.search", {"query": "fix"}),
            ]
            self._codex_rollout(trial, calls)
            (trial / "result.json").write_text(json.dumps({
                "task_name": "swe-bench-multimodal/bpmn-io__bpmn-js-2396",
                "task_checksum": CHECKSUM,
                "config": {"agent": {"name": "codex", "model_name": "gpt-5.6-luna"}},
                "agent_info": {"name": "codex", "model_info": {"name": "gpt-5.6-luna"}},
                "exception_info": None,
                "verifier_result": {"rewards": {"reward": 1.0}},
            }))
            value = run([job], root / "audit",
                        instance_id="bpmn-io__bpmn-js-2396", task_checksum=CHECKSUM,
                        agent="codex", model="gpt-5.6-luna")
            record = value["trials"][0]
            self.assertEqual("invalid_answer_leakage", record["classification"])
            self.assertEqual(0, value["success_count"])
            self.assertTrue({
                "remote_browser_call", "remote_file_search_call",
                "remote_mcp_call", "remote_connector_call",
            }.issubset({hit["rule"] for hit in record["answer_leakage_hits"]}))

    def test_codex_responses_hosted_call_events_are_answer_leakage(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); job = root / "job"; trial = job / "task__responses_remote"
            (trial / "agent/sessions").mkdir(parents=True)
            (trial / "agent" / "trajectory.json").write_text(json.dumps({
                "schema_version": "ATIF-v1.7",
                "steps": [{
                    "step_id": 1, "source": "agent", "message": "remote",
                    "tool_calls": [
                        {"function_name": "web_search_call", "arguments": {"query": "fix"}},
                        {"function_name": "file_search_call", "arguments": {"query": "gold"}},
                        {"function_name": "mcp_call", "arguments": {"server_label": "github"}},
                        {"function_name": "computer_call", "arguments": {"action": "open"}},
                    ],
                }],
            }))
            records = [
                {"type": "session_meta", "payload": {
                    "tools": [{"type": "web_search"}, {"type": "file_search"}],
                }},
                {"type": "turn_context", "payload": {"turn_id": "turn-test"}},
                {"type": "response_item", "payload": {"type": "web_search_call", "query": "fix"}},
                {"type": "response_item", "payload": {"type": "file_search_call", "query": "gold"}},
                {"type": "response_item", "payload": {"type": "mcp_call", "server_label": "github"}},
                {"type": "response_item", "payload": {"type": "computer_call", "action": "open"}},
            ]
            (trial / "agent/sessions/rollout-test.jsonl").write_text(
                "".join(json.dumps(record) + "\n" for record in records)
            )
            (trial / "result.json").write_text(json.dumps({
                "task_name": "swe-bench-multimodal/bpmn-io__bpmn-js-2396",
                "task_checksum": CHECKSUM,
                "config": {"agent": {"name": "codex", "model_name": "gpt-5.6-luna"}},
                "agent_info": {"name": "codex", "model_info": {"name": "gpt-5.6-luna"}},
                "exception_info": None,
                "verifier_result": {"rewards": {"reward": 1.0}},
            }))
            value = run([job], root / "audit",
                        instance_id="bpmn-io__bpmn-js-2396", task_checksum=CHECKSUM,
                        agent="codex", model="gpt-5.6-luna")
            record = value["trials"][0]
            self.assertEqual("invalid_answer_leakage", record["classification"])
            self.assertEqual({
                "remote_web_search_call", "remote_file_search_call",
                "remote_mcp_call", "remote_browser_call",
            }, {hit["rule"] for hit in record["answer_leakage_hits"]})

    def test_codex_atif_reference_patch_read_is_answer_leakage(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); job = root / "job"; trial = job / "task__codex_leaked"
            (trial / "agent").mkdir(parents=True)
            (trial / "agent" / "trajectory.json").write_text(json.dumps({
                "schema_version": "ATIF-v1.7",
                "steps": [{
                    "step_id": 1,
                    "source": "agent",
                    "message": "inspect",
                    "tool_calls": [{
                        "function_name": "exec_command",
                        "arguments": {"cmd": "sed -n '1,80p' /app/task/solution/gold.patch"},
                    }],
                }],
            }))
            self._codex_rollout(trial, [("exec_command", {
                "cmd": "sed -n '1,80p' /app/task/solution/gold.patch",
            })])
            (trial / "result.json").write_text(json.dumps({
                "task_name": "swe-bench-multimodal/bpmn-io__bpmn-js-2396",
                "task_checksum": CHECKSUM,
                "config": {"agent": {"name": "codex", "model_name": "gpt-5.6-luna"}},
                "agent_info": {
                    "name": "codex", "model_info": {"name": "gpt-5.6-luna"},
                },
                "exception_info": None,
                "verifier_result": {"rewards": {"reward": 1.0}},
            }))
            value = run([job], root / "audit",
                        instance_id="bpmn-io__bpmn-js-2396", task_checksum=CHECKSUM,
                        agent="codex", model="gpt-5.6-luna")
            trial_record = value["trials"][0]
            self.assertEqual("invalid_answer_leakage", trial_record["classification"])
            self.assertEqual(
                ["reference_patch_access"],
                [hit["rule"] for hit in trial_record["answer_leakage_hits"]],
            )

    def test_codex_missing_corrupt_or_inconsistent_raw_rollout_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); job = root / "job"; trial = job / "task__codex"
            (trial / "agent/sessions").mkdir(parents=True)
            (trial / "agent/trajectory.json").write_text(json.dumps({
                "schema_version": "ATIF-v1.7", "steps": [{
                    "step_id": 1, "source": "agent", "message": "local",
                    "tool_calls": [{"function_name": "exec_command",
                                    "arguments": {"cmd": "npm test"}}],
                }],
            }))
            (trial / "result.json").write_text(json.dumps({
                "task_name": "swe-bench-multimodal/bpmn-io__bpmn-js-2396",
                "task_checksum": CHECKSUM,
                "config": {"agent": {"name": "codex", "model_name": "gpt-5.6-luna"}},
                "agent_info": {"name": "codex", "model_info": {"name": "gpt-5.6-luna"}},
                "exception_info": None, "verifier_result": {"rewards": {"reward": 1.0}},
            }))
            missing = run([job], root / "missing",
                          instance_id="bpmn-io__bpmn-js-2396", task_checksum=CHECKSUM,
                          agent="codex", model="gpt-5.6-luna")["trials"][0]
            self.assertEqual("missing_codex_raw_rollout", missing["reason"])

            rollout = trial / "agent/sessions/rollout-test.jsonl"
            rollout.write_text("{corrupt\n")
            corrupt = run([job], root / "corrupt",
                          instance_id="bpmn-io__bpmn-js-2396", task_checksum=CHECKSUM,
                          agent="codex", model="gpt-5.6-luna")["trials"][0]
            self.assertEqual("invalid_codex_raw_rollout", corrupt["reason"])

            self._codex_rollout(trial, [("exec_command", {"cmd": "npm run other"})])
            mismatch = run([job], root / "mismatch",
                           instance_id="bpmn-io__bpmn-js-2396", task_checksum=CHECKSUM,
                           agent="codex", model="gpt-5.6-luna")["trials"][0]
            self.assertEqual("codex_atif_raw_call_mismatch", mismatch["reason"])

    def test_indirect_source_hosts_unknown_calls_and_more_git_history_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); job = root / "job"
            self._trial(job, "task__bypass", reward=1.0, tool_calls=[
                ("Bash", {"command": "wget https://api.github.com/repos/o/r/commits/main"}),
                ("Bash", {"command": "git -C /app cat-file -p HEAD"}),
                ("MysteryLocalCall", {"path": "/app"}),
            ])
            record = run([job], root / "audit",
                         instance_id="bpmn-io__bpmn-js-2396", task_checksum=CHECKSUM,
                         agent="kimi-code", model="k3")["trials"][0]
            rules = {hit["rule"] for hit in record["answer_leakage_hits"]}
            self.assertEqual("invalid_answer_leakage", record["classification"])
            self.assertTrue({"source_host_runtime_access", "git_history_access",
                             "unapproved_tool_call"}.issubset(rules))

    def test_unknown_codex_call_like_event_is_leakage_even_if_atif_omits_it(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); job = root / "job"; trial = job / "task__unknown"
            (trial / "agent/sessions").mkdir(parents=True)
            (trial / "agent/trajectory.json").write_text(json.dumps({
                "schema_version": "ATIF-v1.7", "steps": [{
                    "step_id": 1, "source": "agent", "message": "done",
                    "tool_calls": [],
                }],
            }))
            records = [
                {"type": "session_meta", "payload": {"id": "s"}},
                {"type": "turn_context", "payload": {"turn_id": "t"}},
                {"type": "response_item", "payload": {
                    "type": "future_remote_call", "query": "answer"}},
            ]
            (trial / "agent/sessions/rollout-test.jsonl").write_text(
                "".join(json.dumps(record) + "\n" for record in records))
            (trial / "result.json").write_text(json.dumps({
                "task_name": "swe-bench-multimodal/bpmn-io__bpmn-js-2396",
                "task_checksum": CHECKSUM,
                "config": {"agent": {"name": "codex", "model_name": "gpt-5.6-luna"}},
                "agent_info": {"name": "codex", "model_info": {"name": "gpt-5.6-luna"}},
                "exception_info": None, "verifier_result": {"rewards": {"reward": 1.0}},
            }))
            record = run([job], root / "audit",
                         instance_id="bpmn-io__bpmn-js-2396", task_checksum=CHECKSUM,
                         agent="codex", model="gpt-5.6-luna")["trials"][0]
            self.assertEqual("invalid_answer_leakage", record["classification"])
            self.assertIn("unapproved_call_event",
                          {hit["rule"] for hit in record["answer_leakage_hits"]})


if __name__ == "__main__":
    unittest.main()
