import hashlib
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from pr_crawler.api import API, APIError, retry_delay
from analysis.scripts.step_11_01_retry_failed_assets import recover
from pr_crawler.assets import apply_recovery, bounded_download, discover, download, public_target
from pr_crawler.core import (_download_discovered_assets, collect_pr, gql_connection,
                             index_repository, references, repository, rest_pages, select)
from pr_crawler.store import Store
from pr_crawler.__main__ import choose, execute, main, report


def pr(number=1):
    return {"id": number, "node_id": f"PR{number}", "number": number, "title": "Fix render #2",
            "body": "Closes #2 ![before](https://example.com/image.png)", "user": {"login": "author"},
            "state": "closed" if number % 2 else "open", "draft": number % 3 == 0,
            "merged": number % 2 == 1, "created_at": "2020-01-01T00:00:00Z",
            "updated_at": "2020-02-01T00:00:00Z", "merged_at": "2020-02-01T00:00:00Z" if number % 2 else None,
            "base": {"sha": "base", "ref": "main", "repo": {"full_name": "o/r"}},
            "head": {"sha": "head", "ref": "fix", "repo": None}, "merge_commit_sha": "merge",
            "comments": 2, "review_comments": 1, "commits": 2, "changed_files": 2}


class FakeGitHub:
    def __init__(self, count=3):
        self.count, self.calls = count, []
        self.fail_page = None
        self.changed_comment = False

    def __call__(self, method, endpoint, payload, accept, token):
        self.calls.append((method, endpoint, payload))
        path = urlsplit(endpoint).path
        query = parse_qs(urlsplit(endpoint).query)
        page = int(query.get("page", [1])[0])
        if path == "/graphql":
            source, variables = payload["query"], payload["variables"]
            if "includeClosed" in source:
                return 200, {}, b'{"errors":[{"message":"Unsupported argument includeClosed"}]}'
            if "closingIssuesReferences" in source:
                values = [{"id": "I2", "number": 2, "title": "Problem", "url": "https://github.com/o/r/issues/2", "repository": {"nameWithOwner": "o/r"}}]
            elif "reviewThreads" in source:
                values = [{"id": "T1", "isResolved": True, "isOutdated": False, "path": "a.js", "line": 1}]
            else:
                values = [{"id": "C1", "databaseId": 21, "body": "thread note", "createdAt": "2020-01-01T01:00:00Z"}]
            connection = {"nodes": values, "totalCount": len(values), "pageInfo": {"hasNextPage": False, "endCursor": None}}
            parent = {"node": {"connection": connection}} if "id" in variables else {"repository": {"pullRequest": {"connection": connection}}}
            return 200, {}, json.dumps({"data": parent}).encode()
        if path == "/repos/o/r/pulls":
            if self.fail_page == page:
                raise APIError("simulated interruption")
            values = [pr(n) for n in range(1, self.count + 1)]
        elif path == "/repos/o/r/pulls/1":
            if "json" not in accept:
                return 200, {}, b"diff --git a/a.js b/a.js\n--- a/a.js\n+++ b/a.js\n@@ -1 +1 @@\n-old\n+new\n"
            return 200, {}, json.dumps(pr()).encode()
        elif path == "/repos/o/r/issues/2":
            return 200, {}, json.dumps({"id": 2, "number": 2, "title": "Problem", "body": "Original issue", "comments": 1}).encode()
        elif path.endswith("/labels"):
            values = [{"id": n, "name": f"label{n}"} for n in range(105)]
        elif path == "/repos/o/r/issues/1/comments":
            values = [{"id": 10, "body": "edited" if self.changed_comment else "comment"}, {"id": 11, "body": "other"}]
        elif path == "/repos/o/r/issues/2/comments":
            values = [{"id": 12, "body": "issue comment"}]
        elif path.endswith("/reviews"):
            values = [{"id": 20, "body": "review", "state": "APPROVED"}]
        elif path.endswith("/comments"):
            values = [{"id": 21, "body": "inline", "path": "a.js", "line": 1}]
        elif path.endswith("/commits"):
            values = [{"sha": s, "commit": {"message": "fix"}, "parents": [{"sha": "base"}]} for s in ("sha1", "sha2")]
        elif path.endswith("/files"):
            # Identical blob hashes across different paths must not deduplicate files.
            values = [{"sha": "same", "filename": "a.js", "status": "modified", "patch": "@@ -1 +1 @@\n-old\n+new"},
                      {"sha": "same", "filename": "b.js", "status": "renamed", "previous_filename": "c.js"}]
        else:
            return 404, {}, b'{"message":"Not Found"}'
        rows = values[(page - 1) * 100:page * 100]
        headers = {}
        if len(values) > page * 100:
            params = {k: v[0] for k, v in query.items()}
            params.update(page=str(page + 1), per_page="100")
            from urllib.parse import urlencode
            headers["link"] = f'<https://api.github.com{path}?{urlencode(params)}>; rel="next"'
        return 200, headers, json.dumps(rows).encode()


class BaseTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.settings = {"command": "crawl", "repos": ["o/r"], "axis": "created_at", "start": None,
                         "end": None, "download_assets": False, "max_asset_bytes": 10000}
        self.run = self.store.new_run(self.settings)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def api(self, fake=None):
        return API(self.store, self.run, token="never-store-this", send=fake or FakeGitHub(), sleep=lambda _: None)


class TimeAndInputTests(unittest.TestCase):
    def test_time_boundaries(self):
        rows = [pr(), dict(pr(2), created_at="2020-01-02T00:00:00Z")]
        self.assertEqual([1], [x["number"] for x in select(rows, start="2020-01-01", end="2020-01-02")])
        self.assertEqual([1], [x["number"] for x in select(rows, "merged_at")])
        self.assertEqual([1, 2], [x["number"] for x in select(rows, "updated_at", start="2020-02-01T08:00:00+08:00")])
        for start, end in [("2020-01-01T00:00:00", None), ("2020-03-01", "2020-01-01"), ("nonsense", None), ("2020-01-01", "2020-01-01")]:
            with self.assertRaises(ValueError):
                select(rows, start=start, end=end)

    def test_repository_validation(self):
        self.assertEqual("o/r", repository("https://github.com/O/R.git"))
        for value in ("../r", "o/..", "https://evil.com/o/r", "o/r?token=x", "o/r/issues/1", "o/r#1"):
            with self.assertRaises(ValueError):
                repository(value)

    def test_reference_provenance(self):
        found = references("o/r", 1, [("body", "#2 x/y#3 https://github.com/a/b/issues/4 #1")])
        self.assertEqual({("o/r", 2), ("x/y", 3), ("a/b", 4)}, set(found))
        self.assertTrue(all(x["relationship"] == "text_reference" for x in found.values()))

    def test_invalid_untrusted_references_are_isolated(self):
        rejected = []
        found = references("o/r", 1, [("body", "../repo#4 foo_bar/repo#123 #2")], rejected)
        self.assertEqual({("o/r", 2)}, set(found))
        self.assertEqual(2, len(rejected))
        found = references("o/r", 1, [("body", "#" + "1" * 4500 + " #2")], rejected)
        self.assertEqual({("o/r", 2)}, set(found))
        self.assertEqual("invalid_number", rejected[-1]["reason"])


