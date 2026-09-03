import json
import shutil
import tempfile
import unittest
from pathlib import Path

from report_pipeline.paths import TMP_ROOT
from report_pipeline.task_projection import TASK_ENTRIES, materialize


class TaskProjectionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="task-projection-", dir=TMP_ROOT))
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.case = self.root / "owner__repo-1"
        for name in ("environment", "solution", "tests", "meta", "outputs"):
            (self.case / name).mkdir(parents=True)
        (self.case / "environment/Dockerfile").write_text("FROM fixed\n")
        (self.case / "instruction.md").write_text("fix\n")
        (self.case / "solution/solve.sh").write_text("#!/bin/sh\n")
        (self.case / "task.toml").write_text("schema_version='1.2'\n")
        (self.case / "tests/config.json").write_text(json.dumps({"x": 1}))
        (self.case / "outputs/trace.json").write_text("{}\n")
        (self.case / "meta/source.json").write_text("{}\n")

    def test_projection_contains_only_harbor_inputs_and_ignores_new_trace(self):
        first = materialize(self.case, self.root / "projections")
        self.assertEqual(set(TASK_ENTRIES), {item.name for item in first["path"].iterdir()})
        (self.case / "outputs/new-trace.json").write_text("{}\n")
        second = materialize(self.case, self.root / "projections")
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertEqual(first["path"], second["path"])

    def test_projection_changes_when_formal_input_changes(self):
        first = materialize(self.case, self.root / "projections")
        (self.case / "instruction.md").write_text("different\n")
        second = materialize(self.case, self.root / "projections")
        self.assertNotEqual(first["sha256"], second["sha256"])
        self.assertNotEqual(first["path"], second["path"])


if __name__ == "__main__":
    unittest.main()
