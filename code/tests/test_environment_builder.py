import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from report_pipeline import environment_builder


class EnvironmentBuilderTests(unittest.TestCase):
    def test_every_incomplete_archived_case_has_a_pinned_recipe(self):
        expected = {
            "bpmn-io__bpmn-js-2396",
            "automattic__wp-calypso-100957", "automattic__wp-calypso-99049",
            "carbon-design-system__carbon-22019", "excalidraw__excalidraw-9002",
            "excalidraw__excalidraw-9010",
            "googlechrome__lighthouse-16403", "mermaid-js__mermaid-7711",
        }
        self.assertEqual(set(environment_builder.CASES), expected)
        for recipe in environment_builder.CASES.values():
            self.assertRegex(recipe["commit"], r"^[0-9a-f]{40}$")
            self.assertTrue(recipe["install"])

    def test_asset_copy_is_deterministic_and_hash_bound(self):
        with tempfile.TemporaryDirectory() as temporary:
            case = Path(temporary) / "case"
            source = case / "meta/01_visual_review/assets"
            source.mkdir(parents=True)
            (source / "z.png").write_bytes(b"z")
            (source / "a.gif").write_bytes(b"a")
            environment = case / "environment"
            records = environment_builder._copy_assets(case, environment)
            self.assertEqual([item["name"] for item in records], ["asset_01.gif", "asset_02.png"])
            self.assertEqual(records[0]["sha256"], hashlib.sha256(b"a").hexdigest())


if __name__ == "__main__":
    unittest.main()
