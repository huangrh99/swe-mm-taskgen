import importlib.util
import unittest
from pathlib import Path


class CleanupManifestTest(unittest.TestCase):
    def test_cleanup_claims_are_machine_checked(self):
        root = Path(__file__).resolve().parents[2]
        path = root / "manifests/verify_cleanup.py"
        spec = importlib.util.spec_from_file_location("verify_cleanup", path)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        self.assertEqual(module.validate(), 20)


if __name__ == "__main__":
    unittest.main()
