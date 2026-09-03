import contextlib
import io
import sys
import types
import unittest
import subprocess
from pathlib import Path
from unittest.mock import patch

from report_pipeline import cli


class ReportPipelineCliTest(unittest.TestCase):
    def test_public_wrapper_isolated_to_formal_tree(self):
        root = Path(__file__).resolve().parents[2]
        result = subprocess.run(
            [sys.executable, str(root / "run.py"), "list"],
            cwd=root, text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Supported commands", result.stdout)

    def test_direct_collection_entrypoint_is_rejected(self):
        code_root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [sys.executable, "-m", "pr_crawler", "--help"],
            cwd=code_root,
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("Unsupported direct entrypoint", result.stderr)
    def test_list_is_the_single_public_command_index(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(["list"]), 0)
        text = output.getvalue()
        self.assertIn("collect", text)
        self.assertNotIn("legacy-bpmn-export-harbor", text)
        self.assertNotIn("legacy-bpmn-test-wave", text)
        self.assertIn("text-sufficiency", text)
        self.assertIn("candidate-dossier", text)
        self.assertIn("classify-before-review", text)
        self.assertIn("audit-category-distribution", text)
        self.assertIn("promote-harbor-task", text)
        self.assertIn("run-frozen-pass5", text)
        self.assertIn("audit-case-batch", text)
        for command in (
            "prepare-pr-pool", "recall-and-archive", "construct-solver-inputs",
            "screen-multimodal-candidates", "review-visual-gate",
        ):
            self.assertIn(command, text)

    def test_stage_dispatch_preserves_arguments(self):
        with patch.object(cli, "_dispatch_module", return_value=7) as dispatch:
            self.assertEqual(cli.main(["verify-visual", "--output", "out"]), 7)
        dispatch.assert_called_once_with("analysis.scripts.step_09_03_run_visual_verifiers", ["--output", "out"])

    def test_export_visual_replaces_removed_wrapper(self):
        export = unittest.mock.Mock(return_value={"status": "complete"})
        module = types.ModuleType("analysis.scripts.step_09_03_run_visual_verifiers")
        module.export_results = export
        with patch.dict(sys.modules, {module.__name__: module}):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(cli.main(["export-visual", "run"]), 0)
        export.assert_called_once()
        self.assertIn('"status": "complete"', output.getvalue())

    def test_unknown_command_is_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            cli.main(["old-pilot"])
        self.assertEqual(raised.exception.code, 2)

    def test_legacy_model_call_commands_are_not_public(self):
        for command in ("legacy-bpmn-freeze", "legacy-bpmn-test-wave", "legacy-bpmn-generate-tests"):
            with self.subTest(command=command), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                cli.main([command, "--run"])
            self.assertEqual(raised.exception.code, 2)

    def test_candidate_dossier_requires_v3_or_explicit_review_only_migration(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            cli.main(["candidate-dossier", "--verifier", "v.json",
                      "--archive", "a.json", "--output", "out.json"])
        self.assertEqual(2, raised.exception.code)

        write = unittest.mock.Mock(return_value={"status": "review_or_exclude"})
        module = types.ModuleType("report_pipeline.candidate")
        module.write = write
        with patch.dict(sys.modules, {module.__name__: module}):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(cli.main(["candidate-dossier", "--verifier", "v.json",
                    "--archive", "a.json", "--legacy-migration", "--output", "out.json"]), 0)
        self.assertTrue(write.call_args.kwargs["allow_legacy_migration"])
        self.assertIsNone(write.call_args.args[3])

    def test_candidate_dossier_passes_required_v3_classification(self):
        write = unittest.mock.Mock(return_value={"status": "admitted_to_test_construction"})
        module = types.ModuleType("report_pipeline.candidate")
        module.write = write
        with patch.dict(sys.modules, {module.__name__: module}):
            self.assertEqual(cli.main(["candidate-dossier", "--verifier", "v.json",
                "--archive", "a.json", "--classification", "v3.json",
                "--output", "out.json"]), 0)
        self.assertEqual(Path("v3.json"), write.call_args.args[3])
        self.assertFalse(write.call_args.kwargs["allow_legacy_migration"])

    def test_classification_run_requires_authorization_before_evaluator_construction(self):
        engine = types.ModuleType("pr_crawler.api_engines")
        engine.ApiEvaluator = unittest.mock.Mock(side_effect=AssertionError(
            "must reject before evaluator construction"))
        with patch.dict(sys.modules, {engine.__name__: engine}):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                cli.main(["classify-before-review", "--run-directory", "source",
                          "--output", "out", "--run"])
        self.assertEqual(2, raised.exception.code)
        engine.ApiEvaluator.assert_not_called()

    def test_category_distribution_audit_cli_binds_paths(self):
        run = unittest.mock.Mock(return_value={"gate_passed": False})
        module = types.ModuleType("report_pipeline.category_audit")
        module.run = run
        with patch.dict(sys.modules, {module.__name__: module}):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(cli.main(["audit-category-distribution",
                    "--classification", "classification.json", "--output", "audit",
                    "--exclusions", "exclusions.json"]), 0)
            run.assert_called_once_with([Path("classification.json")], Path("audit"),
                                        Path("exclusions.json"))
        self.assertIn('"gate_passed": false', output.getvalue())


if __name__ == "__main__":
    unittest.main()
