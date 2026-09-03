#!/usr/bin/env python3
"""Build p5.js from the tested tree and evaluate the frozen Canvas oracle."""

from __future__ import annotations

import html
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


TESTS = (
    ("f2p_clip_union_has_no_missing_interior_pixels", "F2P"),
    ("p2p_clip_mask_has_no_exterior_expansion", "P2P"),
    ("p2p_normal_clip_selects_interior", "P2P"),
    ("p2p_inverted_clip_selects_complement", "P2P"),
    ("p2p_ordinary_shape_rendering", "P2P"),
)


def _run(command: list[str], *, timeout: int, cwd: Path | None = None,
         env: dict[str, str] | None = None, untrusted: bool = False) -> subprocess.CompletedProcess[str]:
    untrusted_uid = int(os.environ.get("HARBOR_UNTRUSTED_UID", "10002"))

    def drop_untrusted_privileges() -> None:
        os.setgroups([])
        os.setgid(untrusted_uid)
        os.setuid(untrusted_uid)

    return subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True,
                          timeout=timeout, check=False,
                          preexec_fn=drop_untrusted_privileges if untrusted else None)


def _parse_observed(document: str) -> dict:
    marker = '<pre id="result">'
    if marker not in document:
        raise ValueError("Chromium output did not contain the result marker")
    payload = document.split(marker, 1)[1].split("</pre>", 1)[0]
    if not payload.strip():
        raise ValueError("Chromium fixture produced an empty result")
    observed = json.loads(html.unescape(payload))
    required = {
        "missing_interior_pixels", "unexpected_exterior_pixels", "normal_clip",
        "inverted_clip", "ordinary_shape",
    }
    if set(observed) != required:
        raise ValueError("fixture result has an unexpected field inventory")
    if (type(observed["missing_interior_pixels"]) is not int
            or type(observed["unexpected_exterior_pixels"]) is not int):
        raise ValueError("pixel counts must be integers")
    if any(not isinstance(observed[name], bool)
           for name in ("normal_clip", "inverted_clip", "ordinary_shape")):
        raise ValueError("P2P observations must be booleans")
    return observed


def _classify(observed: dict) -> list[dict]:
    predicates = {
        "f2p_clip_union_has_no_missing_interior_pixels": observed["missing_interior_pixels"] == 0,
        "p2p_clip_mask_has_no_exterior_expansion": observed["unexpected_exterior_pixels"] == 0,
        "p2p_normal_clip_selects_interior": observed["normal_clip"],
        "p2p_inverted_clip_selects_complement": observed["inverted_clip"],
        "p2p_ordinary_shape_rendering": observed["ordinary_shape"],
    }
    return [{"test_id": test_id, "status": "pass" if predicates[test_id] else "fail"}
            for test_id, _ in TESTS]


def _copy_tested_tree(source: Path, destination: Path) -> None:
    shutil.copytree(source, destination, symlinks=True,
                    ignore=shutil.ignore_patterns(".git", "node_modules"))
    dependencies = source / "node_modules"
    if not dependencies.is_dir() or dependencies.is_symlink():
        raise ValueError("tested tree lacks a regular node_modules directory")
    (destination / "node_modules").symlink_to(dependencies, target_is_directory=True)
    docs = destination / "docs"
    docs.mkdir(exist_ok=True)
    parameter_data = docs / "parameterData.json"
    if not parameter_data.exists():
        parameter_data.write_text("{}\n")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _chown_tree(root: Path, uid: int) -> None:
    for path in (root, *root.rglob("*")):
        os.chown(path, uid, uid, follow_symlinks=False)


def main() -> int:
    if os.geteuid() != 0:
        raise RuntimeError("functional runner must retain oracle privileges")
    app_root = Path(os.environ.get("HARBOR_APP_ROOT", "/testbed")).resolve()
    tests_root = Path(os.environ.get("HARBOR_TEST_ROOT", "/tests")).resolve()
    chromium = os.environ.get("CHROMIUM_BIN", "/usr/bin/chromium")
    node = os.environ.get("NODE_BIN", "/usr/bin/node")
    with tempfile.TemporaryDirectory(prefix="p5js-7583-") as temporary:
        root = Path(temporary)
        workspace = root / "untrusted-workspace"
        source = workspace / "source"
        bundle = workspace / "p5.js"
        build_script = root / "build_bundle.cjs"
        browser_script = root / "run_fixture.cjs"
        workspace.mkdir()
        _copy_tested_tree(app_root, source)
        build_script.write_bytes((tests_root / "payload/build_bundle.cjs").read_bytes())
        browser_script.write_bytes((tests_root / "payload/run_fixture.cjs").read_bytes())
        os.chmod(root, 0o711)
        os.chmod(build_script, 0o555)
        os.chmod(browser_script, 0o555)
        trusted_hashes = (_sha256(build_script), _sha256(browser_script))
        fixture = (tests_root / "payload/fixture.html").read_bytes()
        untrusted_uid = int(os.environ.get("HARBOR_UNTRUSTED_UID", "10002"))
        _chown_tree(workspace, untrusted_uid)
        build = _run([
            node, str(build_script), str(source),
            str(app_root / "node_modules"), str(bundle),
        ], timeout=180, untrusted=True)
        if ((_sha256(build_script), _sha256(browser_script)) != trusted_hashes
                or build.returncode or bundle.is_symlink() or not bundle.is_file()
                or bundle.stat().st_uid != untrusted_uid):
            raise RuntimeError(f"frozen browserify build failed: {build.stderr[-4000:]}")

        bundle_bytes = bundle.read_bytes()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
                body = bundle_bytes if self.path == "/p5.js" else fixture
                content_type = "text/javascript" if self.path == "/p5.js" else "text/html"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            browser = _run([
                node, str(browser_script),
                str(app_root / "node_modules"), chromium,
                f"http://127.0.0.1:{server.server_port}/fixture.html",
            ], timeout=60, untrusted=True)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        if ((_sha256(build_script), _sha256(browser_script)) != trusted_hashes
                or browser.returncode):
            raise RuntimeError(f"Chromium failed: {browser.stderr[-4000:]}")
        observed = _parse_observed(browser.stdout)
        print(json.dumps({
            "schema_version": "p5js-7583-functional-v1",
            "observed": observed,
            "results": _classify(observed),
        }, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
