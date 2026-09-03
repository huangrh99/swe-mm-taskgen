import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from report_pipeline import harbor_export as harbor_export_module
from report_pipeline.harbor_export import export
from report_pipeline.paths import REPORT_ROOT, TMP_ROOT

ORIGINAL_RENAME = Path.rename


class HarborExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=TMP_ROOT)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"; self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "a@b.c"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "T"], check=True)
        (self.repo / "x.scss").write_text("old\nkeep\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "x.scss"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "base"], check=True)
        self.base = subprocess.check_output(["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True).strip()
        (self.repo / "x.scss").write_text("new\nkeep\n")
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qam", "fix"], check=True)
        self.ref = subprocess.check_output(["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True).strip()
        subprocess.run(["git", "-C", str(self.repo), "checkout", "-q", self.base], check=True)
        self.asset = self.root / "asset"; self.asset.write_bytes(b"png")
        sha = hashlib.sha256(b"png").hexdigest()
        bindings = {}
        for name in ("archive", "verifier", "packet", "curator_assets"):
            path = self.root / f"{name}.json"; path.write_text("{}\n")
            bindings[f"{name}_path"] = str(path)
            bindings[f"{name}_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.dossier = self.root / "dossier.json"
        self.dossier.write_text(json.dumps({
            "candidate_id": "o__r-1", "status": "admitted_to_test_construction", "repository": "o/r", "pr_number": 1,
            "git": {"baseline_sha": self.base, "reference_sha": self.ref},
            "changed_files": [{"filename": "x.scss"}],
            "visual_admission": {"human_calibration_state": "pending", "decision": "auto_admit_high_confidence_verifier"},
            "test_calibration": {"human_semantic_calibration_state": "pending"}, "source_bindings": bindings,
            "leakage_policy": {"safe_agent_assets": [{"asset_id": sha, "sha256": sha, "status": "available",
                "local_path": str(self.asset), "source_ids": ["o/r#2:body"]}],
                "safe_agent_source_ids": ["o/r#2:title", "o/r#2:body"]}}))
        self.tests = self.root / "tests.json"
        self.tests.write_text(json.dumps({"candidate_id": "o__r-1", "tests": [
            {"test_id": "f", "class": "F2P", "path": "x.scss", "expected_transition": "fail->pass", "contains_all": ["new"], "contains_none": []},
            {"test_id": "p", "class": "P2P", "path": "x.scss", "expected_transition": "pass->pass", "contains_all": ["keep"], "contains_none": []}]}))
        self.measurement = self.root / "measurement.json"
        self.measurement.write_text(json.dumps({"all_transitions_match": True, "semantic_calibration": "pending_human_review", "transitions": [
            {"test_id": "f"}, {"test_id": "p"}]}))
        self.instruction = self.root / "instruction.md"; self.instruction.write_text(
            "Fix it; see /visual_context/asset.png and edit /testbed.\n")
        self.rebuild_patcher = patch("report_pipeline.harbor_export._rebuild_candidate")
        self.rebuild = self.rebuild_patcher.start()
        self.rebuild.side_effect = lambda bindings: json.loads(self.dossier.read_text())

    def tearDown(self):
        self.rebuild_patcher.stop()
        self.tmp.cleanup()

    def test_git_diff_preserves_trailing_blank_context_line(self):
        repo = self.root / "trailing-context-repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "a@b.c"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
        target = repo / "sample.txt"
        target.write_text("before\n\n")
        subprocess.run(["git", "-C", str(repo), "add", "sample.txt"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
        base = subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
        target.write_text("after\n\n")
        subprocess.run(["git", "-C", str(repo), "commit", "-qam", "reference"], check=True)
        reference = subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
        patch_text = harbor_export_module._git_diff_preserve(
            repo, base, reference, ["sample.txt"])
        self.assertTrue(patch_text.endswith(" \n"))
        subprocess.run(["git", "-C", str(repo), "checkout", "-q", base], check=True)
        checked = subprocess.run(
            ["git", "-C", str(repo), "apply", "--check", "-"],
            input=patch_text, text=True, capture_output=True, check=False)
        self.assertEqual(checked.returncode, 0, checked.stderr)

    def _fake_command(self, args, image_char="a"):
        if args[0] == "git":
            return subprocess.run(args, text=True, capture_output=True, check=True).stdout.strip()
        if args[:3] == ("docker", "image", "inspect"):
            return json.dumps([{
                "Id": "sha256:" + image_char * 64,
                "RepoDigests": ["visual-harbor-base@sha256:" + image_char * 64],
            }])
        if args[:2] == ("docker", "run"):
            if args[-2:] == ("rev-parse", "HEAD"):
                return "c" * 40
            if args[-2:] == ("rev-parse", "HEAD^{tree}"):
                return subprocess.check_output(
                    ["git", "-C", str(self.repo), "rev-parse", "HEAD^{tree}"], text=True
                ).strip()
            return ""
        if args[:2] == ("docker", "tag"):
            return ""
        raise AssertionError(args)

    @patch("report_pipeline.harbor_export._run")
    def test_deterministic_export_and_leakage_boundary(self, run):
        def fake(*args):
            return self._fake_command(args)
        run.side_effect = fake
        first = export(self.dossier, self.tests, self.measurement, self.repo, self.instruction, "base", self.root / "one")
        second = export(self.dossier, self.tests, self.measurement, self.repo, self.instruction, "base", self.root / "two")
        self.assertEqual(first["task_material_sha256"], second["task_material_sha256"])
        self.assertEqual(first["files"], second["files"])
        self.assertFalse((self.root / "one/export_manifest.json").exists())
        self.assertTrue((self.root / "one.export_manifest.json").is_file())
        self.assertTrue((self.root / "two.export_manifest.json").is_file())
        self.assertTrue((self.root / "one.export_manifest.json.commit.json").is_file())
        self.assertTrue((self.root / "two.export_manifest.json.commit.json").is_file())
        self.assertFalse((self.root / ".one.export_manifest.json.transaction.json").exists())
        self.assertFalse((self.root / ".two.export_manifest.json.transaction.json").exists())
        self.assertEqual(first["calibration"]["visual_necessity_human"], "pending")
        self.assertNotIn("gold.patch", (self.root / "one/instruction.md").read_text())
        inventory = json.loads((self.root / "one/tests/frozen_inventory.json").read_text())
        self.assertEqual(inventory["expected_tests"], [
            {"test_id": "f", "class": "F2P"}, {"test_id": "p", "class": "P2P"}])
        self.assertEqual(inventory["test_manifest_sha256"], hashlib.sha256(
            (self.root / "one/tests/test_manifest.json").read_bytes()).hexdigest())
        self.assertIn("frozen_test_tamper", (self.root / "one/tests/integrity.py").read_text())
        self.assertIn("required_test_disabled", (self.root / "one/tests/sweb_grade.py").read_text())
        self.assertTrue((self.root / "one/tests/test.patch").is_file())
        dockerfile = (self.root / "one/environment/Dockerfile").read_text()
        self.assertIn("USER 10001:10001", dockerfile)
        self.assertIn("--create-home benchmark", dockerfile)
        self.assertIn("/home/benchmark", dockerfile)
        self.assertIn("mv /app /testbed", dockerfile)
        self.assertIn("rm -rf /testbed/.git && git init /testbed", dockerfile)
        self.assertIn("git -C /testbed config gc.auto 0", dockerfile)
        self.assertIn("git -C /testbed commit -q", dockerfile)
        self.assertIn("/opt/benchmark-baseline-sha", dockerfile)
        self.assertIn("git -C /testbed gc --prune=now", dockerfile)
        self.assertIn("collect-agent-patch", dockerfile)
        collector = self.root / "one/environment/collect-agent-patch"
        self.assertEqual(subprocess.run(["bash", "-n", str(collector)], check=False).returncode, 0)
        self.assertIn("/opt/benchmark-transport/agent.patch", collector.read_text())
        self.assertIn("/opt/benchmark-transport/status", collector.read_text())
        terminator = self.root / "one/environment/terminate-agent-processes"
        self.assertEqual(subprocess.run(["bash", "-n", str(terminator)], check=False).returncode, 0)
        self.assertIn('kill -"$signal"', terminator.read_text())
        self.assertIn("user = 10001", (self.root / "one/task.toml").read_text())
        self.assertNotIn("kill -KILL", collector.read_text())
        self.assertIn('"$state" = Z', collector.read_text())

        self.assertNotIn("kill -STOP", collector.read_text())
        self.assertIn('[[ "$path" = assets/* ]] && continue', collector.read_text())
        self.assertIn("COPY --chown=10001:10001 assets /testbed/assets", dockerfile)
        self.assertIn("FROM visual-harbor-base:", dockerfile)
        base_binding = json.loads((self.root / "one/environment/base_image.json").read_text())
        self.assertEqual(base_binding["image_id"], "sha256:" + "a" * 64)
        self.assertIn('/usr/bin/python3 -I', (self.root / "one/tests/test.sh").read_text())
        compose = (self.root / "one/environment/docker-compose.yaml").read_text()
        task_config = (self.root / "one/task.toml").read_text()
        self.assertNotIn("network_mode:", compose)
        self.assertIn('schema_version = "1.2"', task_config)
        self.assertIn('artifacts = ["/opt/benchmark-transport"]', task_config)
        self.assertIn('[environment]\nbuild_timeout_sec = 1800.0', task_config)
        self.assertIn('allow_internet = false', task_config)
        self.assertIn('[agent]\ntimeout_sec = 7200.0', task_config)
        self.assertIn('[verifier]\ntimeout_sec = 3600.0', task_config)
        self.assertIn('environment_mode = "separate"', task_config)
        self.assertIn('[[verifier.collect]]', task_config)
        self.assertEqual(task_config.count('[[verifier.collect]]'), 2)
        self.assertIn('command = "/usr/local/bin/terminate-agent-processes"', task_config)
        self.assertIn('command = "/usr/local/bin/collect-agent-patch"', task_config)
        self.assertIn('user = 0', task_config)
        self.assertNotIn("network_mode", task_config)
        self.assertIn("/testbed/assets/asset.png", (self.root / "one/instruction.md").read_text())
        config = json.loads((self.root / "one/tests/config.json").read_text())
        self.assertEqual(config["repo"], "o/r")
        self.assertEqual(config["FAIL_TO_PASS"], ["f"])
        self.assertEqual(config["PASS_TO_PASS"], ["p"])
        self.assertEqual(inventory["forbidden_git_commits"], sorted({self.base, self.ref}))
        verifier = (self.root / "one/tests/sweb_grade.py").read_text()
        self.assertIn("source_git_history_leak", verifier)
        self.assertIn("source_git_remote_leak", verifier)
        self.assertIn("source_git_remote_check_failed", verifier)
        self.assertIn("safe.directory=", verifier)
        self.assertIn("os.chmod(logs, 0o700)", verifier)
        self.assertIn("quiesce_uid(10002)", verifier)
        self.assertNotIn("os.setuid(10002)", verifier)
        verifier_dockerfile = (self.root / "one/tests/Dockerfile").read_text()
        self.assertIn("FROM visual-harbor-base:", verifier_dockerfile)

        self.assertIn("mv /app /testbed", verifier_dockerfile)
        self.assertIn("COPY . /tests", verifier_dockerfile)
        self.assertIn("ln -s /testbed /app", verifier_dockerfile)
        self.assertNotIn("chown -R 10002:10002 /testbed", verifier_dockerfile)
        self.assertIn("find /tests -type d -exec chmod 0700", verifier_dockerfile)
        self.assertIn("find /tests -type f -exec chmod 0400", verifier_dockerfile)
        self.assertIn("USER 0:0", verifier_dockerfile)
        self.assertIn('file_count=$((file_count + 1))', collector.read_text())
        self.assertIn("git diff --name-only -z", collector.read_text())
        self.assertIn("':(exclude)assets/**'", collector.read_text())
        self.assertIn("268435456", collector.read_text())

    def test_export_cannot_publish_directly_into_formal_report_root(self):
        with self.assertRaisesRegex(ValueError, "provisional task under tmp"):
            export(self.dossier, self.tests, self.measurement, self.repo,
                   self.instruction, "base", REPORT_ROOT / "o__r-1")

    @patch("report_pipeline.harbor_export._run")
    def test_failed_export_never_publishes_partial_task_and_can_retry(self, run):
        run.side_effect = RuntimeError("docker unavailable")
        output = self.root / "atomic"
        with self.assertRaisesRegex(RuntimeError, "docker unavailable"):
            export(self.dossier, self.tests, self.measurement, self.repo,
                   self.instruction, "base", output)
        self.assertFalse(output.exists())
        self.assertFalse((self.root / "atomic.export_manifest.json").exists())

        run.side_effect = lambda *args: self._fake_command(args)
        result = export(self.dossier, self.tests, self.measurement, self.repo,
                        self.instruction, "base", output)
        self.assertEqual(result["status"], "exported_pending_harbor_controls")
        self.assertTrue(output.is_dir())

    @patch("report_pipeline.harbor_export.Path.rename", autospec=True)
    @patch("report_pipeline.harbor_export._run")
    def test_publish_failure_rolls_back_sidecar_and_never_exposes_task(self, run, rename):
        run.side_effect = lambda *args: self._fake_command(args)

        def fail_task_publish(source, target):
            if source.name == ".publish-failure.staging":
                raise OSError("injected task publish failure")
            return ORIGINAL_RENAME(source, target)

        rename.side_effect = fail_task_publish
        output = self.root / "publish-failure"
        with self.assertRaisesRegex(OSError, "injected task publish failure"):
            export(self.dossier, self.tests, self.measurement, self.repo,
                   self.instruction, "base", output)
        self.assertFalse(output.exists())
        self.assertFalse((self.root / "publish-failure.export_manifest.json").exists())
        self.assertFalse((self.root / ".publish-failure.staging").exists())
        self.assertFalse((self.root / ".publish-failure.export_manifest.json.transaction.json").exists())

    @patch("report_pipeline.harbor_export._run")
    def test_interrupted_commit_is_hash_bound_and_retry_recovers(self, run):
        run.side_effect = lambda *args: self._fake_command(args)
        output = self.root / "crash-recovery"
        original_atomic_json = harbor_export_module._atomic_json

        def interrupt_commit(path, value):
            if value.get("schema_version") == "visual-harbor-export-commit-v1":
                raise KeyboardInterrupt("simulated process interruption")
            return original_atomic_json(path, value)

        with patch("report_pipeline.harbor_export._atomic_json",
                   side_effect=interrupt_commit):
            with self.assertRaisesRegex(KeyboardInterrupt, "simulated process interruption"):
                export(self.dossier, self.tests, self.measurement, self.repo,
                       self.instruction, "base", output)

        sidecar = self.root / "crash-recovery.export_manifest.json"
        transaction = self.root / ".crash-recovery.export_manifest.json.transaction.json"
        commit = self.root / "crash-recovery.export_manifest.json.commit.json"
        self.assertTrue(output.is_dir())
        self.assertTrue(sidecar.is_file())
        self.assertTrue(transaction.is_file())
        self.assertFalse(commit.exists())

        result = export(self.dossier, self.tests, self.measurement, self.repo,
                        self.instruction, "base", output)
        self.assertEqual(result["status"], "exported_pending_harbor_controls")
        self.assertTrue(output.is_dir())
        self.assertTrue(sidecar.is_file())
        self.assertTrue(commit.is_file())
        self.assertFalse(transaction.exists())

    def test_existing_sidecar_fails_before_creating_output(self):
        output = self.root / "blocked"
        sidecar = self.root / "blocked.export_manifest.json"
        sidecar.write_text("{}\n")
        with self.assertRaisesRegex(ValueError, "sidecar already exists"):
            export(self.dossier, self.tests, self.measurement, self.repo,
                   self.instruction, "base", output)
        self.assertFalse(output.exists())

    @patch("report_pipeline.harbor_export._run")
    def test_rejects_dossier_when_rebuilt_admission_is_downgraded(self, run):
        run.side_effect = lambda *args: self._fake_command(args)
        rebuilt = json.loads(self.dossier.read_text())
        rebuilt["status"] = "review_or_exclude"
        rebuilt["visual_admission"]["decision"] = "not_auto_admitted"
        self.rebuild.side_effect = None
        self.rebuild.return_value = rebuilt
        with self.assertRaisesRegex(
                ValueError, "dossier differs from source-derived candidate: status"):
            export(self.dossier, self.tests, self.measurement, self.repo,
                   self.instruction, "base", self.root / "downgraded")

    @patch("report_pipeline.harbor_export._run")
    def test_functional_runner_is_bound_and_becomes_reward_scope(self, run):
        def fake(*args):
            return self._fake_command(args, "b")
        run.side_effect = fake
        manifest = json.loads(self.tests.read_text())
        manifest["execution"] = {"kind": "command_json_v1",
                                 "command": ["/usr/bin/python3", "-I", "/tests/functional_runner.py"]}
        for test in manifest["tests"]:
            test["kind"] = "functional_result"
        self.tests.write_text(json.dumps(manifest))
        measurement = json.loads(self.measurement.read_text())
        measurement["oracle_kind"] = "chromium_computed_style"
        self.measurement.write_text(json.dumps(measurement))
        runner = self.root / "runner.py"; runner.write_text("print('{}')\n")
        result = export(self.dossier, self.tests, self.measurement, self.repo, self.instruction,
                        "base", self.root / "functional", runner)
        self.assertIn("real Chromium computed-style reward", result["test_scope"])
        self.assertEqual((self.root / "functional/tests/functional_runner.py").read_bytes(),
                         runner.read_bytes())
        verifier = (self.root / "functional/tests/sweb_grade.py").read_text()
        self.assertIn("functional_result_inventory_mismatch", verifier)
        self.assertIn("functional_assertion_mismatch", verifier)

        with self.assertRaisesRegex(ValueError, "functional runner"):
            export(self.dossier, self.tests, self.measurement, self.repo, self.instruction,
                   "base", self.root / "missing-runner")

    @patch("report_pipeline.harbor_export._run")
    def test_hidden_functional_payload_is_copied_and_integrity_bound(self, run):
        run.side_effect = lambda *args: self._fake_command(args, "d")
        manifest = json.loads(self.tests.read_text())
        manifest["execution"] = {"kind": "command_json_v1",
                                 "command": ["/usr/bin/python3", "-I", "/tests/functional_runner.py"]}
        for test in manifest["tests"]:
            test["kind"] = "functional_result"
        self.tests.write_text(json.dumps(manifest))
        runner = self.root / "runner.py"; runner.write_text("print('{}')\n")
        payload = self.root / "payload"; (payload / "nested").mkdir(parents=True)
        (payload / "nested/case.js").write_text("export default true;\n")

        result = export(self.dossier, self.tests, self.measurement, self.repo,
                        self.instruction, "base", self.root / "payload-task", runner,
                        payload)

        copied = self.root / "payload-task/tests/payload/nested/case.js"
        self.assertEqual(copied.read_bytes(), (payload / "nested/case.js").read_bytes())
        self.assertIn("hidden executable browser/rendering tests", result["test_scope"])
        self.assertEqual(result["source_bindings"]["functional_runner_sha256"],
                         hashlib.sha256(runner.read_bytes()).hexdigest())
        self.assertEqual(result["source_bindings"]["test_payload_files"], [{
            "path": "nested/case.js",
            "sha256": hashlib.sha256((payload / "nested/case.js").read_bytes()).hexdigest(),
            "size_bytes": len((payload / "nested/case.js").read_bytes()),
        }])
        launcher = (self.root / "payload-task/tests/test.sh").read_text()
        self.assertIn("/tests/integrity_manifest.json", launcher)
        self.assertIn("git -C /testbed apply", launcher)
        self.assertIn("nested/case.js", (self.root / "payload-task/tests/test.patch").read_text())
        integrity_manifest = json.loads(
            (self.root / "payload-task/tests/integrity_manifest.json").read_text()
        )
        self.assertIn("/tests/payload/nested/case.js", integrity_manifest["files"])

        apostrophe_payload = self.root / "apostrophe-payload"; apostrophe_payload.mkdir()
        (apostrophe_payload / "author's-case.js").write_text("export default true;\n")
        apostrophe_task = self.root / "apostrophe-task"
        export(self.dossier, self.tests, self.measurement, self.repo,
               self.instruction, "base", apostrophe_task, runner, apostrophe_payload)
        self.assertNotIn("author's-case.js", (apostrophe_task / "tests/test.sh").read_text())
        self.assertIn("/tests/payload/author's-case.js", json.loads(
            (apostrophe_task / "tests/integrity_manifest.json").read_text())["files"])

        unsafe = self.root / "unsafe-payload"; unsafe.mkdir()
        (unsafe / "link").symlink_to(payload / "nested/case.js")
        with self.assertRaisesRegex(ValueError, "unsafe entry"):
            export(self.dossier, self.tests, self.measurement, self.repo,
                   self.instruction, "base", self.root / "unsafe-task", runner, unsafe)

        root_link = self.root / "payload-link"; root_link.symlink_to(payload, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "real directory"):
            export(self.dossier, self.tests, self.measurement, self.repo,
                   self.instruction, "base", self.root / "root-link-task", runner, root_link)

    @patch("report_pipeline.harbor_export._run")
    def test_hidden_payload_cannot_be_a_silent_noop(self, run):
        run.side_effect = lambda *args: self._fake_command(args, "e")
        runner = self.root / "runner.py"; runner.write_text("print('{}')\n")
        payload = self.root / "payload"; payload.mkdir()
        (payload / "case.js").write_text("export default true;\n")

        with self.assertRaisesRegex(ValueError, "manifest execution"):
            export(self.dossier, self.tests, self.measurement, self.repo,
                   self.instruction, "base", self.root / "no-execution", runner, payload)

        manifest = json.loads(self.tests.read_text())
        manifest["execution"] = {"kind": "command_json_v1",
                                 "command": ["/usr/bin/python3", "-I", "/tests/functional_runner.py"]}
        self.tests.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "functional manifest tests"):
            export(self.dossier, self.tests, self.measurement, self.repo,
                   self.instruction, "base", self.root / "no-functional-tests", runner, payload)

    @patch("report_pipeline.harbor_export._run")
    def test_verifier_distinguishes_skip_missing_id_and_missing_source(self, run):
        def fake(*args):
            return self._fake_command(args)
        run.side_effect = fake
        output = self.root / "control"
        export(self.dossier, self.tests, self.measurement, self.repo, self.instruction, "base", output)

        transport = output / "transport"; transport.mkdir()
        (transport / "agent.patch").write_bytes(b"")
        (transport / "status").write_text("ok\n")
        env = {"HARBOR_TEST_ROOT": str(output / "tests"),
               "HARBOR_APP_ROOT": str(self.repo),
               "HARBOR_TRANSPORT_ROOT": str(transport)}
        # Baseline has the P2P marker but not the F2P marker.
        # Re-run from an isolated script copy that redirects the fixed Harbor log path.
        script = (output / "tests/sweb_grade.py").read_text().replace(
            'logs = Path("/logs/verifier")', f'logs = Path({str(output / "logs")!r})')
        local = output / "tests/verify_local.py"; local.write_text(script)
        subprocess.run(["python3", str(local)], env=env, check=True)
        record = json.loads((output / "logs/test_results.json").read_text())
        self.assertEqual([item["status"] for item in record["results"]], ["fail", "pass"])

        manifest = json.loads((output / "tests/test_manifest.json").read_text())
        manifest["tests"][0]["enabled"] = False
        (output / "tests/test_manifest.json").write_text(json.dumps(manifest))
        inventory = json.loads((output / "tests/frozen_inventory.json").read_text())
        inventory["test_manifest_sha256"] = hashlib.sha256(
            (output / "tests/test_manifest.json").read_bytes()).hexdigest()
        (output / "tests/frozen_inventory.json").write_text(json.dumps(inventory))
        subprocess.run(["python3", str(local)], env=env, check=True)
        record = json.loads((output / "logs/test_results.json").read_text())
        self.assertEqual(record["results"][0]["status"], "skip")
        self.assertEqual(record["reward"], 0)

        manifest["tests"] = manifest["tests"][1:]
        (output / "tests/test_manifest.json").write_text(json.dumps(manifest))
        inventory["test_manifest_sha256"] = hashlib.sha256(
            (output / "tests/test_manifest.json").read_bytes()).hexdigest()
        (output / "tests/frozen_inventory.json").write_text(json.dumps(inventory))
        subprocess.run(["python3", str(local)], env=env, check=True)
        record = json.loads((output / "logs/test_results.json").read_text())
        self.assertEqual(record["results"][0]["status"], "missing")

        # Restore the full manifest, point F2P at an absent production file, and keep hashes coherent.
        manifest = json.loads(self.tests.read_text()); manifest["tests"][0]["path"] = "absent.scss"
        (output / "tests/test_manifest.json").write_text(json.dumps(manifest))
        inventory["test_manifest_sha256"] = hashlib.sha256(
            (output / "tests/test_manifest.json").read_bytes()).hexdigest()
        (output / "tests/frozen_inventory.json").write_text(json.dumps(inventory))
        subprocess.run(["python3", str(local)], env=env, check=True)
        record = json.loads((output / "logs/test_results.json").read_text())
        self.assertEqual(record["results"][0]["status"], "error")
        self.assertEqual(record["results"][0]["failure_class"], "missing_source_file")

    @patch("report_pipeline.harbor_export._run")
    def test_rejects_non_issue_asset_source(self, run):
        data = json.loads(self.dossier.read_text())
        data["leakage_policy"]["safe_agent_assets"][0]["source_ids"] = ["o/r#1:pr_comment"]
        self.dossier.write_text(json.dumps(data))
        run.side_effect = lambda *args: self._fake_command(args)
        with self.assertRaisesRegex(ValueError, "non-Issue"):
            export(self.dossier, self.tests, self.measurement, self.repo, self.instruction, "base", self.root / "out")

    @patch("report_pipeline.harbor_export._run")
    def test_rejects_absolute_and_parent_test_paths(self, run):
        run.side_effect = lambda *args: ""
        original = json.loads(self.tests.read_text())
        for index, unsafe in enumerate(("/tests/secret.json", "../solution/gold.patch")):
            changed = json.loads(json.dumps(original))
            changed["tests"][0]["path"] = unsafe
            self.tests.write_text(json.dumps(changed))
            with self.subTest(path=unsafe), self.assertRaisesRegex(ValueError, "unsafe task path"):
                export(self.dossier, self.tests, self.measurement, self.repo, self.instruction,
                       "base", self.root / f"unsafe-{index}")
        self.tests.write_text(json.dumps(original))

    @patch("report_pipeline.harbor_export._run")
    def test_rejects_changed_dossier_source_binding(self, run):
        dossier = json.loads(self.dossier.read_text())
        Path(dossier["source_bindings"]["archive_path"]).write_text('{"changed": true}\n')
        with self.assertRaisesRegex(ValueError, "source binding changed"):
            export(self.dossier, self.tests, self.measurement, self.repo, self.instruction,
                   "base", self.root / "bound")

    @patch("report_pipeline.harbor_export._run")
    def test_rejects_self_signed_asset_allowlist(self, run):
        trusted = json.loads(self.dossier.read_text())
        self.rebuild.side_effect = None; self.rebuild.return_value = trusted
        changed = json.loads(json.dumps(trusted))
        changed["leakage_policy"]["safe_agent_source_ids"].append("o/r#1:body")
        self.dossier.write_text(json.dumps(changed))
        with self.assertRaisesRegex(ValueError, "source-derived candidate: leakage_policy"):
            export(self.dossier, self.tests, self.measurement, self.repo, self.instruction,
                   "base", self.root / "self-signed")


if __name__ == "__main__":
    unittest.main()
