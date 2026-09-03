import importlib.util
import copy
import json
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("05_verify_freeze.py")
SPEC = importlib.util.spec_from_file_location("verify_freeze", MODULE_PATH)
assert SPEC and SPEC.loader
VERIFY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFY)


class FreezeVerificationTest(unittest.TestCase):
    def test_committed_manifest(self) -> None:
        self.assertEqual(VERIFY.main(), 0)

    def test_secret_patterns_reject_values(self) -> None:
        patterns = [
            r"(?i)(api[_-]?key|access[_-]?token|secret)[\"']?\s*[:=]\s*[\"'][^\"']+",
            r"sk-[A-Za-z0-9_-]{16,}",
            r"AIza[0-9A-Za-z_-]{20,}",
        ]
        samples = ['"api_key": "not-a-real-value"', "sk-abcdefghijklmnop", "AIzaabcdefghijklmnopqrst"]
        for pattern, sample in zip(patterns, samples, strict=True):
            self.assertRegex(sample, pattern)

    def test_complete_state_cannot_bypass_pending_bindings(self) -> None:
        base = json.loads(VERIFY.MANIFEST.read_text(encoding="utf-8"))
        base["freeze_status"] = "complete"
        base["open_items"] = []
        cases = [
            ("dependencies", "formal_pipeline"),
            ("harbor", "selected_visual_task"),
            ("docker", "selected_task_image"),
            ("models", "coding_agent"),
            ("runtime_policy", "selected_task_limits"),
        ]
        for group, item in cases:
            candidate = copy.deepcopy(base)
            candidate[group][item]["state"] = "pending"
            with self.subTest(group=group, item=item), self.assertRaises(ValueError):
                VERIFY.validate_data(candidate, verify_files=False)

    def test_schema_is_enforced(self) -> None:
        data = json.loads(VERIFY.MANIFEST.read_text(encoding="utf-8"))
        data["unexpected"] = True
        with self.assertRaises(ValueError):
            VERIFY.validate_data(data, verify_files=False)

    def test_selected_task_material_change_invalidates_old_controls(self) -> None:
        data = json.loads(VERIFY.MANIFEST.read_text(encoding="utf-8"))
        task = data["harbor"]["selected_visual_task"]
        image = data["docker"]["selected_task_image"]
        self.assertEqual("material_changed_controls_pending", task["state"])
        self.assertEqual("7a09fc4066c86f8b6df96d1b692cbd9a4daed3219d54889b2978154f1b09499e",
                         task["task_directory_checksum"])
        self.assertNotEqual("c1a6b2890708291adc14fcbd3cedb118d8ed628fdbb26c54f66fd8845b0e6402",
                            task["task_directory_checksum"])
        self.assertEqual("historical_checksum_rerun_required", task["control_evidence"]["status"])
        self.assertEqual(0, task["control_evidence"]["controls"])
        self.assertEqual("rebuild_required_after_dockerfile_change", image["state"])
        self.assertRegex(image["produced_image_id"], r"^sha256:[0-9a-f]{64}$")

    def test_nonempty_placeholders_cannot_complete_freeze(self) -> None:
        data = json.loads(VERIFY.MANIFEST.read_text(encoding="utf-8"))
        data["freeze_status"] = "complete"
        data["open_items"] = []
        data["dependencies"]["formal_pipeline"].update(state="realtime_verified", hash_locked_resolution_present=True)
        data["harbor"]["installed_package"]["source_commit_independently_bound"] = True
        data["harbor"]["source_revision"]["commit"] = "x"
        data["harbor"]["selected_visual_task"].update(
            state="realtime_verified", task_directory_checksum="x",
            task_schema_compatibility_verified=True, agent_command="x", verifier_command="x")
        data["docker"]["daemon"]["state"] = "realtime_verified"
        data["docker"]["selected_task_image"].update(
            state="realtime_verified", base_digest="x", produced_image_id="x",
            produced_repo_digest="x", architecture="x", build_command="x", offline_archive_sha256="x")
        data["models"]["coding_agent"].update(
            state="realtime_verified", provider="x", model_id="x", agent="x", agent_version="x",
            instruction_path="x", instruction_sha256="x", sampling="x", tool_policy="x", budget="x",
            timeout_sec="x", expected_external_calls="x", authorization_record="x")
        data["runtime_policy"]["selected_task_limits"]["state"] = "realtime_verified"
        with self.assertRaises(ValueError):
            VERIFY.validate_data(data, verify_files=False)

    def test_well_formed_but_unbound_entities_cannot_complete_freeze(self) -> None:
        data = json.loads(VERIFY.MANIFEST.read_text(encoding="utf-8"))
        data["freeze_status"] = "complete"
        data["open_items"] = []
        zeros = "0" * 64
        data["dependencies"]["formal_pipeline"].update(
            state="realtime_verified", hash_locked_resolution_present=True,
            lock_evidence={"path": "does/not/exist", "sha256": zeros})
        data["harbor"]["installed_package"]["source_commit_independently_bound"] = True
        data["harbor"]["selected_visual_task"].update(
            state="realtime_verified", task_directory_checksum=zeros,
            task_schema_compatibility_verified=True, agent_command=["agent"], verifier_command=["verify"],
            task_inventory={"path": "does/not/exist", "sha256": zeros})
        data["docker"]["daemon"]["state"] = "realtime_verified"
        data["docker"]["selected_task_image"].update(
            state="realtime_verified", base_digest="sha256:" + zeros,
            produced_image_id="sha256:" + zeros, produced_repo_digest="sha256:" + zeros,
            architecture="linux/arm64", build_command=["docker", "build"],
            offline_archive_sha256=zeros, offline_archive={"path": "does/not/exist", "sha256": zeros},
            inspection_evidence={"path": "does/not/exist", "sha256": zeros})
        data["models"]["coding_agent"].update(
            state="realtime_verified", provider="gemini", model_id="model", agent="kimi-code", agent_version="1",
            instruction_path="does/not/exist", instruction_sha256=zeros, sampling={"temperature": 0},
            tool_policy="fixed", budget={"tokens": 1}, timeout_sec=1, expected_external_calls=5,
            authorization_record={"path": "does/not/exist", "sha256": zeros, "authorization_id": "auth"})
        data["runtime_policy"]["selected_task_limits"]["state"] = "realtime_verified"
        with self.assertRaises(ValueError):
            VERIFY.validate_data(data, verify_files=True)

    def test_authorization_binds_exact_task_and_run_config(self) -> None:
        zeros = "0" * 64
        task = {"task_directory_checksum": zeros, "task_inventory": {"sha256": "1" * 64},
                "agent_command": ["agent"], "verifier_command": ["verify"]}
        agent = {"authorization_record": {"authorization_id": "auth"}, "model_id": "model",
                 "agent": "kimi-code", "agent_version": "1", "instruction_sha256": "2" * 64,
                 "sampling": {"temperature": 0}, "budget": {"tokens": 10}, "tool_policy": "fixed",
                 "timeout_sec": 60, "trial_count": 5, "expected_external_calls": 5}
        record = {"authorized": True, "authorization_id": "auth", "task_directory_checksum": zeros,
                  "task_inventory_sha256": "1" * 64, "model_id": "model", "agent": "kimi-code",
                  "agent_version": "1", "instruction_sha256": "2" * 64, "sampling": {"temperature": 0},
                  "budget": {"tokens": 10}, "tool_policy": "fixed", "timeout_sec": 60,
                  "trial_count": 5, "expected_external_calls": 5,
                  "agent_command": ["agent"], "verifier_command": ["verify"]}
        VERIFY.validate_authorization_values(record, task, agent)
        record["task_directory_checksum"] = "3" * 64
        with self.assertRaises(ValueError):
            VERIFY.validate_authorization_values(record, task, agent)


if __name__ == "__main__":
    unittest.main()
