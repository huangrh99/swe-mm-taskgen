from pathlib import Path
import subprocess
import tempfile
import unittest

from report_pipeline.test_context_builder import assemble_repository_test_context


class TestContextBuilderTests(unittest.TestCase):
    def _repository(self, root: Path) -> tuple[Path, str]:
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.test"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
        (repo / "src").mkdir()
        (repo / "src/subject.ts").write_text("import { value } from './value';\nexport default () => value;\n")
        (repo / "src/value.ts").write_text("export const value = 3;\n")
        (repo / "test").mkdir()
        (repo / "test/subject.test.ts").write_text("import subject from '../src/subject';\nit('works', () => subject());\n")
        (repo / "package.json").write_text('{"scripts":{"test":"jest"}}\n')
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
        commit = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
        return repo, commit

    def _packet(self, commit: str) -> dict:
        return {
            "task_id": "o__r-1",
            "production_change_summary": {"base_commit": commit, "paths": ["src/subject.ts"]},
            "repository_test_context": {
                "working_directory": ".", "target_command": "npm test",
                "allowed_test_commands": [{"command_id": "frozen", "working_directory": ".",
                                             "command": "npm test"}],
                "writable_test_roots": ["test/"], "test_collection_roots": ["test/"],
            },
            "existing_tests": {"files": [{"path": "test/subject.test.ts",
                                             "content": "import subject from '../src/subject';\nit('works', () => subject());\n"}]},
        }

    def test_adds_exact_sut_dependency_closure_and_roles(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, commit = self._repository(Path(directory))
            packet = assemble_repository_test_context(self._packet(commit), repo)
            context = packet["repository_test_context"]
            self.assertEqual(context["completeness"]["status"], "complete")
            files = {item["path"]: item for item in context["context_files"]}
            self.assertEqual(files["src/subject.ts"]["role"], "sut")
            self.assertEqual(files["src/value.ts"]["role"], "sut_dependency")
            self.assertEqual(files["test/subject.test.ts"]["role"], "test_template")
            self.assertTrue(all(item["sha256"] for item in files.values()))
            self.assertIn("package.json", files)

    def test_unresolved_relative_import_blocks_model_ready_status(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, commit = self._repository(Path(directory))
            (repo / "src/value.ts").unlink()
            subprocess.run(["git", "-C", str(repo), "add", "-u"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "remove dependency"], check=True)
            commit = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
            packet = assemble_repository_test_context(self._packet(commit), repo)
            completeness = packet["repository_test_context"]["completeness"]
            self.assertEqual(completeness["status"], "incomplete")
            self.assertIn("relative_import_unresolved",
                          {item["code"] for item in completeness["blockers"]})

    def test_packet_bytes_that_differ_from_base_are_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, commit = self._repository(Path(directory))
            packet = self._packet(commit)
            packet["existing_tests"]["files"][0]["content"] += "// curator extension\n"
            enriched = assemble_repository_test_context(packet, repo)
            item = next(item for item in enriched["existing_tests"]["files"]
                        if item["path"] == "test/subject.test.ts")
            self.assertEqual(item["source"], "packet_supplied_nonbase")
            self.assertFalse(item["base_blob_matches"])

    def test_missing_frozen_package_script_blocks_invocation(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, commit = self._repository(Path(directory))
            packet = self._packet(commit)
            packet["repository_test_context"]["allowed_test_commands"][0][
                "command"] = "npm run does-not-exist"
            enriched = assemble_repository_test_context(packet, repo)
            blockers = enriched["repository_test_context"]["completeness"]["blockers"]
            self.assertIn("package_script_missing", {item["code"] for item in blockers})


if __name__ == "__main__":
    unittest.main()
