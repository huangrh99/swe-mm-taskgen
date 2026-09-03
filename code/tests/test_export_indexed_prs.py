import unittest

from analysis.scripts.step_00_01_export_indexed_prs import build_rows


class ExportIndexedPrsTests(unittest.TestCase):
    def test_filters_created_time_adds_provenance_and_orders_identities(self):
        documents = {
            "index/z/repo": {"repo": "z/repo", "status": "partial",
                             "consistency": "unreconciled", "cutoff": "cut",
                             "observed_at": "observed", "items": [
                                 {"id": 3, "number": 3, "created_at": "2024-12-31T23:59:59Z"},
                                 {"id": 2, "number": 2, "created_at": "2025-01-02T00:00:00Z"},
                             ]},
            "index/a/repo": {"repo": "a/repo", "status": "complete",
                             "consistency": "two_pass_observed_stable", "cutoff": "cut",
                             "observed_at": "observed", "items": [
                                 {"id": 1, "number": 1, "created_at": "2025-01-01T00:00:00Z"},
                             ]},
        }
        rows, provenance = build_rows(
            documents, "run", "2025-01-01T00:00:00Z", "2026-01-01T00:00:00Z")
        self.assertEqual([("a/repo", 1), ("z/repo", 2)],
                         [(item["repo"], item["number"]) for item in rows])
        self.assertTrue(all(item["source_run_id"] == "run" for item in rows))
        self.assertEqual(["a/repo", "z/repo"], [item["repo"] for item in provenance])


if __name__ == "__main__":
    unittest.main()
