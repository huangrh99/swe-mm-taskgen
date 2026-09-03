"""Enumeration, independent nested pagination, and evidence-preserving enrichment."""

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlsplit

from . import SCHEMA_VERSION
from .api import APIError, API_VERSION
from .assets import discover, download
from .store import now


def repository(value):
    if value.startswith("https://github.com/"):
        value = value.removeprefix("https://github.com/").rstrip("/")
    if value.endswith(".git"):
        value = value[:-4]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", value):
        raise ValueError("Repository must be owner/name or an HTTPS github.com repository URL")
    if value.split("/")[1] in (".", ".."):
        raise ValueError("Invalid repository name")
    return value.lower()


def timestamp(value):
    if value is None:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        value += "T00:00:00+00:00"
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp requires an explicit timezone; date-only means midnight UTC")
    return parsed.astimezone(timezone.utc)


def select(rows, axis="created_at", start=None, end=None):
    if axis not in ("created_at", "updated_at", "merged_at"):
        raise ValueError("Unsupported time axis")
    lo, hi = timestamp(start), timestamp(end)
    if lo and hi and lo >= hi:
        raise ValueError("start must precede end")
    selected = []
    for row in rows:
        value = timestamp(row.get(axis))
        if value is not None and (lo is None or value >= lo) and (hi is None or value < hi):
            selected.append(row)
    return selected


def section(items=None, status="complete", **metadata):
    return {"status": status, "items": items or [], **metadata}


def rest_pages(api, endpoint, scope="", cap=None, expected=None, anonymous=False):
    items, ids, seen_urls = [], set(), set()
    url = endpoint + ("&" if "?" in endpoint else "?") + "per_page=100&page=1"
    pages, duplicate = 0, False
    try:
        while url:
            if url in seen_urls:
                raise APIError("Repeated pagination URL")
            seen_urls.add(url)
            response = api.request(url, scope=scope)
            values = response["data"]
            if not isinstance(values, list):
                raise APIError("Expected a list response")
            pages += 1
            for item in values:
                identity = item.get("id", item.get("filename", item.get("sha", item.get("name"))))
                if anonymous:
                    identity = (item.get('event'), identity) if identity is not None else hashlib.sha256(
                        json.dumps(item, sort_keys=True).encode()).hexdigest()
                if identity is None:
                    raise APIError("Collection item lacks stable identity")
                if identity in ids:
                    duplicate = True
                    continue
                ids.add(identity)
                items.append(item)
            link = response["headers"].get("link", "")
            matches = re.findall(r'<([^>]+)>;\s*rel="next"', link)
            url = None
            if matches:
                parsed = urlsplit(matches[0])
                if parsed.scheme != "https" or parsed.netloc != "api.github.com":
                    raise APIError("Unsafe pagination origin")
                url = parsed.path + ("?" + parsed.query if parsed.query else "")
            if cap is not None and len(items) >= cap:
                break
        capped = cap is not None and len(items) >= cap and (bool(url) or expected is None or expected > len(items))
        mismatch = expected is not None and expected != len(items)
        reasons = (["endpoint_cap"] if capped else []) + (["count_mismatch"] if mismatch else []) + (["duplicate_ids_or_concurrent_change"] if duplicate else [])
        return section(items, "partial" if reasons else "complete", pages=pages, observed_count=len(items),
                       expected_count=expected, reasons=reasons)
    except APIError as exc:
        return section(items, **exc.info(), pages=pages, observed_count=len(items), expected_count=expected)


