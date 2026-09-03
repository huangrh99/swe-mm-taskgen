import inspect
import unittest

from report_pipeline.capability_verifier import run


class CapabilityVerifierDefaultsTests(unittest.TestCase):
    def test_default_parallelism_is_ten(self):
        self.assertEqual(inspect.signature(run).parameters["workers"].default, 10)


if __name__ == "__main__":
    unittest.main()
