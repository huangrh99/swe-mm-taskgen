"""Audit the immutable base-image contract for each case environment."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")


def _command(*parts: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(parts, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if check and result.returncode:
        raise RuntimeError(f"command_failed:{parts[0]}:{result.returncode}:{result.stdout[-500:]}")
    return result


def audit_one(case: Path, *, build_task_image: bool = True) -> dict:
    case = case.resolve()
    environment = case / "environment"
    errors = []
    required = ("Dockerfile", "base_image.json", "docker-compose.yaml", "assets")
    for relative in required:
        if not (environment / relative).exists():
            errors.append(f"missing:{relative}")
    if errors:
        return {"instance_id": case.name, "status": "incomplete", "errors": errors}
    try:
        binding = json.loads((environment / "base_image.json").read_text())
        reference, expected_id = binding["build_reference"], binding["image_id"]
        if not IMAGE_ID.fullmatch(expected_id):
            raise ValueError("invalid_image_id")
        inspect = json.loads(_command("docker", "image", "inspect", reference).stdout)[0]
        if inspect.get("Id") != expected_id:
            raise ValueError("image_id_mismatch")
        first = (environment / "Dockerfile").read_text().splitlines()[0]
        if first != f"FROM {reference}":
            raise ValueError("dockerfile_base_binding_mismatch")
        probe = _command(
            "docker", "run", "--rm", "--network", "none", "--entrypoint", "/bin/sh",
            reference, "-lc",
            "git -C /app rev-parse HEAD; test -z \"$(git -C /app status --porcelain --untracked-files=all)\"; "
            "test -z \"$(git -C /app remote)\"; printf 'REMOTE=empty\\n'; node --version; "
            "printf 'CHROMIUM='; command -v chromium || command -v chromium-browser",
        ).stdout.splitlines()
        if (not probe or probe[0] != binding.get("source_baseline_sha")
                or not any(line.startswith("REMOTE=") for line in probe)):
            raise ValueError("offline_source_probe_failed")
        assets = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in (environment / "assets").iterdir() if path.is_file()}
        expected_assets = {item["name"]: item["sha256"] for item in binding.get("asset_inventory", [])}
        if expected_assets and assets != expected_assets:
            raise ValueError("asset_inventory_mismatch")
        task_image = None
        if build_task_image:
            task_image = "case-environment-audit:" + case.name.lower().replace("__", "-")
            _command("docker", "build", "--tag", task_image, str(environment))
            final_probe = _command(
                "docker", "run", "--rm", "--network", "none", "--entrypoint", "/bin/sh",
                task_image, "-lc",
                "test -d /testbed/assets; test $(git -C /testbed rev-list --count HEAD) -eq 1; "
                "test -z \"$(git -C /testbed remote)\"; test -z \"$(git -C /testbed status --porcelain)\"",
            )
            if final_probe.returncode:
                raise ValueError("final_task_image_probe_failed")
        return {"instance_id": case.name, "status": "complete", "errors": [],
                "image_id": expected_id, "base_commit": binding["source_baseline_sha"],
                "asset_count": len(assets), "task_image": task_image, "probe": probe}
    except (KeyError, OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        return {"instance_id": case.name, "status": "failed",
                "errors": [f"{type(exc).__name__}: {exc}"]}


def audit_all(cases: list[Path], workers: int = 3) -> dict:
    with ThreadPoolExecutor(max_workers=workers) as pool:
        records = list(pool.map(audit_one, cases))
    records.sort(key=lambda item: item["instance_id"])
    return {"schema_version": "case-environment-audit-v1",
            "status": "complete" if all(x["status"] == "complete" for x in records) else "incomplete",
            "records": records}
