"""Discover media without rendering it; download via pinned public IPs only."""

import hashlib
import html
import http.client
import ipaddress
import os
import multiprocessing
import re
import shutil
import socket
import ssl
import tempfile
import time
import json
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from .store import now

MEDIA_EXTENSION = re.compile(r"\.(?:png|jpe?g|gif|webp|svg|avif|bmp|mp4|webm|mov)(?:[?#]|$)", re.I)


def discover(sources):
    assets = {}
    for source, body in sources:
        if not isinstance(body, str):
            continue
        explicit = re.findall(r'!\[[^\]]*\]\(\s*<?(https?://[^\s)>]+)', body)
        explicit += re.findall(r'<(?:img|video|source)\b[^>]*?src=["\']([^"\']+)', body, re.I)
        urls = explicit + [u for u in re.findall(r'https?://[^\s<>"\')\]]+', body)
                           if MEDIA_EXTENSION.search(u) or "/user-attachments/assets/" in u or
                           "user-images.githubusercontent.com/" in u]
        for url in urls:
            url = html.unescape(url)
            try:
                parsed = urlsplit(url)
            except ValueError:
                assets.setdefault(url, {"url": url, "sources": [source], "status": "error", "reason": "Malformed asset URL"})
                continue
            if parsed.username or parsed.password:
                # Do not persist URL userinfo, even when present in source text.
                continue
            entry = assets.setdefault(url, {"url": url, "sources": [], "status": "not_requested"})
            if source not in entry["sources"]:
                entry["sources"].append(source)
    return list(assets.values())


def public_target(url, resolver=socket.getaddrinfo):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Only credential-free HTTPS asset URLs are allowed")
    if parsed.port not in (None, 443):
        raise ValueError("Nonstandard asset port is not allowed")
    host = parsed.hostname.encode("idna").decode("ascii")
    addresses = resolver(host, 443, type=socket.SOCK_STREAM)
    ips = [a[4][0] for a in addresses]
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise ValueError("Asset destination is not exclusively public")
    return host, ips[0], (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address):
        super().__init__(host, timeout=20, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        # Resolve/validate once, connect to that exact IP, retain hostname for TLS.
        sock = socket.create_connection((self.address, 443), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def _download(entry, directory, max_bytes=20 * 1024 * 1024, connector=PinnedHTTPS,
             resolver=socket.getaddrinfo):
    result = dict(entry, attempted_at=now())
    destination = Path(directory) / "assets"
    destination.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        url = entry["url"]
        for redirect in range(6):
            host, address, path = public_target(url, resolver)
            connection = connector(host, address)
            try:
                # No token, cookies, proxy credentials, or Referer.
                connection.request("GET", path, headers={"User-Agent": "auditable-pr-crawler/1", "Accept-Encoding": "identity"})
                response = connection.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader("Location")
                    if redirect == 5 or not location:
                        raise ValueError("Invalid or excessive asset redirects")
                    url = urljoin(url, location)
                    continue
                if response.status != 200:
                    result.update(status="unavailable" if response.status in (401, 403, 404, 410) else "error",
                                  reason=f"Asset HTTP {response.status}")
                    return result
                media_type = (response.getheader("Content-Type") or "").split(";")[0].lower()
                if not media_type.startswith(("image/", "video/")):
                    raise ValueError("Response is not image/video media")
                size = response.getheader("Content-Length")
                if size is not None:
                    if not size.isdecimal():
                        raise ValueError("Invalid asset Content-Length")
                    if int(size) > max_bytes:
                        raise ValueError("Asset exceeds size limit")
                digest, total = hashlib.sha256(), 0
                with tempfile.NamedTemporaryFile(dir=destination, delete=False) as stream:
                    temp = Path(stream.name)
                    while chunk := response.read(min(65536, max_bytes + 1 - total)):
                        total += len(chunk)
                        if total > max_bytes:
                            raise ValueError("Asset exceeds size limit")
                        digest.update(chunk)
                        stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
                if size is not None and total != int(size):
                    raise ValueError("Asset Content-Length mismatch (truncated response)")
                sha = digest.hexdigest()
                target = destination / sha
                os.replace(temp, target)
                temp = None
                result.update(status="complete", sha256=sha, bytes=total, media_type=media_type,
                              local_path=str(Path("assets") / sha), fetched_at=now())
                return result
            finally:
                connection.close()
        raise ValueError("Asset redirect limit")
    except (ValueError, OSError, http.client.HTTPException) as exc:
        # Do not emit raw network exception messages which may include URL secrets.
        result.update(status="error", reason=str(exc) if isinstance(exc, ValueError) else type(exc).__name__)
        return result
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def _worker(pipe, entry, directory, max_bytes):
    try:
        pipe.send(_download(entry, directory, max_bytes))
    finally:
        pipe.close()


def bounded_download(entry, directory, max_bytes, timeout=60, worker=_worker):
    """A disposable process bounds DNS, TLS, redirects AND slow-drip reads."""
    destination = Path(directory)
    destination.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="asset-staging-", dir=destination))
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=worker, args=(sender, entry, str(staging), max_bytes), daemon=True)
    try:
        process.start()
        sender.close()
        if not receiver.poll(timeout):
            return dict(entry, status="error", reason="Asset wall-clock deadline exceeded", attempted_at=now())
        try:
            result = receiver.recv()
        except EOFError:
            return dict(entry, status="error", reason="Asset download worker failed", attempted_at=now())
        if result["status"] == "complete":
            (destination / "assets").mkdir(exist_ok=True)
            os.replace(staging / result["local_path"], destination / result["local_path"])
        return result
    finally:
        receiver.close()
        sender.close()
        if process.pid is not None:
            process.join(timeout=0.1)
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
            if process.is_alive():
                process.kill()
                process.join(timeout=2)
        # Only the exact per-download staging directory created above is removed.
        shutil.rmtree(staging)


