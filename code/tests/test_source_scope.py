import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from report_pipeline.source_scope import audit, build_packet, discover, render, run, validate


class SourceScopeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent / "fixtures"
        self.base = self.root / "carbon_20978_archive"
        self.dossier = self.base / "18_01_candidate_dossier.json"
        self.context = self.base / "18_40_test_review_context.json"
        self.snapshot = {
            "schema_version": "source-scope-issue-snapshot-v1",
            "repository": "carbon-design-system/carbon",
            "issue_number": 17992,
            "url": "https://github.com/carbon-design-system/carbon/issues/17992",
            "title": "React|WC Parity: Sync existing components",
            "body": "Acceptance criteria. Sub-issues: Tabs #18768, Accordion #17926.",
            "state": "open", "created_at": "2024-11-06T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z", "fetched_at": "2026-09-01T00:00:00Z",
            "descendants_fetched": False, "sub_issues_fetched": False,
        }

    def annotation(self):
        return {
            "schema_version": "source-scope-verifier-v1",
            "candidate_id": "carbon-design-system__carbon-20978",
            "overall_decision": "curator_only", "confidence": "high",
            "expand_descendants": False, "human_review_required": True,
            "scope_blockers": ["Storybook criterion is not bound to a frozen test"],
            "ancestor_issues": [{
                "repository": "carbon-design-system/carbon", "issue_number": 17992,
                "relation": "acceptance_parent_reference", "overall_decision": "curator_only",
                "summary": "Umbrella acceptance context", "descendants_excluded": True,
                "reason": "Sibling component work must not expand this task",
                "requirements": [{
                    "requirement": "Update component visuals and styles",
                    "source_quote": "update component visuals/styles",
                    "new_information": "no", "patch_relevant": "yes", "executable": "yes",
                    "currently_tested": "yes", "decision": "curator_only",
                    "requires_test_update": False, "reason": "Duplicated by the direct Issue",
                }],
            }],
        }

    def test_discovers_only_explicit_one_hop_parent(self):
        packet = json.loads((self.base / "16_06_packet_0021.json").read_text())
        result = discover(packet)
        self.assertEqual([("carbon-design-system/carbon", 17992)], [
            (item["repository"], item["issue_number"]) for item in result
        ])
        self.assertTrue(all(item["ancestor_depth"] == 1 for item in result))
        self.assertTrue(all(not item["expand_descendants"] for item in result))

    def test_bound_run_does_not_expand_parent_sub_issues(self):
        seen = {}
        def evaluator(**kwargs):
            packet = kwargs["packet"]
            seen["packet"] = packet
            return self.annotation(), {"backend": "fake", "model": "fixture"}
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "18_45_parent_issue_scope_verifier"
            with patch("report_pipeline.source_scope.fetch_issue", return_value=self.snapshot) as fetch:
                manifest = run(self.dossier, output, self.context, evaluator=evaluator)
            fetch.assert_called_once_with("carbon-design-system/carbon", 17992, None)
            self.assertEqual("complete", manifest["status"])
            self.assertFalse(manifest["descendants_fetched"])
            self.assertEqual(1, manifest["ancestor_count"])
            self.assertFalse(seen["packet"]["traversal_policy"]["expand_descendants"])
            self.assertNotIn(18768, [x["issue_number"] for x in seen["packet"]["ancestor_discovery"]])
            self.assertTrue((output / "18_45_07_verifier_result.json").is_file())
            record = audit(output, output / "18_45_09_audit.json")
            self.assertEqual("passed", record["status"])
            self.assertFalse(record["descendants_fetched"])
            page = render(output, output / "18_45_10_source_scope_review.html")
            text = page.read_text()
            self.assertIn("Parent Issue 范围判定", text)
            self.assertIn("未抓取 parent 的 descendants", text)
            self.assertIn("curator_only", text)

    def test_included_requirement_requires_test_binding_or_update(self):
        packet = build_packet(self.dossier, self.context, [self.snapshot])
        annotation = self.annotation()
        requirement = annotation["ancestor_issues"][0]["requirements"][0]
        requirement.update(decision="include_agent_prompt", currently_tested="no",
                           requires_test_update=False)
        with self.assertRaisesRegex(ValueError, "lacks a test binding"):
            validate(annotation, packet)


if __name__ == "__main__":
    unittest.main()