def gql_connection(api, repo, number, field, node_fields, scope="", thread_id=None):
    items, seen, cursors = [], set(), set()
    cursor, expected, pages = None, None, 0
    try:
        while True:
            if thread_id:
                query = "query($id:ID!,$cursor:String){node(id:$id){... on PullRequestReviewThread{connection:comments(first:100,after:$cursor){nodes{" + node_fields + "} totalCount pageInfo{hasNextPage endCursor}}}}}"
                variables = {"id": thread_id, "cursor": cursor}
            else:
                query = "query($owner:String!,$name:String!,$number:Int!,$cursor:String){repository(owner:$owner,name:$name){pullRequest(number:$number){connection:" + field + "(first:100,after:$cursor){nodes{" + node_fields + "} totalCount pageInfo{hasNextPage endCursor}}}}}"
                owner, name = repo.split("/")
                variables = {"owner": owner, "name": name, "number": number, "cursor": cursor}
            response = api.request("/graphql", {"query": query, "variables": variables}, scope=scope)["data"]
            try:
                parent = response["data"]["node"] if thread_id else response["data"]["repository"]["pullRequest"]
                connection = parent["connection"]
                nodes, info = connection["nodes"], connection["pageInfo"]
            except (KeyError, TypeError):
                raise APIError("Missing GraphQL connection") from None
            pages += 1
            expected = connection["totalCount"]
            for item in nodes:
                if item is None or item["id"] in seen:
                    raise APIError("Null or duplicate GraphQL node")
                seen.add(item["id"])
                items.append(item)
            if not info["hasNextPage"]:
                break
            cursor = info["endCursor"]
            if not cursor or cursor in cursors:
                raise APIError("Invalid GraphQL pagination cursor")
            cursors.add(cursor)
        return section(items, "complete" if len(items) == expected else "partial", pages=pages,
                       expected_count=expected, observed_count=len(items),
                       reasons=[] if len(items) == expected else ["count_mismatch"])
    except APIError as exc:
        return section(items, **exc.info(), pages=pages, observed_count=len(items), expected_count=expected)


def one(api, endpoint, scope="", accept="application/vnd.github+json"):
    try:
        response = api.request(endpoint, scope=scope, accept=accept)
        return {"status": "complete", "data": response["data"], "response_id": response["response_id"],
                "fetched_at": response["fetched_at"], "sha256": response["sha256"]}
    except APIError as exc:
        return exc.info()


def index_repository(api, repo, page_workers=1):
    start = api.store.run(api.run_id)["started_at"]
    endpoint = f"/repos/{repo}/pulls?state=all&sort=created&direction=asc"
    if page_workers == 1:
        first = rest_pages(api, endpoint, scope="index:first")
        second = rest_pages(api, endpoint, scope="index:reconcile")
    else:
        from .pagination import parallel_index_pages
        first = parallel_index_pages(api, endpoint, "index:first", page_workers)
        second = parallel_index_pages(api, endpoint, "index:reconcile", page_workers)
    def bounded(values):
        return [x for x in values if timestamp(x["created_at"]) < timestamp(start)]
    first_rows, rows = bounded(first["items"]), bounded(second["items"])
    identity = lambda values: sorted((r["id"], r["updated_at"]) for r in values)
    stable = first["status"] == second["status"] == "complete" and identity(first_rows) == identity(rows)
    # Never discard records observed only in the first pass during a concurrent change.
    union = {r["id"]: r for r in first_rows}
    union.update({r["id"]: r for r in rows})
    result = {"schema_version": SCHEMA_VERSION, "repo": repo, "cutoff": start,
              "observed_at": now(), "status": "complete" if stable else "partial",
              "consistency": "two_pass_observed_stable" if stable else "unreconciled",
              "atomic_snapshot": False, "passes": [{k: v for k, v in s.items() if k != "items"} for s in (first, second)],
              "items": sorted(union.values(), key=lambda r: r["number"])}
    api.store.put(api.run_id, "index/" + repo, result)
    return result


def references(repo, number, sources, rejected=None):
    candidates = {}
    for source, text in sources:
        if not isinstance(text, str):
            continue
        found = [(repo, n) for n in re.findall(r"(?<![\w/])#(\d+)\b", text)]
        found += re.findall(r"https://github\.com/([\w.-]+/[\w.-]+)/(?:issues|pull)/(\d+)\b", text)
        found += re.findall(r"(?<![\w/])([\w.-]+/[\w.-]+)#(\d+)\b", text)
        for name, num in found:
            if len(num) > 10 or not num.isascii() or not 0 < int(num) <= 2147483647:
                if rejected is not None:
                    rejected.append({"source": source, "reference": name + "#" + num[:40], "reason": "invalid_number"})
                continue
            num = int(num)
            try:
                name = repository(name)
            except ValueError:
                if rejected is not None:
                    rejected.append({"source": source, "reference": f"{name}#{num}", "reason": "invalid_repository"})
                continue
            if (name, num) == (repo, number) or num == 0:
                continue
            entry = candidates.setdefault((name, num), {"repo": name, "number": num,
                "relationship": "text_reference", "confidence": "unverified", "sources": []})
            if source not in entry["sources"]:
                entry["sources"].append(source)
    return candidates


