import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from report_pipeline.candidate_pool_translation import run


class FakeEvaluator:
    backend = "gemini"
    profile = {"model": "fake-gemini"}
    attempts = 3

    def __call__(self, *, packet, image_paths, system_prompt, schema, workdir, timeout):
        del image_paths, system_prompt, schema, timeout
        source = packet["items"][0]
        value = {"translations": [{
            "case_id": source["case_id"],
            "pr_title_zh": "标题",
            "problem_statement_zh": "视觉材料 1\n```js\nconst x = 1\n```",
        }]}
        Path(workdir, "09_model_raw.json").write_text(json.dumps(value))
        Path(workdir, "10_api_invocation.json").write_text("{}")
        return value, {"backend": "gemini", "requested_model": "fake-gemini"}


class CandidatePoolTranslationTests(unittest.TestCase):
    def test_reuses_bound_translation_and_translates_only_missing_case(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "pool"
            source.mkdir()
            rows = []
            for index in (1, 2):
                rows.append({
                    "case_id": f"org__repo-{index}",
                    "archive": {"pr_title": f"title {index}"},
                    "problem_statement": "视觉材料 1\n```js\nconst x = 1\n```",
                })
            data = {"schema_version": "capability-candidate-pool-v2", "records": rows}
            data_path = source / "16_11_05_candidate_pool.json"
            data_path.write_text(json.dumps(data))
            manifest = {
                "schema_version": "capability-candidate-pool-manifest-v2",
                "data": data_path.name,
                "data_sha256": hashlib.sha256(data_path.read_bytes()).hexdigest(),
            }
            (source / "16_11_07_manifest.json").write_text(json.dumps(manifest))
            digest = lambda row: hashlib.sha256((
                row["case_id"] + "\0" + row["archive"]["pr_title"]
                + "\0" + row["problem_statement"]).encode()).hexdigest()
            existing = root / "existing.json"
            existing.write_text(json.dumps({
                "schema_version": "human-review-zh-translations-v1",
                "notice": "curator display only",
                "items": [{
                    "case_id": rows[0]["case_id"],
                    "pr_title_zh": "已有标题",
                    "problem_statement_zh": rows[0]["problem_statement"],
                    "source_text_sha256": digest(rows[0]),
                }],
            }))
            audit = run(source, root / "output", [existing], FakeEvaluator(), workers=10)
            self.assertEqual(audit["status"], "complete")
            self.assertEqual(audit["reused_count"], 1)
            self.assertEqual(audit["invoked_count"], 1)
            artifact = json.loads((
                root / "output/16_12_04_translations_zh.json").read_text())
            self.assertEqual([item["case_id"] for item in artifact["items"]],
                             [row["case_id"] for row in rows])
            self.assertEqual(len(artifact["invocations"]), 1)


if __name__ == "__main__":
    unittest.main()
