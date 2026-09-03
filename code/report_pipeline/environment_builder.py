"""Build content-addressed, dependency-complete base images for archived cases."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from report_pipeline.atomic import write_json
from report_pipeline.paths import CASES_ROOT, TMP_ROOT, WORKSPACE_ROOT


NODE_IMAGE = "node@sha256:8a34c4ab3ea2c5cd194f07e317b2a8f09461d3c8b05c4e34c8ccd56d56024c4d"
BROWSER_NODE20_IMAGE = "visual-harbor-base:aacaed53a4cd54d4c052c3d808808d0f15252a655076c584305804586df67ef9"
CASES = {
    "bpmn-io__bpmn-js-2396": {
        "repo": "bpmn-io/bpmn-js", "commit": "686561a9b9c733dc3a466a26e3803c5832b3c956",
        "local": "tmp/multimodal-2025/14_pr_tests/bpmn-js", "install": "npm ci --ignore-scripts",
        "base": BROWSER_NODE20_IMAGE, "preserve_asset_names": True,
    },
    "automattic__wp-calypso-100957": {
        "repo": "automattic/wp-calypso", "commit": "19ed337b2b8b48d9d768b35add8a65221e512c9c",
        "local": "tmp/archive_google_wp/wp-calypso", "install": "yarn install --immutable",
        "base": BROWSER_NODE20_IMAGE, "node_overlay": NODE_IMAGE,
    },
    "automattic__wp-calypso-99049": {
        "repo": "automattic/wp-calypso", "commit": "047aeef4a31c9e93ccb975adc77fb2107067fd6e",
        "local": "tmp/archive_google_wp/wp-calypso", "install": "yarn install --immutable",
        "base": BROWSER_NODE20_IMAGE, "node_overlay": NODE_IMAGE,
    },
    "carbon-design-system__carbon-22019": {
        "repo": "carbon-design-system/carbon", "commit": "05166d4aff1dfbcef426642af124b8af4393421c",
        "local": "tmp/environment-build/repos/carbon", "install": "yarn install --immutable --mode=skip-build",
        "base": BROWSER_NODE20_IMAGE,
    },
    "excalidraw__excalidraw-9002": {
        "repo": "excalidraw/excalidraw", "commit": "c92f3bebf5fc4e9a1512be368f05d800ae1b92f7",
        "local": "tmp/archive_mermaid_excalidraw/repos/excalidraw",
        "install": "corepack prepare yarn@1.22.22 --activate && yarn install --frozen-lockfile --ignore-scripts",
        "base": BROWSER_NODE20_IMAGE,
    },
    "excalidraw__excalidraw-9010": {
        "repo": "excalidraw/excalidraw", "commit": "00b5b0a0ca556a527feb3f768fbec5842df86549",
        "local": "tmp/multimodal-2025/20_17_v4_f2p_p2p_luna/repos/excalidraw",
        "install": "corepack prepare yarn@1.22.22 --activate && yarn install --frozen-lockfile --ignore-scripts",
        "base": BROWSER_NODE20_IMAGE,
    },
    "googlechrome__lighthouse-16403": {
        "repo": "googlechrome/lighthouse", "commit": "9ab6a2f970094a9ae45280d47215e3cbce5e1937",
        "local": "tmp/archive_google_wp/lighthouse", "install": "yarn install --immutable",
        "base": BROWSER_NODE20_IMAGE,
    },
    "mermaid-js__mermaid-7711": {
        "repo": "mermaid-js/mermaid", "commit": "98b3155bd1d2ee8e29f8f9cfcad1bd1a4b0a5c8e",
        "local": "tmp/archive_mermaid_excalidraw/repos/mermaid",
        "install": "corepack prepare pnpm@10.30.3 --activate && pnpm install --frozen-lockfile --ignore-scripts",
        "base": BROWSER_NODE20_IMAGE,
    },
}


def _run(command: list[str], *, cwd: Path | None = None, log: Path | None = None) -> str:
    process = subprocess.run(command, cwd=cwd, text=True, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT)
    if log:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(process.stdout)
    if process.returncode:
        raise RuntimeError(f"command_failed:{command[0]}:{process.returncode}")
    return process.stdout.strip()


def _copy_assets(case: Path, environment: Path, *, preserve_names: bool = False) -> list[dict]:
    source = case / "meta/01_visual_review"
    candidates = sorted((source / "assets").glob("*")) if (source / "assets").is_dir() else []
    candidates += sorted(source.glob("*_solver_visible.*"))
    target = environment / "assets"
    target.mkdir(parents=True, exist_ok=True)
    records = []
    for index, path in enumerate(candidates, 1):
        if not path.is_file():
            continue
        name = path.name if preserve_names else f"asset_{index:02d}{path.suffix.lower()}"
        shutil.copyfile(path, target / name)
        records.append({"name": name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    return records


def build_one(instance_id: str) -> dict:
    spec = CASES[instance_id]
    case = CASES_ROOT / instance_id
    (case / "meta/environment/build_failure.json").unlink(missing_ok=True)
    working = TMP_ROOT / "environment-build" / "contexts" / instance_id
    log = case / "meta/environment/build.log"
    if working.exists():
        shutil.rmtree(working)
    working.parent.mkdir(parents=True, exist_ok=True)
    repository = WORKSPACE_ROOT / spec["local"]
    app = working / "app"
    # A local clone of a blobless partial clone loses its promisor relationship:
    # its index names files whose blobs cannot be materialized.  Fetch the one
    # immutable baseline directly in that case; ordinary complete archives keep
    # the faster local hardlink path.
    promisor = subprocess.run(
        ["git", "config", "--get", "remote.origin.promisor"], cwd=repository,
        text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    ).stdout.strip()
    if promisor == "true":
        remote = _run(["git", "remote", "get-url", "origin"], cwd=repository)
        app.mkdir(parents=True)
        _run(["git", "init"], cwd=app)
        _run(["git", "remote", "add", "origin", remote], cwd=app)
        _run(["git", "fetch", "--depth=1", "--filter=blob:none", "origin", spec["commit"]], cwd=app)
        _run(["git", "checkout", "--detach", "FETCH_HEAD"], cwd=app)
    else:
        # Local clone hardlinks the object database; Docker still receives a
        # standalone .git directory without duplicating large packs.
        _run(["git", "clone", "--local", "--no-checkout", str(repository), str(app)])
        _run(["git", "checkout", "--detach", spec["commit"]], cwd=app)
    # --no-checkout clones may already point HEAD at the requested commit while
    # their worktree is empty. Explicitly materialize the archived baseline.
    _run(["git", "reset", "--hard", spec["commit"]], cwd=working / "app")
    _run(["git", "remote", "remove", "origin"], cwd=working / "app")
    dockerfile = working / "Dockerfile"
    base = spec.get("base", NODE_IMAGE)
    node_overlay = spec.get("node_overlay")
    node_stage = f"FROM {node_overlay} AS node_runtime\n" if node_overlay else ""
    node_copy = "COPY --from=node_runtime /usr/local/ /usr/local/\n" if node_overlay else ""
    browser_layer = ("RUN rm -rf /app\n" if base == BROWSER_NODE20_IMAGE else
        "RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "
        "chromium xvfb git python3 make g++ ca-certificates fonts-liberation && rm -rf /var/lib/apt/lists/*\n")
    dockerfile.write_text(
        node_stage + f"FROM {base}\n" + node_copy +
        "ENV CI=1 COREPACK_ENABLE_DOWNLOAD_PROMPT=0 CHROME_PATH=/usr/bin/chromium\n"
        + browser_layer +
        "RUN corepack enable\n"
        "COPY app /app\nWORKDIR /app\n"
        f"RUN {spec['install']}\n"
        "RUN git reset --hard HEAD && test -z \"$(git status --porcelain --untracked-files=all)\" "
        "&& test -z \"$(git remote)\"\n"
    )
    tag = "visual-env-build:" + instance_id.lower().replace("__", "-").replace("_", "-")
    _run(["docker", "build", "--progress=plain", "--tag", tag, str(working)], log=log)
    inspect = json.loads(_run(["docker", "image", "inspect", tag]))[0]
    image_id = inspect["Id"]
    frozen_ref = "visual-harbor-base:" + image_id.removeprefix("sha256:")
    _run(["docker", "tag", image_id, frozen_ref])
    head = _run(["docker", "run", "--rm", "--network", "none", "--entrypoint", "git",
                 image_id, "-C", "/app", "rev-parse", "HEAD"])
    if head != spec["commit"]:
        raise RuntimeError("base_commit_mismatch")
    environment = case / "environment"
    if environment.exists():
        shutil.rmtree(environment)
    environment.mkdir()
    assets = _copy_assets(case, environment, preserve_names=spec.get("preserve_asset_names", False))
    (environment / "Dockerfile").write_text(
        f"FROM {frozen_ref}\n"
        "RUN rm -rf /app/.git && ln -s /app /testbed\n"
        "WORKDIR /app\nCOPY assets /app/assets\n"
        "RUN git init /app "
        "&& git -C /app config user.name 'Benchmark Baseline' "
        "&& git -C /app config user.email 'benchmark@invalid.local' "
        "&& git -C /app config gc.auto 0 "
        "&& git -C /app add -A "
        "&& GIT_AUTHOR_DATE='2000-01-01T00:00:00Z' GIT_COMMITTER_DATE='2000-01-01T00:00:00Z' "
        "git -C /app commit -q -m 'benchmark baseline' "
        "&& git -C /app reflog expire --expire=now --all "
        "&& rm -f /app/.git/gc.pid && git -C /app gc --prune=now --force\n"
        "WORKDIR /testbed\n"
    )
    (environment / "docker-compose.yaml").write_text(
        "services:\n  main:\n    cap_drop: [ALL]\n    cap_add: [FOWNER]\n"
        "    security_opt: [no-new-privileges:true]\n    pids_limit: 1024\n    shm_size: 1gb\n"
    )
    binding = {
        "schema_version": "harbor-base-image-binding-v1", "status": "local_content_addressed_image",
        "instance_id": instance_id, "repository": spec["repo"], "source_baseline_sha": spec["commit"],
        "image_id": image_id, "build_reference": frozen_ref, "architecture": inspect["Architecture"],
        "node_base": node_overlay or base, "browser_base": base if node_overlay else None,
        "node_overlay": node_overlay, "asset_inventory": assets,
    }
    write_json(environment / "base_image.json", binding)
    audit = {**binding, "schema_version": "case-environment-build-v1",
             "built_at": datetime.now(timezone.utc).isoformat(),
             "build_log": "meta/environment/build.log", "status": "complete"}
    write_json(case / "meta/environment/build.json", audit)
    (case / "meta/environment/build_failure.json").unlink(missing_ok=True)
    return audit


def build_all(instance_ids: list[str], workers: int = 2) -> dict:
    results, failures = {}, {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(build_one, item): item for item in instance_ids}
        for future in as_completed(futures):
            instance_id = futures[future]
            try:
                results[instance_id] = future.result()
            except Exception as exc:
                failures[instance_id] = f"{type(exc).__name__}: {exc}"
                write_json(CASES_ROOT / instance_id / "meta/environment/build_failure.json", {
                    "schema_version": "case-environment-build-failure-v1", "instance_id": instance_id,
                    "failed_at": datetime.now(timezone.utc).isoformat(), "error": failures[instance_id],
                    "retryable": True,
                })
    return {"schema_version": "case-environment-build-batch-v1", "results": results,
            "failures": failures}
