import os
import subprocess
import sys
import unittest
from pathlib import Path

from report_pipeline.paths import REPORT_ROOT


class FormalTestRunnerTests(unittest.TestCase):
    def test_runner_rejects_unpinned_environment_before_discovery(self):
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        completed = subprocess.run(
            [sys.executable, str(REPORT_ROOT / "test.py"), "--evidence"],
            cwd=REPORT_ROOT, env=environment, text=True, capture_output=True,
            check=False,
        )
        self.assertEqual(2, completed.returncode)
        self.assertIn("exact isolation environment", completed.stderr)


if __name__ == "__main__":
    unittest.main()