def retryable(result):
    if result.get("status") != "error":
        return False
    reason = result.get("reason", "")
    return (reason in {"ConnectionResetError", "ConnectionAbortedError", "TimeoutError",
                       "RemoteDisconnected", "IncompleteRead", "gaierror", "SSLError",
                       "Asset wall-clock deadline exceeded", "Asset download worker failed",
                       "Asset Content-Length mismatch (truncated response)"}
            or re.fullmatch(r"Asset HTTP 5\d\d", reason) is not None)


def apply_recovery(archive_path, asset):
    """Overlay a verified append-only Stage-11 recovery without mutating its record."""
    archive_path = Path(archive_path)
    recovery_path = archive_path.parent / "11_01_asset_recovery_manifest.json"
    if not recovery_path.exists():
        return asset
    recovery = json.loads(recovery_path.read_text())
    manifest_path = archive_path.parent / "11_manifest.json"
    if (recovery.get("schema_version") != "stage11-asset-recovery-v1"
            or recovery.get("source_manifest_sha256") != hashlib.sha256(manifest_path.read_bytes()).hexdigest()):
        raise ValueError("Stage-11 asset recovery provenance mismatch")
    matches = [entry for entry in recovery.get("entries", [])
               if entry.get("record") == archive_path.name and entry.get("url") == asset.get("url")]
    if len(matches) > 1:
        raise ValueError("Duplicate Stage-11 asset recovery entry")
    if not matches or matches[0]["recovery"].get("status") != "complete":
        return asset
    recovered = matches[0]["recovery"]
    path = archive_path.parent / "11_http_archive" / recovered["local_path"]
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != recovered["sha256"]:
        raise ValueError("Recovered Stage-11 asset hash mismatch")
    return dict(asset, status="complete", sha256=recovered["sha256"], bytes=recovered["bytes"],
                media_type=recovered["media_type"], local_path=recovered["local_path"],
                fetched_at=recovered["fetched_at"], recovered_from_status=asset.get("status"),
                recovered_from_reason=asset.get("reason"), recovery_manifest=str(recovery_path))


def download(entry, directory, max_bytes=20 * 1024 * 1024, connector=PinnedHTTPS,
             resolver=socket.getaddrinfo, attempts=3, sleep=time.sleep):
    if attempts < 1:
        raise ValueError("Asset download attempts must be positive")
    history, result = [], None
    for attempt in range(1, attempts + 1):
        if connector is not PinnedHTTPS or resolver is not socket.getaddrinfo:
            # Injection seam for deterministic protocol tests, never used by CLI.
            result = _download(entry, directory, max_bytes, connector, resolver)
        else:
            result = bounded_download(entry, directory, max_bytes)
        history.append({"attempt": attempt, "status": result.get("status"),
                        "reason": result.get("reason"),
                        "attempted_at": result.get("attempted_at")})
        if not retryable(result) or attempt == attempts:
            break
        sleep(0.5 * 2 ** (attempt - 1))
    return dict(result, attempt_count=len(history), download_attempts=history)
