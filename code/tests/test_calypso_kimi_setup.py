import unittest

from report_pipeline.paths import REPORT_ROOT


class CalypsoKimiSetupTests(unittest.TestCase):
    def test_harbor_setup_uses_frozen_node_before_global_install(self):
        for case_id in (
            "automattic__wp-calypso-100957",
            "automattic__wp-calypso-99049",
        ):
            with self.subTest(case_id=case_id):
                dockerfile = (
                    REPORT_ROOT / "cases" / case_id / "environment" / "Dockerfile"
                ).read_text()
                self.assertIn(
                    'ENV PATH="/opt/frozen-agent-node/bin:/root/.local/bin:${PATH}"',
                    dockerfile,
                )
                self.assertIn("@moonshot-ai/kimi-code@0.29.0", dockerfile)


if __name__ == "__main__":
    unittest.main()
