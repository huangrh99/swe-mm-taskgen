"""Opt-in bounded parallel REST index pages, with durable per-page checkpoints."""

from concurrent.futures import ThreadPoolExecutor, as_completed
import re
from urllib.parse import parse_qsl, urlencode, urlsplit

from .api import API, APIError
from .store import Store


def link_url(headers, relation):
    matches = re.findall(r'<([^>]+)>;\s*rel="' + relation + '"', headers.get("link", ""))
    if not matches:
        return None
    url = urlsplit(matches[0])
    if url.scheme != "https" or url.netloc != "api.github.com":
        raise APIError("Unsafe pagination origin")
    return url


def parallel_index_pages(api, endpoint, scope, workers):
    from .core import rest_pages, section
    first_url = endpoint + ("&" if "?" in endpoint else "?") + "per_page=100&page=1"
    try:
        first = api.request(first_url, scope=scope)
        last = link_url(first["headers"], "last")
        next_url = link_url(first["headers"], "next")
        if last is None or next_url is None:
            return rest_pages(api, endpoint, scope=scope)
        # GitHub commonly canonicalizes /repos/owner/name to /repositories/ID
        # in Link headers. Both links must agree, but need not echo input path.
        if next_url.path != last.path:
            raise APIError("Pagination path changed")
        last_page = int(dict(parse_qsl(last.query))["page"])
        if not 2 <= last_page <= 100000:
            raise APIError("Invalid last page bound")
    except (ValueError, KeyError):
        return section(status="error", reason="Invalid last-page link", pages=0, observed_count=0)
    except APIError as exc:
        return section(**exc.info(), pages=0, observed_count=0)

    def fetch(page):
        # Preserve GitHub's query ordering so existing serial caches are reused.
        pairs = [(key, str(page) if key == "page" else value) for key, value in parse_qsl(next_url.query)]
        url = next_url.path + "?" + urlencode(pairs)
        store = Store(api.store.directory)
        child = API(store, api.run_id, api.token, api.send, api.sleep)
        try:
            response = child.request(url, scope=scope)
            return response, child.response_ids, None
        except APIError as exc:
            return None, child.response_ids, exc.info()
        finally:
            store.close()

    responses = {1: first}
    failures = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch, page): page for page in range(2, last_page + 1)}
        for future in as_completed(futures):
            page = futures[future]
            response, ids, error = future.result()
            api.response_ids.update(ids)
            if error:
                failures[page] = error
            else:
                responses[page] = response
    items, seen, reasons = [], set(), []
    for page, response in sorted(responses.items()):
        values = response["data"]
        if not isinstance(values, list):
            failures[page] = {"status": "error", "reason": "Expected a list response"}
            continue
        for value in values:
            identity = value.get("id")
            if identity is None:
                failures[page] = {"status": "error", "reason": "Missing PR identity"}
                continue
            if identity in seen:
                reasons.append("duplicate_ids_or_concurrent_change")
            else:
                seen.add(identity)
                items.append(value)
        try:
            following = link_url(response["headers"], "next")
            if page == last_page and following:
                reasons.append("page_range_grew_during_pass")
            if page < last_page and not following:
                reasons.append("page_range_shrank_during_pass")
        except APIError as exc:
            failures[page] = exc.info()
    if failures:
        reasons.append("failed_pages")
    return section(items, "partial" if reasons else "complete", pages=len(responses),
                   expected_pages=last_page, observed_count=len(items), expected_count=None,
                   reasons=sorted(set(reasons)), failed_pages=failures, page_workers=workers)