class PaginationTests(BaseTest):
    def test_more_than_1000_index_all_states_and_two_passes(self):
        fake = FakeGitHub(1105)
        result = index_repository(self.api(fake), "o/r")
        self.assertEqual("complete", result["status"])
        self.assertEqual(1105, len(result["items"]))
        self.assertEqual(24, len(fake.calls))
        self.assertTrue(any(r["draft"] for r in result["items"]))
        self.assertEqual({"open", "closed"}, {r["state"] for r in result["items"]})
        self.assertTrue(all("state=all" in c[1] for c in fake.calls))

    def test_nested_rest_and_files_identity(self):
        api = self.api()
        self.assertEqual(105, len(rest_pages(api, "/repos/o/r/issues/1/labels")["items"]))
        files = rest_pages(api, "/repos/o/r/pulls/1/files", expected=2)
        self.assertEqual("complete", files["status"])
        self.assertEqual(2, len(files["items"]))

    def test_graphql_nested_pagination(self):
        def send(method, endpoint, payload, accept, token):
            cursor = payload["variables"]["cursor"]
            values = [{"id": str(n)} for n in (range(100) if cursor is None else range(100, 103))]
            conn = {"nodes": values, "totalCount": 103, "pageInfo": {"hasNextPage": cursor is None, "endCursor": "next"}}
            parent = {"node": {"connection": conn}} if "id" in payload["variables"] else {"repository": {"pullRequest": {"connection": conn}}}
            return 200, {}, json.dumps({"data": parent}).encode()
        api = self.api(send)
        for thread in (None, "thread1"):
            result = gql_connection(api, "o/r", 1, "reviewThreads", "id", thread_id=thread)
            self.assertEqual(("complete", 103, 2), (result["status"], len(result["items"]), result["pages"]))

    def test_caps_are_not_silent(self):
        for cap, expected in ((250, 251), (3000, 3001)):
            def send(method, endpoint, payload, accept, token):
                page = int(parse_qs(urlsplit(endpoint).query)["page"][0])
                values = [{"id": n} for n in range((page-1)*100, min(page*100, cap))]
                headers = {"link": f'<https://api.github.com/items?per_page=100&page={page+1}>; rel="next"'} if page*100 < cap else {}
                return 200, headers, json.dumps(values).encode()
            result = rest_pages(self.api(send), "/items", scope=str(cap), cap=cap, expected=expected)
            self.assertEqual("partial", result["status"])
            self.assertIn("endpoint_cap", result["reasons"])

    def test_resume_after_page_failure_reuses_durable_pages(self):
        fake = FakeGitHub(201)
        fake.fail_page = 2
        result = rest_pages(self.api(fake), "/repos/o/r/pulls")
        self.assertEqual("error", result["status"])
        self.assertEqual(100, len(result["items"]))
        fake.fail_page = None
        fake.calls.clear()
        result = rest_pages(self.api(fake), "/repos/o/r/pulls")
        self.assertEqual(201, len(result["items"]))
        self.assertEqual(2, len(fake.calls))
        self.assertEqual(["2", "3"], [parse_qs(urlsplit(c[1]).query)["page"][0] for c in fake.calls])

    def test_unsafe_or_looping_pagination(self):
        for url in ("https://evil.com/path", "https://api.github.com/items?per_page=100&page=1"):
            def send(*args):
                return 200, {"link": f'<{url}>; rel="next"'}, b'[{"id":1}]'
            result = rest_pages(self.api(send), "/items", scope=url)
            self.assertEqual("error", result["status"])

    def test_duplicate_and_count_mismatch(self):
        result = rest_pages(self.api(lambda *a: (200, {}, b'[{"id":1},{"id":1}]')), "/items", expected=2)
        self.assertEqual("partial", result["status"])
        self.assertEqual(1, len(result["items"]))

    def test_concurrent_index_change_not_complete(self):
        fake = FakeGitHub(2)
        count = 0
        def send(*args):
            nonlocal count
            count += 1
            status, headers, body = fake(*args)
            if count == 2:
                values = json.loads(body)
                values[0]["updated_at"] = "2021-01-01T00:00:00Z"
                body = json.dumps(values).encode()
            return status, headers, body
        self.assertEqual("partial", index_repository(self.api(send), "o/r")["status"])


