"""Bounded read-only API transport; every response is checkpointed before use."""

import json
import http.client
import os
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

from .store import dumps

API_VERSION = "2026-03-10"
MAX_RESPONSE = 32 * 1024 * 1024


class APIError(Exception):
    def __init__(self, reason, status=0, retry_at=None):
        super().__init__(reason)
        self.status = status
        self.retry_at = retry_at

    def info(self):
        return {"status": "unavailable" if self.status in (401, 403, 404, 410) and not self.retry_at
                else "error", "reason": str(self), "http_status": self.status,
                "retry_at": self.retry_at}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def credential():
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    try:
        result = subprocess.run(["gh", "auth", "token", "--hostname", "github.com"],
                                capture_output=True, text=True, timeout=10)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def transport(method, endpoint, payload, accept, token):
    # Never follow a server-provided redirect with Authorization. A renamed repo
    # can be supplied under its current name; the 3xx response remains evidence.
    parsed = urlsplit(endpoint)
    if parsed.scheme or parsed.netloc or not endpoint.startswith("/") or endpoint.startswith("//"):
        raise APIError("Invalid API endpoint")
    headers = {"User-Agent": "auditable-pr-crawler/1", "Accept": accept,
               "X-GitHub-Api-Version": API_VERSION}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = dumps(payload).encode() if payload is not None else None
    if data:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request("https://api.github.com" + endpoint,
                                     data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(NoRedirect)
    try:
        response = opener.open(request, timeout=45)
    except urllib.error.HTTPError as exc:
        response = exc
    except (OSError, urllib.error.URLError) as exc:
        # Exception text may contain credentials/proxy URLs; do not persist it.
        raise APIError("Network failure: " + type(exc).__name__) from None
    try:
        with response:
            body = response.read(MAX_RESPONSE + 1)
            if len(body) > MAX_RESPONSE:
                raise APIError("API response exceeds 32 MiB limit")
            safe_headers = {k.lower(): v for k, v in response.headers.items()
                            if k.lower() in {"link", "etag", "date", "content-type", "retry-after",
                                             "x-ratelimit-remaining", "x-ratelimit-reset",
                                             "x-github-request-id", "x-github-api-version-selected"}}
            return response.status, safe_headers, body
    except (OSError, http.client.HTTPException) as exc:
        # A connection can fail after headers arrive, while reading the body.
        # Let the same bounded retry/checkpoint path handle this safely.
        raise APIError("Network failure: " + type(exc).__name__) from None


def retry_delay(headers, attempt):
    if "retry-after" in headers:
        try:
            return max(0, float(headers["retry-after"]))
        except ValueError:
            try:
                return max(0, parsedate_to_datetime(headers["retry-after"]).timestamp() - time.time())
            except (ValueError, TypeError):
                return 60
    if headers.get("x-ratelimit-remaining") == "0":
        try:
            return max(1, float(headers["x-ratelimit-reset"]) - time.time() + 1)
        except (ValueError, KeyError):
            return 60
    return 2 ** attempt


class API:
    def __init__(self, store, run_id, token=None, send=transport, sleep=time.sleep):
        self.store, self.run_id, self.token = store, run_id, token
        self.send, self.sleep = send, sleep
        self.response_ids = set()
        self.cooldown = store.cooldown()

    def request(self, endpoint, payload=None, accept="application/vnd.github+json", scope=""):
        method = "POST" if payload is not None else "GET"
        if method == "POST" and (endpoint != "/graphql" or
                not payload.get("query", "").lstrip().startswith("query")):
            raise APIError("Only read-only GraphQL queries are supported")
        key = dumps([scope, method, endpoint, payload, accept])
        cached = self.store.cached(self.run_id, key)
        if cached:
            self.response_ids.add(cached["id"])
            return self.decode(cached, accept)
        self.cooldown = max(self.cooldown or 0, self.store.cooldown())
        if self.cooldown and self.cooldown > time.time():
            raise APIError("API rate-limit cooldown; resume after retry_at", 429,
                           datetime.fromtimestamp(self.cooldown, timezone.utc).isoformat())
        for attempt in range(3):
            try:
                status, headers, body = self.send(method, endpoint, payload, accept, self.token)
            except APIError:
                if attempt == 2:
                    raise
                self.sleep(2 ** attempt)
                continue
            parsed = None
            malformed = False
            if "json" in accept:
                try:
                    parsed = json.loads(body)
                except (ValueError, UnicodeDecodeError):
                    malformed = True
            graphql_errors = isinstance(parsed, dict) and bool(parsed.get("errors"))
            success = 200 <= status < 300 and not malformed and not graphql_errors
            row = self.store.response(self.run_id, key, method, endpoint, payload, status,
                                      headers, body, success)
            self.response_ids.add(row["id"])
            if success:
                return self.decode(row, accept)
            gql_limited = graphql_errors and any(e.get("type") == "RATE_LIMITED" or
                "rate limit" in str(e.get("message", "")).lower() for e in parsed["errors"])
            limited = gql_limited or status == 429 or (status >= 400 and "retry-after" in headers) or (status == 403 and (
                "retry-after" in headers or headers.get("x-ratelimit-remaining") == "0" or
                b"rate limit" in body.lower()))
            delay = retry_delay(headers, attempt)
            if limited and delay < 60 and "retry-after" not in headers and headers.get("x-ratelimit-remaining") != "0":
                delay = 60  # Secondary limits: do not hammer the API.
            retry_at = None
            if limited:
                self.cooldown = time.time() + max(delay, 1)
                self.store.cooldown(self.cooldown)
                retry_at = datetime.fromtimestamp(self.cooldown, timezone.utc).isoformat()
            if (limited and delay <= 5 or status >= 500 and not limited) and attempt < 2:
                self.sleep(min(delay, 5))
                continue
            reason = "GraphQL partial/errors response" if graphql_errors else (
                "Malformed JSON response" if malformed else f"HTTP {status}")
            raise APIError(reason, status, retry_at)

    @staticmethod
    def decode(row, accept):
        return {"data": json.loads(row["body"]) if "json" in accept else row["body"].decode("utf-8", errors="replace"),
                "headers": json.loads(row["headers"]), "response_id": row["id"],
                "sha256": row["sha256"], "fetched_at": row["fetched_at"]}
