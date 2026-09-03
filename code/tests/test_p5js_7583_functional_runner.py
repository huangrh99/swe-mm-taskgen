import importlib.util
import json
import os
import unittest
from unittest.mock import patch
from pathlib import Path


RUNNER = (Path(__file__).parents[1] / "harbor_tests/p5js_7583/functional_runner.py")
SPEC = importlib.util.spec_from_file_location("p5js_7583_functional_runner", RUNNER)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class P5js7583FunctionalRunnerTests(unittest.TestCase):
    def test_only_untrusted_workspace_is_chowned_and_runner_scripts_are_immutable(self):
        source = RUNNER.read_text()
        self.assertIn("_chown_tree(workspace, untrusted_uid)", source)
        self.assertNotIn("_chown_tree(root, untrusted_uid)", source)
        self.assertIn("os.chmod(build_script, 0o555)", source)
        self.assertIn("trusted_hashes", source)

    def test_baseline_transition_is_one_f2p_plus_four_p2p(self):
        observed = {
            "missing_interior_pixels": 675,
            "unexpected_exterior_pixels": 0,
            "normal_clip": True,
            "inverted_clip": True,
            "ordinary_shape": True,
        }
        results = MODULE._classify(observed)
        self.assertEqual([item["test_id"] for item in results],
                         [item[0] for item in MODULE.TESTS])
        self.assertEqual([item["status"] for item in results],
                         ["fail", "pass", "pass", "pass", "pass"])

    def test_reference_transition_passes_every_test(self):
        observed = {
            "missing_interior_pixels": 0,
            "unexpected_exterior_pixels": 0,
            "normal_clip": True,
            "inverted_clip": True,
            "ordinary_shape": True,
        }
        self.assertEqual({item["status"] for item in MODULE._classify(observed)}, {"pass"})

    def test_parse_rejects_missing_or_extra_fields(self):
        payload = {
            "missing_interior_pixels": 0,
            "unexpected_exterior_pixels": 0,
            "normal_clip": True,
            "inverted_clip": True,
            "ordinary_shape": True,
        }
        document = '<pre id="result">' + json.dumps(payload) + "</pre>"
        self.assertEqual(MODULE._parse_observed(document), payload)
        payload["extra"] = True
        with self.assertRaisesRegex(ValueError, "field inventory"):
            MODULE._parse_observed('<pre id="result">' + json.dumps(payload) + "</pre>")

    def test_parse_rejects_boolean_pixel_count(self):
        payload = {
            "missing_interior_pixels": True,
            "unexpected_exterior_pixels": 0,
            "normal_clip": True,
            "inverted_clip": True,
            "ordinary_shape": True,
        }
        with self.assertRaisesRegex(ValueError, "pixel counts"):
            MODULE._parse_observed('<pre id="result">' + json.dumps(payload) + "</pre>")

    @patch("subprocess.run")
    def test_untrusted_commands_drop_oracle_privileges(self, run):
        with patch.dict(os.environ, {"HARBOR_UNTRUSTED_UID": "12345"}):
            MODULE._run(["true"], timeout=1, untrusted=True)
        preexec = run.call_args.kwargs["preexec_fn"]
        self.assertIsNotNone(preexec)
        with patch("os.setgroups") as setgroups, patch("os.setgid") as setgid, patch("os.setuid") as setuid:
            preexec()
        setgroups.assert_called_once_with([])
        setgid.assert_called_once_with(12345)
        setuid.assert_called_once_with(12345)


if __name__ == "__main__":
    unittest.main()