class TransportAndStorageTests(BaseTest):
    def test_chunked_index_roundtrip_and_replacement(self):
        value = {"status": "complete", "items": [{"id": n} for n in range(1105)]}
        self.store.put(self.run, "index/o/r", value)
        self.assertEqual(3, self.store.db.execute("SELECT count(*) FROM document_chunks").fetchone()[0])
        self.assertEqual(value, self.store.get(self.run, "index/o/r"))
        self.assertEqual(value, self.store.documents(self.run)["index/o/r"])
        self.store.put(self.run, "index/o/r", {"status": "complete", "items": []})
        self.assertEqual(0, self.store.db.execute("SELECT count(*) FROM document_chunks").fetchone()[0])
        self.assertEqual([], self.store.get(self.run, "index/o/r")["items"])

    def test_missing_index_chunk_is_not_silent(self):
        self.store.put(self.run, "index/o/r", {"items": [{"id": n} for n in range(501)]})
        with self.store.db:
            self.store.db.execute("DELETE FROM document_chunks WHERE chunk_number=0")
        with self.assertRaisesRegex(ValueError, "Missing index chunk"):
            self.store.get(self.run, "index/o/r")

    def test_chunking_stays_below_sqlite_single_value_limit(self):
        import sqlite3
        value = {"items": [{"id": n, "body": "x" * 300} for n in range(20)]}
        self.store.INDEX_CHUNK_SIZE = 2
        self.store.db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1024)
        self.store.put(self.run, "index/o/r", value)
        self.assertEqual(value, self.store.get(self.run, "index/o/r"))

    def test_auth_not_in_raw_cache_and_hashes_correct(self):
        api = self.api()
        api.request("/repos/o/r/pulls/1")
        rows = self.store.db.execute("SELECT * FROM responses").fetchall()
        self.assertEqual(1, len(rows))
        self.assertNotIn("never-store-this", str(dict(rows[0])))
        self.assertEqual(rows[0]["sha256"], hashlib.sha256(rows[0]["body"]).hexdigest())

    def test_rate_limit_cooldown_no_hammering(self):
        calls = []
        def send(*args):
            calls.append(args)
            return 429, {"retry-after": "120"}, b'{"message":"rate limit"}'
        api = self.api(send)
        for _ in range(2):
            with self.assertRaises(APIError) as cm:
                api.request("/items")
            self.assertIsNotNone(cm.exception.retry_at)
        self.assertEqual(1, len(calls))

    def test_rate_limit_persists_across_objects_and_new_runs(self):
        calls = []
        def send(*args):
            calls.append(args)
            return 200, {"retry-after": "120"}, b'{"data":null,"errors":[{"type":"RATE_LIMITED","message":"API rate limit exceeded"}]}'
        for _ in range(2):
            with self.assertRaises(APIError) as cm:
                self.api(send).request("/graphql", {"query": "query {viewer{login}}"})
            self.assertIsNotNone(cm.exception.retry_at)
            self.run = self.store.new_run(self.settings)
        self.assertEqual(1, len(calls))

    def test_retry_transient_and_reject_mutation(self):
        calls = []
        def send(*args):
            calls.append(args)
            return (500, {}, b'{}') if len(calls) < 3 else (200, {}, b'[]')
        self.assertEqual([], self.api(send).request("/items")["data"])
        self.assertEqual(3, len(calls))
        with self.assertRaises(APIError):
            self.api().request("/graphql", {"query": "mutation {x}"})

    def test_graphql_partial_error_is_retained_not_reused(self):
        calls = []
        def send(*args):
            calls.append(args)
            return 200, {}, b'{"data":{"repository":null},"errors":[{"message":"denied"}]}'
        api = self.api(send)
        for _ in range(2):
            with self.assertRaises(APIError):
                api.request("/graphql", {"query": "query {viewer{login}}"})
        self.assertEqual(2, len(calls))
        self.assertEqual(2, self.store.db.execute("SELECT count(*) FROM responses").fetchone()[0])

    def test_retry_after_http_date(self):
        with patch("pr_crawler.api.time.time", return_value=0):
            self.assertEqual(60, retry_delay({"retry-after": "Thu, 01 Jan 1970 00:01:00 GMT"}, 0))


