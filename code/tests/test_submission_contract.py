import json
import tempfile
import unittest
from pathlib import Path

from report_pipeline.submission_contract import validate


class SubmissionContractTests(unittest.TestCase):
    def _root(self) -> tuple[tempfile.TemporaryDirectory, Path]:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        for name in ("README.md", "SUBMISSION_CONTRACT.md", "pipeline_design.svg"):
            (root / name).write_text("x")
        (root / "code").mkdir()
        (root / "cases").mkdir()
        return temporary, root

    def _task(self, root: Path, number: int) -> None:
        case = root / "cases" / f"owner__repo-{number}"
        task = case
        for directory in ("environment/assets", "solution", "tests"):
            (task / directory).mkdir(parents=True, exist_ok=True)
        (task / "environment/assets/a.png").write_bytes(b"png")
        (task / "environment/Dockerfile").write_text("FROM visual-harbor-base:" + "a" * 64 + "\n")
        (task / "environment/base_image.json").write_text(json.dumps({
            "image_id": "sha256:" + "a" * 64,
            "repo_digest": "pinned@sha256:" + "a" * 64,
            "build_reference": "visual-harbor-base:" + "a" * 64,
            "offline_archive": "archive.tar",
            "offline_archive_sha256": "b" * 64,
        }))
        (task / "instruction.md").write_text("Edit /testbed; see /testbed/assets/a.png\n")
        (task / "solution/solve.sh").write_text("git apply /solution/gold.patch\n")
        (task / "solution/gold.patch").write_text("diff --git a/a b/a\n")
        (task / "tests/test.sh").write_text(
            "mkdir -p /logs/verifier\n"
            "git apply /tests/test.patch\n"
            "python3 /tests/sweb_grade.py\n"
        )
        (task / "tests/sweb_grade.py").write_text("print('grade')\n")
        (task / "tests/test.patch").write_text("diff --git a/a b/a\n")
        (task / "tests/config.json").write_text(json.dumps({
            "repo": "owner/repo", "instance_id": task.name, "base_commit": "c" * 40,
            "FAIL_TO_PASS": ["f"], "PASS_TO_PASS": ["p"], "log_parser": "parser"}))
        (task / "task.toml").write_text(
            'schema_version = "1.2"\n[environment]\ncpus=1\nmemory_mb=1\n'
            'storage_mb=1\nallow_internet=false\n[agent]\ntimeout_sec=1\n'
            '[verifier]\ntimeout_sec=1\n')

    def test_accepts_direct_named_task_root(self):
        temporary, root = self._root()
        self.addCleanup(temporary.cleanup)
        self._task(root, 1)
        result = validate(root, minimum_tasks=1)
        self.assertEqual("valid_static_contract", result["tasks"][0]["status"])
        self.assertEqual(".", result["tasks"][0]["task_path"])

    def test_accepts_curator_meta_and_runtime_outputs_beside_task_inputs(self):
        temporary, root = self._root()
        self.addCleanup(temporary.cleanup)
        self._task(root, 1)
        case = root / "cases/owner__repo-1"
        (case / "meta").mkdir()
        (case / "outputs").mkdir()
        result = validate(root, minimum_tasks=1)
        self.assertEqual("valid_static_contract", result["tasks"][0]["status"])

    def test_does_not_count_source_only_dossiers(self):
        temporary, root = self._root()
        self.addCleanup(temporary.cleanup)
        self._task(root, 1)
        (root / "cases/other__candidate-2/meta").mkdir(parents=True)
        result = validate(root, minimum_tasks=1)
        self.assertEqual(1, result["observed_iid_tasks"])

    def test_reports_five_task_minimum_separately_from_task_validity(self):
        temporary, root = self._root()
        self.addCleanup(temporary.cleanup)
        self._task(root, 1)
        result = validate(root)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["tasks"][0]["status"], "valid_static_contract")
        self.assertEqual(result["errors"][0]["code"], "insufficient_iid_tasks")

    def test_accepts_five_static_contracts(self):
        temporary, root = self._root()
        self.addCleanup(temporary.cleanup)
        for number in range(1, 6):
            self._task(root, number)
        result = validate(root)
        self.assertEqual(result["status"], "static_layout_complete_not_exam_ready")
        self.assertFalse(result["exam_ready"])
        self.assertEqual(result["observed_iid_tasks"], 5)

    def test_local_base_tag_reports_missing_restore_binding_as_extension_warning(self):
        temporary, root = self._root()
        self.addCleanup(temporary.cleanup)
        self._task(root, 1)
        binding = root / "cases/owner__repo-1/environment/base_image.json"
        record = json.loads(binding.read_text())
        record.pop("offline_archive")
        binding.write_text(json.dumps(record))
        result = validate(root, minimum_tasks=1)
        codes = [item["code"] for item in result["tasks"][0]["warnings"]]
        self.assertIn("missing_local_base_restore_binding", codes)
        self.assertEqual("valid_static_contract", result["tasks"][0]["status"])

    def test_accepts_compact_contiguous_asset_range(self):
        temporary, root = self._root()
        self.addCleanup(temporary.cleanup)
        self._task(root, 1)
        task = root / "cases/owner__repo-1"
        (task / "environment/assets/a.png").unlink()
        for index in range(1, 7):
            (task / f"environment/assets/asset_{index:02d}.png").write_bytes(b"png")
        (task / "instruction.md").write_text(
            "Edit /testbed; see `/testbed/assets/asset_01.png` through "
            "`/testbed/assets/asset_06.png`.\n"
        )
        result = validate(root, minimum_tasks=1)
        self.assertEqual("valid_static_contract", result["tasks"][0]["status"])

    def test_accepts_parent_image_binding_for_derived_case_image(self):
        temporary, root = self._root()
        self.addCleanup(temporary.cleanup)
        self._task(root, 1)
        task = root / "cases/owner__repo-1"
        binding = json.loads((task / "environment/base_image.json").read_text())
        binding["build_reference"] = "report-case:owner-repo-1"
        binding["parent_image_id"] = "sha256:" + "a" * 64
        (task / "environment/base_image.json").write_text(json.dumps(binding))
        result = validate(root, minimum_tasks=1)
        self.assertEqual("valid_static_contract", result["tasks"][0]["status"])

    def test_registry_digest_is_not_confused_with_image_id(self):
        temporary, root = self._root()
        self.addCleanup(temporary.cleanup)
        self._task(root, 1)
        task = root / "cases/owner__repo-1"
        reference = "registry.example/task@sha256:" + "d" * 64
        (task / "environment/Dockerfile").write_text(f"FROM {reference}\n")
        (task / "environment/base_image.json").write_text(json.dumps({
            "image_id": "sha256:" + "e" * 64,
            "repo_digest": reference,
            "build_reference": reference,
        }))
        result = validate(root, minimum_tasks=1)
        self.assertEqual(result["tasks"][0]["status"], "valid_static_contract")

    def test_uses_final_from_in_multistage_dockerfile(self):
        temporary, root = self._root()
        self.addCleanup(temporary.cleanup)
        self._task(root, 1)
        task = root / "cases/owner__repo-1"
        dockerfile = task / "environment/Dockerfile"
        dockerfile.write_text(
            "FROM node@sha256:" + "b" * 64 + " AS frozen-agent-node\n"
            "FROM visual-harbor-base:" + "a" * 64 + "\n"
            "COPY --from=frozen-agent-node /usr/local/ /opt/node/\n"
        )
        result = validate(root, minimum_tasks=1)
        self.assertEqual(result["tasks"][0]["status"], "valid_static_contract")


if __name__ == "__main__":
    unittest.main()
