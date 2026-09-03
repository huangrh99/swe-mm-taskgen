import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from report_pipeline import cli
from report_pipeline.v4_environment_plan import run


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, text=True, check=True,
                          stdout=subprocess.PIPE).stdout.strip()


class V4EnvironmentPlanTests(unittest.TestCase):
    def _repo(self, root: Path, name: str, files: dict[str, str]) -> tuple[Path, str]:
        repo = root / "repos" / name
        repo.mkdir(parents=True)
        _git(repo, "init")
        _git(repo, "config", "user.name", "Test")
        _git(repo, "config", "user.email", "test@example.com")
        for path, content in files.items():
            target = repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "base")
        return repo, _git(repo, "rev-parse", "HEAD")

    def test_clusters_identical_repo_manifests_and_marks_superset_unsupported(self):
        with tempfile.TemporaryDirectory(dir="tmp") as value:
            root = Path(value)
            _, js_base = self._repo(root, "carbon", {
                "package.json": json.dumps({"packageManager": "pnpm@9.1.0",
                                             "engines": {"node": ">=20"}}),
                "pnpm-lock.yaml": "lockfileVersion: '9.0'\n",
            })
            _, py_base = self._repo(root, "superset", {
                "pyproject.toml": "[project]\nrequires-python = '>=3.10'\n",
                "requirements/base.txt": "flask==3.0\n",
            })
            cases = [
                {"case_id": "carbon-design-system__carbon-1", "repository": "carbon-design-system/carbon"},
                {"case_id": "carbon-design-system__carbon-2", "repository": "carbon-design-system/carbon"},
                {"case_id": "apache__superset-3", "repository": "apache/superset"},
            ]
            payload = root / "payload.json"
            payload.write_text(json.dumps({"cases": cases}))
            campaign = root / "campaign/20_17_02_model_runs"
            for case in cases:
                case_root = campaign / case["case_id"]
                case_root.mkdir(parents=True)
                case_root.joinpath("20_17_01_packet.json").write_text(json.dumps({
                    "task_id": case["case_id"], "repository": case["repository"],
                    "base_commit": py_base if "superset" in case["repository"] else js_base,
                }))
            result = run(payload, root / "repos", root / "output", campaign)
            self.assertEqual(3, result["case_count"])
            self.assertEqual(2, result["reusable_group_count"])
            self.assertEqual(1, result["multi_case_reuse_group_count"])
            carbon = [item for item in result["cases"] if item["repository"].endswith("carbon")]
            self.assertEqual({item["environment_group"] for item in carbon},
                             {carbon[0]["environment_group"]})
            self.assertEqual("corepack pnpm install --frozen-lockfile",
                             carbon[0]["recommended_install_command"])
            superset = next(item for item in result["cases"] if "superset" in item["repository"])
            self.assertTrue(superset["unsupported"])
            self.assertIn("superset_mixed_python_node_stack", superset["risks"])
            self.assertTrue((root / "output/20_20_02_environment_plan.html").is_file())

    def test_cli_dispatches_plan(self):
        with unittest.mock.patch("report_pipeline.v4_environment_plan.run",
                                 return_value={"case_count": 39, "reusable_group_count": 10,
                                               "unsupported_count": 1}) as planner:
            status = cli.main(["plan-v4-environments", "--payload", "payload.json",
                               "--repositories", "/tmp/repos", "--output", "out"])
        self.assertEqual(0, status)
        planner.assert_called_once_with(Path("payload.json"), Path("/tmp/repos").resolve(),
                                        Path("out"), None)


if __name__ == "__main__":
    unittest.main()