def _download_discovered_assets(api, assets, max_asset_bytes, asset_workers):
    if not 1 <= asset_workers <= 8:
        raise ValueError("Asset workers must be 1-8")
    results, pending = list(assets), []
    for index, asset in enumerate(assets):
        cache_name = "asset/" + hashlib.sha256(asset["url"].encode()).hexdigest()
        cached = api.store.get(api.run_id, cache_name)
        path = api.store.directory / cached["local_path"] if cached and cached.get("local_path") else None
        if (cached and cached["status"] == "complete" and path and path.is_file()
                and hashlib.sha256(path.read_bytes()).hexdigest() == cached["sha256"]):
            results[index] = dict(cached, sources=asset["sources"])
        else:
            pending.append((index, cache_name, asset))

    def fetch(item):
        index, cache_name, asset = item
        return index, cache_name, download(asset, api.store.directory, max_asset_bytes)

    if asset_workers == 1 or len(pending) < 2:
        fetched = map(fetch, pending)
    else:
        pool = ThreadPoolExecutor(max_workers=min(asset_workers, len(pending)))
        fetched = pool.map(fetch, pending)
    try:
        for index, cache_name, result in fetched:
            results[index] = result
            # SQLite writes remain serialized even when credential-free downloads run in parallel.
            api.store.put(api.run_id, cache_name, result)
    finally:
        if asset_workers > 1 and len(pending) >= 2:
            pool.shutdown()
    return results


