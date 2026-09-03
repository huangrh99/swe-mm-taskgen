"""PR-body image evidence, not a classifier of visual necessity.

The optional screening command uses markdown-it-py; the core crawler remains
standard-library-only. No URLs are fetched by this module.
"""

import hashlib
import html
from html.parser import HTMLParser
import re
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt

RULE_VERSION = "pr-body-images-v1"
MARKDOWN = MarkdownIt("commonmark", {"html": True})
IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "svg", "avif", "bmp", "tiff", "tif", "ico", "heic", "heif", "apng"}
VIDEO_EXTENSIONS = {"mp4", "webm", "mov", "m4v", "avi", "mkv", "wmv", "flv", "mpeg"}
BARE_URL = re.compile(r'https?://[^\s<>"\]\x60]+')


def url_kind(url):
    try:
        parsed = urlsplit(url)
        if parsed.scheme == "data":
            return "image" if url.lower().startswith("data:image/") else None
        if parsed.scheme and parsed.scheme not in ("http", "https"):
            return None
        extension = unquote(parsed.path).rsplit(".", 1)[-1].lower()
        if extension in IMAGE_EXTENSIONS:
            return "image"
        if extension in VIDEO_EXTENSIONS:
            return "video"
        if (parsed.hostname == "github.com" and "/user-attachments/assets/" in parsed.path or
                parsed.hostname in {"user-images.githubusercontent.com", "private-user-images.githubusercontent.com"}):
            return "untyped_attachment"
    except ValueError:
        pass
    return None


def decoration_reason(url):
    try:
        parsed = urlsplit(url)
        host, path = (parsed.hostname or "").lower(), parsed.path.lower()
    except ValueError:
        return None
    if host == "dependabot-badges.githubapp.com" or host in {"img.shields.io", "shields.io"}:
        return "known_badge_host"
    if host == "developer.mend.io" and "/badges/" in path:
        return "renovate_merge_confidence_badge"
    if host == "github.com" and "/workflows/" in path and path.endswith("/badge.svg"):
        return "github_workflow_badge"
    if host == "twemoji.maxcdn.com":
        return "twemoji_icon"
    if host == "pixel.wp.com":
        return "tracking_pixel"
    return None


class MediaHTML(HTMLParser):
    def __init__(self, emit, text):
        super().__init__(convert_charrefs=True)
        self.emit, self.text = emit, text
        self.stack = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        blocked = any(t in {"script", "style", "pre", "code"} for t in self.stack)
        if tag not in {"img", "source", "br", "hr", "input", "meta", "link", "wbr"}:
            self.stack.append(tag)
        if blocked or tag in {"script", "style", "pre", "code"}:
            return
        if tag == "img" and attrs.get("src"):
            self.emit(attrs["src"], "image", "html_img", attrs.get("alt", ""))
        elif tag == "video":
            if attrs.get("src"):
                self.emit(attrs["src"], "video", "html_video", "")
            if attrs.get("poster"):
                self.emit(attrs["poster"], "image", "html_video_poster", "")
        elif tag == "source" and attrs.get("src"):
            kind = "image" if attrs.get("type", "").startswith("image/") or "picture" in self.stack else "video"
            self.emit(attrs["src"], kind, "html_source", "")
        elif tag == "a" and attrs.get("href") and url_kind(attrs["href"]):
            self.emit(attrs["href"], None, "html_media_link", "")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in self.stack:
            del self.stack[len(self.stack) - 1 - self.stack[::-1].index(tag):]

    def handle_data(self, data):
        if not any(t in {"script", "style", "pre", "code"} for t in self.stack):
            self.text.append(data)


def discover_body(body):
    """Parse rendered-image syntax; ignore code and HTML template comments.

    URL presence is evidence, not a successful download. Relative images remain
    unresolved instead of inventing a repository path or historical commit.
    """
    assets = {}

    def emit(url, declared=None, syntax="bare_media_url", alt=""):
        url = html.unescape(url.strip())
        if not url:
            return
        try:
            parsed = urlsplit(url)
            if parsed.username or parsed.password or parsed.scheme not in ("", "https", "http", "data"):
                return
        except ValueError:
            return
        hinted = url_kind(url)
        if not declared and not hinted:
            return
        key = hashlib.sha256(url.encode()).hexdigest()
        entry = assets.setdefault(key, {"asset_id": key, "url": url, "source": "pr.body",
            "declarations": [], "syntax": [], "alt_texts": [], "extension_kind": hinted,
            "decoration_reason": decoration_reason(url), "availability": "not_checked",
            "url_resolution": "absolute" if parsed.scheme in {"http", "https"} else
                              "inline_data" if parsed.scheme == "data" else "relative_unresolved"})
        if declared and declared not in entry["declarations"]:
            entry["declarations"].append(declared)
        if syntax not in entry["syntax"]:
            entry["syntax"].append(syntax)
        if alt and alt not in entry["alt_texts"]:
            entry["alt_texts"].append(alt)

    def walk(tokens, parse_html=True):
        inline_chunks = []
        inline_html = MediaHTML(emit, inline_chunks)
        for token in tokens:
            if token.type == "html_inline" and parse_html:
                inline_html.feed(token.content)
                continue
            if any(t in {"script", "style", "pre", "code"} for t in inline_html.stack):
                continue
            if token.type == "image":
                emit(token.attrGet("src") or "", "image", "markdown_image", token.content)
            elif token.type == "link_open":
                emit(token.attrGet("href") or "", syntax="markdown_media_link")
            elif token.type == "text":
                for match in BARE_URL.finditer(token.content):
                    emit(match.group().rstrip(".,;:!?)}"))
            elif token.type == "html_block" and parse_html:
                chunks = []
                parser = MediaHTML(emit, chunks)
                parser.feed(token.content)
                parser.close()
                # Markdown can occur inside <details> blocks. Comments never
                # reach handle_data, and fenced/inline code is still ignored.
                if chunks:
                    walk(MARKDOWN.parse("".join(chunks)), parse_html=False)
            if token.children and token.type != "image":
                walk(token.children, parse_html)

    walk(MARKDOWN.parse(body or ""))
    for entry in assets.values():
        kinds = set(entry["declarations"])
        if entry["extension_kind"] in {"image", "video"}:
            kinds.add(entry["extension_kind"])
        entry["media_kind"] = next(iter(kinds)) if len(kinds) == 1 else "conflicting" if kinds else "untyped_attachment"
    return list(assets.values())


def classify(assets):
    images = [a for a in assets if a["media_kind"] == "image"]
    if any(not a["decoration_reason"] for a in images):
        return "non_badge_image_evidence"
    if images:
        return "only_badge_or_decoration_image_evidence"
    if any(a["media_kind"] in {"untyped_attachment", "conflicting"} for a in assets):
        return "untyped_attachment_without_image_evidence"
    if any(a["media_kind"] == "video" for a in assets):
        return "video_without_image_evidence"
    return "no_detected_media_in_pr_body"
