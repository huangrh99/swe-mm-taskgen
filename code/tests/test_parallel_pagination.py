import json
from urllib.parse import parse_qs, urlencode, urlsplit

from pr_crawler.api import APIError
from pr_crawler.core import index_repository
from pr_crawler.pagination import parallel_index_pages
from test_pr_crawler import BaseTest, FakeGitHub


class ParallelPaginationTests(BaseTest):
    def paginated_fake(self, count=305):
        fake = FakeGitHub(count)
        def send(method, endpoint, payload, accept, token):
            status, headers, body = fake(method, endpoint, payload, accept, token)
            parsed = urlsplit(endpoint)
            params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            params.update(page=str((count + 99) // 100), per_page="100")
            if "link" in headers:
                headers["link"] += f', <https://api.github.com{parsed.path}?{urlencode(params)}>; rel="last"'
            return status, headers, body
        return fake, send

    def test_parallel_index_and_cached_repeat(self):
        fake, send = self.paginated_fake()
        first = index_repository(self.api(send), "o/r", page_workers=4)
        self.assertEqual("complete", first["status"])
        self.assertEqual(305, len(first["items"]))
        self.assertEqual([4, 4], [p["pages"] for p in first["passes"]])
        self.assertEqual(8, len(fake.calls))
        second = index_repository(self.api(send), "o/r", page_workers=4)
        self.assertEqual(first["items"], second["items"])
        self.assertEqual(8, len(fake.calls))

    def test_eight_workers_over_one_thousand_prs(self):
        fake, send = self.paginated_fake(1105)
        result = index_repository(self.api(send), "o/r", page_workers=8)
        self.assertEqual("complete", result["status"])
        self.assertEqual(1105, len(result["items"]))
        self.assertEqual(24, len(fake.calls))

    def test_failed_page_is_retried_without_refetching_successes(self):
        fake, send = self.paginated_fake()
        fake.fail_page = 2
        first = parallel_index_pages(self.api(send), "/repos/o/r/pulls", "test", 4)
        self.assertEqual("partial", first["status"])
        self.assertIn(2, first["failed_pages"])
        self.assertEqual(205, len(first["items"]))
        fake.fail_page = None
        fake.calls.clear()
        second = parallel_index_pages(self.api(send), "/repos/o/r/pulls", "test", 4)
        self.assertEqual("complete", second["status"])
        self.assertEqual(305, len(second["items"]))
        self.assertEqual(1, len(fake.calls))

    def test_no_last_link_falls_back_to_serial(self):
        result = parallel_index_pages(self.api(FakeGitHub(105)), "/repos/o/r/pulls", "test", 4)
        self.assertEqual("complete", result["status"])
        self.assertEqual(105, len(result["items"]))

    def test_github_numeric_repository_link_aliases(self):
        fake, send = self.paginated_fake()
        def aliased(method, endpoint, payload, accept, token):
            status, headers, body = send(method, endpoint.replace("/repositories/99", "/repos/o/r"), payload, accept, token)
            if "link" in headers:
                headers["link"] = headers["link"].replace("/repos/o/r", "/repositories/99")
            return status, headers, body
        result = parallel_index_pages(self.api(aliased), "/repos/o/r/pulls", "test", 4)
        self.assertEqual("complete", result["status"])
        self.assertEqual(305, len(result["items"]))

    def test_rejects_foreign_last_page_link(self):
        def send(*args):
            return 200, {"link": '<https://evil.example/pulls?page=3>; rel="last", <https://api.github.com/items?page=2>; rel="next"'}, b'[{"id":1}]'
        result = parallel_index_pages(self.api(send), "/items", "test", 4)
        self.assertEqual("error", result["status"])

    def test_growing_range_is_partial(self):
        fake, send = self.paginated_fake()
        def growing(method, endpoint, payload, accept, token):
            status, headers, body = send(method, endpoint, payload, accept, token)
            if parse_qs(urlsplit(endpoint).query)["page"] == ["4"]:
                headers["link"] = '<https://api.github.com/repos/o/r/pulls?per_page=100&page=5>; rel="next"'
            return status, headers, body
        result = parallel_index_pages(self.api(growing), "/repos/o/r/pulls", "test", 4)
        self.assertEqual("partial", result["status"])
        self.assertIn("page_range_grew_during_pass", result["reasons"])