def collect_pr(api, repo, number, download_assets=False,
               max_asset_bytes=20 * 1024 * 1024, asset_workers=1):
    api.response_ids = set()
    scope = f"pr:{repo}:{number}"
    root = f"/repos/{repo}/pulls/{number}"
    sections = {"pull_request": one(api, root, scope)}
    pr = sections["pull_request"].get("data", {})
    for name, endpoint, expected, cap in (
        ("labels", f"/repos/{repo}/issues/{number}/labels", None, None),
        ("comments", f"/repos/{repo}/issues/{number}/comments", pr.get("comments"), None),
        ("reviews", root + "/reviews", None, None),
        ("review_comments", root + "/comments", pr.get("review_comments"), None),
        ("commits", root + "/commits", pr.get("commits"), 250),
        ("files", root + "/files", pr.get("changed_files"), 3000),
    ):
        sections[name] = rest_pages(api, endpoint, scope, cap, expected)
    for kind in ("diff", "patch"):
        sections[kind] = one(api, root, scope, f"application/vnd.github.{kind}")
    sections["closing_issues"] = gql_connection(api, repo, number, "closingIssuesReferences",
        "id number url title repository{nameWithOwner}", scope)
    sections["review_threads"] = gql_connection(api, repo, number, "reviewThreads",
        "id isResolved isOutdated path line startLine diffSide startDiffSide resolvedBy{login}", scope)
    comment_fields = "id databaseId url body createdAt updatedAt author{login} path line originalLine diffHunk commit{oid} originalCommit{oid} replyTo{id}"
    for thread in sections["review_threads"]["items"]:
        thread["comments"] = gql_connection(api, repo, number, "comments", comment_fields, scope, thread["id"])
    if any(t["comments"]["status"] != "complete" for t in sections["review_threads"]["items"]):
        sections["review_threads"]["status"] = "partial"

    sources = [("pr:title", pr.get("title")), ("pr:body", pr.get("body"))]
    for kind in ("comments", "reviews", "review_comments"):
        sources.extend((f"{kind}:{item['id']}", item.get("body")) for item in sections[kind]["items"])
    sources.extend(("commit:" + item["sha"], item.get("commit", {}).get("message")) for item in sections["commits"]["items"])
    rejected_references = []
    candidates = references(repo, number, sources, rejected_references)
    for issue in sections["closing_issues"]["items"]:
        key = (repository(issue["repository"]["nameWithOwner"]), issue["number"])
        item = candidates.setdefault(key, {"repo": key[0], "number": key[1], "sources": []})
        item.update(relationship="closes", confidence="github_reported")
        item["sources"].append("closingIssuesReferences")
    linked = []
    for (issue_repo, issue_number), relation in sorted(candidates.items()):
        issue_root = f"/repos/{issue_repo}/issues/{issue_number}"
        detail = one(api, issue_root, scope)
        comments = rest_pages(api, issue_root + "/comments", scope,
                              expected=detail.get("data", {}).get("comments"))
        labels = rest_pages(api, issue_root + "/labels", scope)
        required = (relation.get("relationship") == "closes"
                    and relation.get("confidence") == "github_reported")
        linked.append({**relation,
                       "required_for_source_complete": required,
                       "source_scope": ("problem_source_candidate" if required
                                        else "curator_reference_only"),
                       "detail": detail, "comments": comments, "labels": labels,
                       "kind": "pull_request" if "pull_request" in detail.get("data", {}) else
                       "issue" if detail.get("data") else "unknown"})
        sources.append((f"issue:{issue_repo}#{issue_number}:body", detail.get("data", {}).get("body")))
        sources.extend((f"issue:{issue_repo}#{issue_number}:comment:{c['id']}", c.get("body")) for c in comments["items"])
    unresolved_required = [
        f'{item["repo"]}#{item["number"]}' for item in linked
        if item["required_for_source_complete"] and any(
            item[name]["status"] != "complete" for name in ("detail", "comments", "labels"))
    ]
    unresolved_optional = [
        f'{item["repo"]}#{item["number"]}' for item in linked
        if not item["required_for_source_complete"] and any(
            item[name]["status"] != "complete" for name in ("detail", "comments", "labels"))
    ]
    sections["linked_issues"] = section(
        linked, "partial" if unresolved_required else "complete",
        unresolved_required_sources=unresolved_required,
        unresolved_optional_references=unresolved_optional,
        rejected_references=rejected_references,
        discovery="GitHub closing relationships and textual references; not a claim of all semantic relations")
    for thread in sections["review_threads"]["items"]:
        sources.extend(("thread_comment:" + c["id"], c.get("body")) for c in thread["comments"]["items"])
    for kind in ("diff", "patch"):
        sources.append((kind, sections[kind].get("data")))
    for file in sections["files"]["items"]:
        file["patch_status"] = "present_unverified" if "patch" in file else "missing_binary_or_omitted"
    assets = discover(sources)
    if download_assets:
        assets = _download_discovered_assets(api, assets, max_asset_bytes, asset_workers)
    missing_sources = [k for k in ("pull_request", "comments", "reviews", "review_comments", "commits",
                       "closing_issues", "linked_issues", "review_threads", "files", "diff", "patch")
                       if sections[k]["status"] != "complete"]
    sections["assets"] = section(assets, "complete" if all(a["status"] == "complete" for a in assets)
                                  else "partial" if download_assets else "not_requested",
                                  manifest_status="partial" if missing_sources else "complete",
                                  missing_sources=missing_sources, download_requested=download_assets)
    # Bind the probe to the durable material, not this process's request count.
    # New pages change this key even after interruption before verification;
    # a failed probe cannot fall back to a success for an older material set.
    material_ids = sorted(api.response_ids)
    fingerprint = hashlib.sha256(json.dumps(material_ids).encode()).hexdigest()
    final_pr = one(api, root, scope + ":verify:" + fingerprint)
    comparable = ("updated_at", "head", "base", "comments", "review_comments", "commits", "changed_files")
    sections["consistency"] = {"status": "complete" if final_pr.get("data") and pr and all(
        final_pr["data"].get(k) == pr.get(k) for k in comparable) else "partial",
        "atomic_snapshot": False, "reason": "PR boundary recheck; child resources are observations, not a transactional snapshot",
        "material_response_ids": material_ids, "material_fingerprint": fingerprint,
        "end_pull_request": final_pr}
    complete = all(v["status"] == "complete" for k, v in sections.items() if k != "assets")
    if download_assets and sections["assets"]["status"] != "complete":
        complete = False
    record = {"schema_version": SCHEMA_VERSION, "api_version": API_VERSION,
              "instance_id": repo.replace("/", "__") + "-" + str(number), "repo": repo, "number": number,
              "collected_at": now(), "status": "complete" if complete else "partial", "sections": sections,
              "provenance": {"run_id": api.run_id, "response_ids": sorted(api.response_ids)},
              "derived": {"base_commit": None, "test_patch": None, "hints_text": None,
                          "validation_status": "not_executed", "reason": "Raw evidence preserved; no unvalidated benchmark derivation"}}
    api.store.put(api.run_id, f"pr/{repo}/{number}", record)
    return record
