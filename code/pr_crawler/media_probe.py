"""Anonymous, bounded media-type probes; not full media downloads or decoding."""

import base64
import hashlib
import http.client
from urllib.parse import urljoin, urlsplit, urlunsplit

from .assets import PinnedHTTPS, bounded_download, public_target
from .store import now


def signature_kind(prefix):
    if (prefix.startswith((b'\x89PNG\r\n\x1a\n', b'\xff\xd8\xff', b'GIF87a', b'GIF89a', b'BM', b'II*\x00', b'MM\x00*')) or
            prefix.startswith(b'RIFF') and prefix[8:12] == b'WEBP' or b'<svg' in prefix.lstrip()[:512]):
        return "image"
    if prefix[4:8] == b'ftyp':
        return "image" if prefix[8:12] in {b'avif', b'avis', b'heic', b'heix', b'mif1'} else "video"
    if prefix.startswith(b'\x1a\x45\xdf\xa3'):
        return "video"
    return None


def probe(entry, connector=PinnedHTTPS, target=public_target):
    result = {"asset_id": entry["asset_id"], "url": entry["url"], "attempted_at": now(),
              "probe_kind": "anonymous_get_prefix_512_bytes", "full_download": False, "decoded": False}
    url = entry["url"]
    try:
        for hop in range(6):
            host, address, path = target(url)
            connection = connector(host, address)
            try:
                connection.request("GET", path, headers={"User-Agent": "auditable-pr-crawler/1", "Range": "bytes=0-511", "Accept-Encoding": "identity"})
                response = connection.getresponse()
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.getheader("Location")
                    if not location or hop == 5:
                        raise ValueError("Invalid or excessive media redirects")
                    url = urljoin(url, location)
                    continue
                result["http_status"] = response.status
                if response.status not in {200, 206}:
                    return {**result, "status": "unavailable", "media_kind": None, "reason": f"HTTP {response.status}"}
                prefix = response.read(512)
                media_type = (response.getheader("Content-Type") or "").split(";")[0].strip().lower()
                sniffed = signature_kind(prefix)
                declared = "image" if media_type.startswith("image/") else "video" if media_type.startswith("video/") else None
                kind = sniffed or declared
                if not prefix or declared and sniffed and declared != sniffed or media_type == 'text/html':
                    kind = None
                final = urlsplit(url)
                result.update(status="typed" if kind else "unresolved", media_kind=kind, media_type=media_type,
                    signature_kind=sniffed, prefix_bytes=len(prefix), prefix_sha256=hashlib.sha256(prefix).hexdigest(),
                    prefix_base64=base64.b64encode(prefix).decode(),
                    content_length=response.getheader("Content-Length"), content_range=response.getheader("Content-Range"),
                    final_url_without_query=urlunsplit((final.scheme, final.netloc, final.path, "", "")), fetched_at=now())
                return result
            finally:
                connection.close()
    except (ValueError, OSError, http.client.HTTPException) as exc:
        return {**result, "status": "error", "media_kind": None, "reason": type(exc).__name__}
    return {**result, "status": "error", "media_kind": None, "reason": "Redirect limit"}


def _probe_worker(pipe, entry, directory, max_bytes):
    try:
        pipe.send(probe(entry))
    finally:
        pipe.close()


def bounded_probe(entry, temporary):
    # Reuse the crawler's disposable-process deadline and exact staging cleanup.
    # "typed" is deliberately not "complete": there is no full asset to move.
    return bounded_download(entry, temporary, 512, timeout=25, worker=_probe_worker)
