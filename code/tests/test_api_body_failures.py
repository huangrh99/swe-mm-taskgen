import http.client
import unittest
from unittest.mock import MagicMock, patch

from pr_crawler.api import APIError, transport


class BodyFailureTests(unittest.TestCase):
    def test_body_failures_become_retryable_sanitized_errors(self):
        for failure in (TimeoutError("secret"), http.client.IncompleteRead(b"secret")):
            with self.subTest(failure=type(failure).__name__):
                response = MagicMock()
                response.__enter__.return_value = response
                response.read.side_effect = failure
                with patch("pr_crawler.api.urllib.request.build_opener") as opener:
                    opener.return_value.open.return_value = response
                    with self.assertRaises(APIError) as raised:
                        transport("GET", "/repos/o/r/pulls", None, "application/json", "token")
                self.assertNotIn("secret", str(raised.exception))
                self.assertIn(type(failure).__name__, str(raised.exception))
                response.__exit__.assert_called_once()