class EndToEndTests(BaseTest):
    def test_rich_record_raw_provenance_and_idempotent_resume(self):
        fake = FakeGitHub()
        record = collect_pr(self.api(fake), "o/r", 1)
        self.assertEqual("complete", record["status"])
        self.assertEqual("not_requested", record["sections"]["assets"]["status"])
        self.assertEqual("closes", record["sections"]["linked_issues"]["items"][0]["relationship"])
        self.assertEqual(105, len(record["sections"]["labels"]["items"]))
        self.assertTrue(record["sections"]["review_threads"]["items"][0]["isResolved"])
        self.assertEqual("missing_binary_or_omitted", record["sections"]["files"]["items"][1]["patch_status"])
        self.assertIsNone(record["derived"]["base_commit"])
        self.assertTrue(record["provenance"]["response_ids"])
        count = len(fake.calls)
        resumed = collect_pr(self.api(fake), "o/r", 1)
        self.assertEqual(count, len(fake.calls))
        self.assertEqual(record["sections"], resumed["sections"])
        self.assertEqual(1, self.store.db.execute("SELECT count(*) FROM documents WHERE name LIKE 'pr/%'").fetchone()[0])

    def test_unverified_reference_failure_is_recorded_but_does_not_block_core_archive(self):
        fake = FakeGitHub()

        def send(method, endpoint, payload, accept, token):
            if endpoint == "/graphql" and "closingIssuesReferences" in payload["query"]:
                connection = {"nodes": [], "totalCount": 0,
                              "pageInfo": {"hasNextPage": False, "endCursor": None}}
                body = {"data": {"repository": {"pullRequest": {"connection": connection}}}}
                return 200, {}, json.dumps(body).encode()
            if endpoint == "/repos/o/r/pulls/1" and "json" in accept:
                value = pr()
                value["title"] = "Fix render"
                value["body"] = "See policy https://github.com/o/r/issues/420918"
                return 200, {}, json.dumps(value).encode()
            if endpoint == "/repos/o/r/issues/420918":
                return 404, {}, b'{"message":"Not Found"}'
            return fake(method, endpoint, payload, accept, token)

        record = collect_pr(self.api(send), "o/r", 1)
        self.assertEqual("complete", record["status"])
        self.assertEqual("complete", record["sections"]["linked_issues"]["status"])
        linked = record["sections"]["linked_issues"]["items"][0]
        self.assertFalse(linked["required_for_source_complete"])
        self.assertEqual("curator_reference_only", linked["source_scope"])
        self.assertEqual("unavailable", linked["detail"]["status"])
        self.assertEqual(
            ["o/r#420918"],
            record["sections"]["linked_issues"]["unresolved_optional_references"])

    def test_refresh_child_without_parent_timestamp_change(self):
        fake = FakeGitHub()
        first = collect_pr(self.api(fake), "o/r", 1)
        old_run = self.run
        self.run = self.store.new_run(self.settings)
        fake.changed_comment = True
        second = collect_pr(self.api(fake), "o/r", 1)
        self.assertEqual("comment", first["sections"]["comments"]["items"][0]["body"])
        self.assertEqual("edited", second["sections"]["comments"]["items"][0]["body"])
        self.assertEqual(first, self.store.get(old_run, "pr/o/r/1"))

    def test_resume_rechecks_boundary_after_new_requests(self):
        fake = FakeGitHub()
        changed = False
        def send(method, endpoint, payload, accept, token):
            if endpoint.startswith("/repos/o/r/pulls/1/files") and not changed:
                return 500, {}, b'{}'
            status, headers, body = fake(method, endpoint, payload, accept, token)
            if endpoint == "/repos/o/r/pulls/1" and "json" in accept and changed:
                value = json.loads(body)
                value["head"]["sha"] = "new-head"
                body = json.dumps(value).encode()
            return status, headers, body
        first = collect_pr(self.api(send), "o/r", 1)
        self.assertEqual("partial", first["status"])
        self.assertEqual("partial", first["sections"]["assets"]["manifest_status"])
        self.assertIn("files", first["sections"]["assets"]["missing_sources"])
        changed = True
        resumed = collect_pr(self.api(send), "o/r", 1)
        self.assertEqual("partial", resumed["status"])
        self.assertEqual("partial", resumed["sections"]["consistency"]["status"])
        self.assertEqual("new-head", resumed["sections"]["consistency"]["end_pull_request"]["data"]["head"]["sha"])

    def test_three_stage_resume_never_reuses_old_successful_probe(self):
        fake = FakeGitHub()
        stage = 0
        def send(method, endpoint, payload, accept, token):
            if endpoint.startswith("/repos/o/r/pulls/1/files") and stage == 0:
                return 500, {}, b'{}'
            if endpoint == "/repos/o/r/pulls/1" and "json" in accept and stage == 1:
                return 500, {}, b'{}'
            status, headers, body = fake(method, endpoint, payload, accept, token)
            if endpoint == "/repos/o/r/pulls/1" and "json" in accept and stage == 2:
                value = json.loads(body)
                value["head"]["sha"] = "new-head"
                body = json.dumps(value).encode()
            return status, headers, body
        for stage in range(3):
            record = collect_pr(self.api(send), "o/r", 1)
            self.assertEqual("partial", record["status"], f"stage {stage}")
        self.assertEqual("new-head", record["sections"]["consistency"]["end_pull_request"]["data"]["head"]["sha"])

    def test_interrupt_after_material_commit_before_verification(self):
        fake = FakeGitHub()
        stage = 0
        def send(method, endpoint, payload, accept, token):
            if endpoint.startswith("/repos/o/r/pulls/1/files") and stage == 0:
                return 500, {}, b'{}'
            if endpoint == "/repos/o/r/pulls/1" and "json" in accept and stage == 1:
                raise KeyboardInterrupt()
            status, headers, body = fake(method, endpoint, payload, accept, token)
            if endpoint == "/repos/o/r/pulls/1" and "json" in accept and stage == 2:
                value = json.loads(body)
                value["head"]["sha"] = "new-head"
                body = json.dumps(value).encode()
            return status, headers, body
        self.assertEqual("partial", collect_pr(self.api(send), "o/r", 1)["status"])
        stage = 1
        with self.assertRaises(KeyboardInterrupt):
            collect_pr(self.api(send), "o/r", 1)
        stage = 2
        result = collect_pr(self.api(send), "o/r", 1)
        self.assertEqual("partial", result["status"])
        self.assertEqual("new-head", result["sections"]["consistency"]["end_pull_request"]["data"]["head"]["sha"])

    def test_report_and_offline_selection(self):
        idx = index_repository(self.api(FakeGitHub(3)), "o/r")
        selection = choose([idx], {"prs": ["o/r#1", "o/r#999"]})
        self.assertEqual("partial", selection["status"])
        self.assertEqual(["o/r#999"], selection["missing_requested_prs"])
        self.store.put(self.run, "selection", selection)
        collect_pr(self.api(), "o/r", 1)
        result = report(self.store, self.run)
        self.assertEqual(1, result["archived_detail_count"])
        self.assertTrue((Path(self.temp.name) / "exports" / self.run / "report.md").exists())
        with patch("pr_crawler.__main__.credential", side_effect=AssertionError("offline must not access credentials")):
            self.assertEqual(0, main(["select", "--output", self.temp.name, "--run", self.run, "--axis", "merged_at"]))

    def test_real_cli_orchestration_with_fake_api(self):
        self.settings["command"] = "enrich"
        self.settings["source_run"] = self.run
        self.settings["prs"] = ["o/r#1"]
        index_repository(self.api(), "o/r")
        target = self.store.new_run(self.settings)
        fake = FakeGitHub()
        with patch("pr_crawler.__main__.API", side_effect=lambda s, r, t: API(s, r, t, send=fake, sleep=lambda _: None)):
            self.assertEqual(0, execute(self.store, target))
            count = len(fake.calls)
            self.assertEqual(0, execute(self.store, target))
            self.assertEqual(count, len(fake.calls))

    def test_multiple_repository_indexes_remain_separate(self):
        settings = {**self.settings, "command": "index", "repos": ["o/r", "x/y"]}
        target = self.store.new_run(settings)
        fake = FakeGitHub(3)
        def send(method, endpoint, payload, accept, token):
            return fake(method, endpoint.replace("/repos/x/y", "/repos/o/r"), payload, accept, token)
        with patch("pr_crawler.__main__.API", side_effect=lambda s, r, t: API(s, r, t, send=send, sleep=lambda _: None)):
            self.assertEqual(0, execute(self.store, target))
        self.assertEqual(3, len(self.store.get(target, "index/o/r")["items"]))
        self.assertEqual(3, len(self.store.get(target, "index/x/y")["items"]))
        self.assertEqual(6, len(self.store.get(target, "selection")["items"]))


