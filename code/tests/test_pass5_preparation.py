import json
import shutil
import tempfile
import unittest
from pathlib import Path

import jsonschema

from report_pipeline.pass5_preparation import run
from report_pipeline.paths import REPORT_ROOT, TMP_ROOT, WORKSPACE_ROOT
from report_pipeline.workflow import OFFICIAL_CODEX_DISABLED_FEATURES


class Pass5PreparationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="pass5-preparation-", dir=TMP_ROOT))
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.cases = self.root / "cases"
        self.artifacts = self.cases
        self.case_id = "owner__repo-1"
        task = self.cases / self.case_id
        for directory in ("environment", "solution", "tests"):
            (task / directory).mkdir(parents=True)
        (task / "instruction.md").write_text("Fix the visual defect.\n")
        (task / "task.toml").write_text('schema_version = "1.2"\n')
        (task / "solution/solve.sh").write_text("#!/bin/sh\n")
        (task / "tests/test.sh").write_text("#!/bin/sh\n")
        (task / "tests/config.json").write_text(json.dumps({
            "FAIL_TO_PASS": ["f2p"], "PASS_TO_PASS": ["p2p"],
        }) + "\n")
        (task / "tests/test_manifest.json").write_text("{}\n")
        (task / "environment/Dockerfile").write_text(
            "FROM example.invalid/fixed\n"
            "RUN npm install --global @moonshot-ai/kimi-code@0.29.0 "
            "@openai/codex@0.148.0 \\\n"
            " && npm config set offline true --location=user \\\n"
            " && printf 'apk is disabled in this frozen glibc image' > /usr/local/bin/apk \\\n"
            " && command -v kimi && kimi --version \\\n"
            " && command -v codex && codex --version\n"
        )
        self.harbor = self.root / "bin/harbor"
        self.harbor.parent.mkdir()
        self.harbor.write_text("#!/bin/sh\necho 0.22.0\n")
        self.harbor.chmod(0o755)

    def test_generates_exact_pending_profiles_without_claiming_freeze(self):
        result = run(
            [self.case_id], cases_root=self.cases,
            case_artifacts_root=self.artifacts, harbor_executable=self.harbor,
        )
        self.assertEqual(result["status"], "prepared_not_launchable")
        self.assertEqual(result["launch_authorized_count"], 0)
        output = self.artifacts / self.case_id / "outputs/09_network_policy_remediation"
        status = json.loads((output / "00_preparation_status.json").read_text())
        self.assertFalse(status["launch_authorized"])
        self.assertFalse(status["freeze_binding"]["current"])
        self.assertIn("current_image_binding_missing", status["blocking_reasons"])
        self.assertEqual(status["task"]["source_case"],
                         (self.cases / self.case_id).relative_to(WORKSPACE_ROOT).as_posix())
        self.assertIn("tmp/harbor-task-projections/", status["task"]["path"])

        schema = json.loads(
            (REPORT_ROOT / "schemas/frozen_pass5_config_v1.schema.json").read_text()
        )
        allowed_job_keys = {
            "n_concurrent_trials", "n_attempts", "environment", "agents", "tasks",
            "retry", "load_trajectory", "resume",
        }
        for provider in ("kimi-k3", "codex-luna-max"):
            pass5_concurrency = 2 if provider == "kimi-k3" else 5
            for label, concurrency in (("pass1", 1), ("pass5", pass5_concurrency)):
                root = output / provider
                job = json.loads((root / f"{label}_job.pending.json").read_text())
                config = json.loads(
                    (root / f"{label}_frozen_pass5_config.pending.json").read_text()
                )
                jsonschema.Draft202012Validator(schema).validate(config)
                self.assertEqual(set(job), allowed_job_keys)
                self.assertNotIn("job_name", job)
                self.assertNotIn("jobs_dir", job)
                self.assertEqual(job["n_attempts"], 1)
                self.assertEqual(job["n_concurrent_trials"], concurrency)
                self.assertEqual(job["tasks"], [{"path": status["task"]["path"]}])
                self.assertEqual(job["environment"]["extra_allowed_hosts"], [])
                self.assertEqual(job["agents"][0]["mcp_servers"], [])
                self.assertEqual(job["agents"][0]["skills"], [])
                self.assertEqual(config["expected_test_ids"], ["f2p", "p2p"])
                self.assertEqual(config["trial_concurrency"], concurrency)
                self.assertFalse(Path(config["harbor_job_config"]["path"]).is_absolute())

        kimi = json.loads(
            (output / "kimi-k3/pass5_job.pending.json").read_text()
        )["agents"][0]
        self.assertEqual(kimi["extra_allowed_hosts"], ["ark-cn-beijing.bytedance.net"])
        self.assertEqual(kimi["env"]["KIMI_MODEL_MAX_CONTEXT_SIZE"], "1048576")
        self.assertEqual(kimi["env"]["KIMI_MODEL_MAX_COMPLETION_TOKENS"], "+131072")

        codex = json.loads(
            (output / "codex-luna-max/pass5_job.pending.json").read_text()
        )["agents"][0]
        self.assertEqual(codex["kwargs"]["web_search"], "disabled")
        self.assertEqual(codex["kwargs"]["config"]["mcp_servers"], {})
        self.assertEqual(codex["kwargs"]["config"]["features"],
                         OFFICIAL_CODEX_DISABLED_FEATURES)


if __name__ == "__main__":
    unittest.main()
