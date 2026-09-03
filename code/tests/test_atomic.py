import json
import tempfile
import unittest
from pathlib import Path

from report_pipeline.atomic import write_json
from report_pipeline.paths import TMP_ROOT


class AtomicPublicationTests(unittest.TestCase):
    def test_precreated_legacy_temp_symlink_cannot_overwrite_victim(self):
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory).resolve()
            victim = root / "victim.txt"
            victim.write_text("must remain unchanged\n")
            target = root / "evidence.json"
            legacy_temp = target.with_suffix(target.suffix + ".tmp")
            legacy_temp.symlink_to(victim)

            write_json(target, {"status": "published"})

            self.assertEqual("must remain unchanged\n", victim.read_text())
            self.assertEqual({"status": "published"}, json.loads(target.read_text()))
            self.assertTrue(legacy_temp.is_symlink())

    def test_leaf_symlink_is_replaced_without_overwriting_its_target(self):
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory).resolve()
            victim = root / "victim.txt"
            victim.write_text("must remain unchanged\n")
            target = root / "evidence.json"
            target.symlink_to(victim)

            write_json(target, {"status": "published"})

            self.assertEqual("must remain unchanged\n", victim.read_text())
            self.assertFalse(target.is_symlink())
            self.assertEqual({"status": "published"}, json.loads(target.read_text()))


if __name__ == "__main__":
    unittest.main()