def addresses(host, port, **kwargs):
    return [(2, 1, 6, "", ("93.184.216.34", port))]


def stalled_worker(pipe, entry, directory, max_bytes):
    time.sleep(10)


class AssetTests(unittest.TestCase):
    def test_discovered_assets_download_concurrently_but_persist_in_source_order(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            run_id = store.new_run({"purpose": "asset-concurrency-test"})
            api = SimpleNamespace(store=store, run_id=run_id)
            assets = [{"url": f"https://example.com/{index}.png",
                       "sources": [f"source:{index}"], "status": "not_requested"}
                      for index in range(4)]
            lock = threading.Lock()
            active = maximum = 0

            def fake_download(asset, _directory, _limit):
                nonlocal active, maximum
                with lock:
                    active += 1
                    maximum = max(maximum, active)
                time.sleep(0.03)
                with lock:
                    active -= 1
                return {**asset, "status": "complete", "sha256": str(asset["url"]),
                        "bytes": 1, "media_type": "image/png"}

            try:
                with patch("pr_crawler.core.download", side_effect=fake_download):
                    result = _download_discovered_assets(api, assets, 1024, 4)
                self.assertGreaterEqual(maximum, 2)
                self.assertEqual([item["url"] for item in assets],
                                 [item["url"] for item in result])
                self.assertTrue(all(store.get(run_id, "asset/" + hashlib.sha256(
                    item["url"].encode()).hexdigest()) for item in assets))
            finally:
                store.close()

    def test_stage11_recovery_is_append_only_and_hash_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            record = run / '11_record_0001.json'
            value = {'repo': 'o/r', 'number': 1, 'sections': {'assets': {'items': [
                {'url': 'https://example.com/a.png', 'status': 'error',
                 'reason': 'ConnectionResetError'}]}}}
            record.write_text(json.dumps(value))
            original = record.read_bytes()
            manifest = {'files': {record.name: hashlib.sha256(original).hexdigest()}}
            (run / '11_manifest.json').write_text(json.dumps(manifest))
            def fake(asset, destination):
                payload = b'png'
                sha = hashlib.sha256(payload).hexdigest()
                target = Path(destination) / 'assets' / sha
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
                return {**asset, 'status': 'complete', 'sha256': sha, 'bytes': len(payload),
                    'media_type': 'image/png', 'local_path': 'assets/' + sha,
                    'fetched_at': '2026-09-02T00:00:00Z', 'attempt_count': 1,
                    'download_attempts': [{'attempt': 1, 'status': 'complete'}]}
            output = recover(run, fake)
            self.assertEqual(original, record.read_bytes())
            self.assertEqual(1, json.loads(output.read_text())['counts']['complete'])
            overlaid = apply_recovery(record, value['sections']['assets']['items'][0])
            self.assertEqual('complete', overlaid['status'])
            self.assertEqual('ConnectionResetError', overlaid['recovered_from_reason'])

    def test_transient_asset_failure_is_retried_and_audited(self):
        class Response:
            status = 200
            sent = False
            def getheader(self, name):
                return {"Content-Type": "image/png", "Content-Length": "3"}.get(name)
            def read(self, size):
                if self.sent:
                    return b""
                self.sent = True
                return b"png"
        class Connection:
            responses = 0
            def __init__(self, *args):
                pass
            def request(self, *args, **kwargs):
                pass
            def getresponse(self):
                type(self).responses += 1
                if self.responses == 1:
                    raise ConnectionResetError()
                return Response()
            def close(self):
                pass
        with tempfile.TemporaryDirectory() as directory:
            result = download({"url": "https://example.com/a.png"}, directory,
                              connector=Connection, resolver=addresses, sleep=lambda _: None)
            self.assertEqual("complete", result["status"])
            self.assertEqual(2, result["attempt_count"])
            self.assertEqual("ConnectionResetError", result["download_attempts"][0]["reason"])
            self.assertEqual("complete", result["download_attempts"][1]["status"])

    def test_deterministic_asset_failure_is_not_retried(self):
        class Response:
            status = 200
            def getheader(self, name):
                return "text/html" if name == "Content-Type" else None
        class Connection:
            responses = 0
            def __init__(self, *args):
                pass
            def request(self, *args, **kwargs):
                pass
            def getresponse(self):
                type(self).responses += 1
                return Response()
            def close(self):
                pass
        with tempfile.TemporaryDirectory() as directory:
            result = download({"url": "https://example.com/a"}, directory,
                              connector=Connection, resolver=addresses, sleep=lambda _: None)
            self.assertEqual("error", result["status"])
            self.assertEqual(1, result["attempt_count"])
            self.assertEqual(1, Connection.responses)

    def test_discovery_and_source_locations(self):
        assets = discover([("body", '![a](https://example.com/x.png) <video src="https://example.com/v"></video>'),
                           ("comment:1", "https://example.com/x.png https://github.com/user-attachments/assets/uuid")])
        self.assertEqual(3, len(assets))
        self.assertEqual(["body", "comment:1"], assets[0]["sources"])

    def test_bad_media_url_is_recorded_not_fatal(self):
        assets = discover([("body", "![img](https://[broken/x.png) ![ok](https://example.com/good.png)")])
        self.assertEqual(2, len(assets))
        self.assertEqual("error", assets[0]["status"])

    def test_wall_clock_deadline_terminates_stalled_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            start = time.monotonic()
            result = bounded_download({"url": "https://example.com/a.png"}, directory, 100, timeout=0.2, worker=stalled_worker)
            self.assertEqual("error", result["status"])
            self.assertIn("deadline", result["reason"])
            self.assertLess(time.monotonic() - start, 3)
            self.assertEqual([], list(Path(directory).iterdir()))

    def test_reject_unsafe_protocol_userinfo_port_and_ips(self):
        for url in ("http://example.com/a.png", "file:///tmp/x", "https://a:b@example.com/x", "https://example.com:8443/x"):
            with self.assertRaises(ValueError):
                public_target(url, addresses)
        for ip in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1"):
            with self.assertRaises(ValueError):
                public_target("https://example.com/x", lambda *a, **k: [(2, 1, 6, "", (ip, 443))])

    def test_download_hash_and_dedup_no_path_traversal(self):
        class Response:
            status = 200
            def __init__(self):
                self.sent = False
            def getheader(self, name):
                return {"Content-Type": "image/png", "Content-Length": "3"}.get(name)
            def read(self, size):
                if self.sent:
                    return b""
                self.sent = True
                return b"png"
        class Connection:
            calls = []
            def __init__(self, host, address):
                self.calls.append((host, address))
            def request(self, *args, **kwargs):
                self.calls.append(kwargs)
            def getresponse(self):
                return Response()
            def close(self):
                pass
        with tempfile.TemporaryDirectory() as directory:
            entry = {"url": "https://example.com/../../unsafe.png", "sources": ["body"]}
            for _ in range(2):
                result = download(entry, directory, connector=Connection, resolver=addresses)
                self.assertEqual("complete", result["status"])
                self.assertEqual(hashlib.sha256(b"png").hexdigest(), result["sha256"])
            self.assertEqual(1, len(list((Path(directory) / "assets").iterdir())))
            self.assertNotIn("Authorization", str(Connection.calls))
            original = Response.getheader
            Response.getheader = lambda self, name: "100" if name == "Content-Length" else original(self, name)
            truncated = download(entry, directory, connector=Connection, resolver=addresses)
            self.assertEqual("error", truncated["status"])
            self.assertIn("Content-Length mismatch", truncated["reason"])

    def test_size_limit_and_redirect_revalidation(self):
        class Response:
            status = 302
            def getheader(self, name):
                return "https://127.0.0.1/secret" if name == "Location" else None
        class Connection:
            def __init__(self, *a):
                pass
            def request(self, *a, **k):
                pass
            def getresponse(self):
                return Response()
            def close(self):
                pass
        def resolver(host, port, **kwargs):
            return [(2, 1, 6, "", ("127.0.0.1" if host == "127.0.0.1" else "93.184.216.34", port))]
        with tempfile.TemporaryDirectory() as directory:
            result = download({"url": "https://example.com/a"}, directory, connector=Connection, resolver=resolver)
            self.assertEqual("error", result["status"])
            self.assertIn("public", result["reason"])
            Response.status = 200
            Response.getheader = lambda self, name: {"Content-Type": "image/png", "Content-Length": "100"}.get(name)
            result = download({"url": "https://example.com/a"}, directory, max_bytes=10, connector=Connection, resolver=resolver)
            self.assertEqual("Asset exceeds size limit", result["reason"])


if __name__ == "__main__":
    unittest.main()
