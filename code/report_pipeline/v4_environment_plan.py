"""Plan reusable V4 dependency environments from exact local Git objects.

This module is read-only with respect to repositories.  It never installs
dependencies, checks out a commit, builds an image, calls a model, or uses the
network.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import re
import subprocess

from report_pipeline.atomic import write_bytes, write_json


CODE_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = CODE_ROOT.parents[1]
REPOSITORY_NAMES = {
    "automattic/wp-calypso": "wp-calypso", "carbon-design-system/carbon": "carbon",
    "bpmn-io/bpmn-js": "bpmn-js", "grommet/grommet": "grommet",
    "googlechrome/lighthouse": "lighthouse", "mermaid-js/mermaid": "mermaid",
    "pixijs/pixijs": "pixijs", "excalidraw/excalidraw": "excalidraw",
    "apache/echarts": "echarts", "tldraw/tldraw": "tldraw",
    "apache/superset": "superset", "fabricjs/fabric.js": "fabric.js",
    "maplibre/maplibre-gl-js": "maplibre-gl-js", "xyflow/xyflow": "xyflow",
}


ROOT_FILES = {
    "package.json", "package-lock.json", "npm-shrinkwrap.json", "yarn.lock",
    "pnpm-lock.yaml", "pnpm-workspace.yaml", "lerna.json", "nx.json", "turbo.json",
    ".nvmrc", ".node-version", "pyproject.toml", "poetry.lock", "uv.lock",
    "Pipfile", "Pipfile.lock", "requirements.txt", "setup.py", "setup.cfg",
}
PYTHON_REQUIREMENTS = {
    "requirements/base.txt", "requirements/development.txt", "requirements/dev.txt",
    "requirements/testing.txt", "requirements/test.txt", "requirements/local.txt",
}


def _git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *arguments], cwd=repository, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, check=False)


def _packet_root(campaign: Path | None) -> Path | None:
    if campaign is None:
        return None
    value = campaign.resolve(strict=True)
    if value.is_file():
        value = value.parent
    if value.name == "20_17_02_model_runs":
        return value
    root = value / "20_17_02_model_runs"
    if not root.is_dir():
        raise ValueError("campaign has no 20_17_02_model_runs directory")
    return root


def _archive(case: dict) -> dict:
    binding = case.get("source_bindings") or {}
    path = Path(binding.get("source_archive", ""))
    if not path.is_absolute():
        path = WORKSPACE_ROOT / path
    if not path.is_file():
        raise ValueError("bound source archive is missing")
    expected = binding.get("source_archive_sha256")
    if expected and hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise ValueError("source archive hash mismatch")
    value = json.loads(path.read_text())
    if value.get("instance_id") != case["case_id"]:
        raise ValueError("source archive identity mismatch")
    return value


def _base(case: dict, packet_root: Path | None) -> tuple[str, str]:
    if packet_root is not None:
        packet_path = packet_root / case["case_id"] / "20_17_01_packet.json"
        if packet_path.is_file():
            packet = json.loads(packet_path.read_text())
            if packet.get("task_id") != case["case_id"] or packet.get("repository") != case["repository"]:
                raise ValueError(f"campaign packet identity mismatch: {case['case_id']}")
            return packet["base_commit"], str(packet_path)
    archive = _archive(case)
    return archive["sections"]["pull_request"]["data"]["base"]["sha"], "payload_source_archive"


def _dependency_paths(repository: Path, base: str) -> list[str]:
    tree = _git(repository, "ls-tree", "-r", "--name-only", base)
    if tree.returncode:
        raise ValueError("exact base object unavailable")
    paths = tree.stdout.decode(errors="replace").splitlines()
    return sorted(path for path in paths if path in ROOT_FILES or path in PYTHON_REQUIREMENTS)


def _blob(repository: Path, base: str, path: str) -> bytes:
    value = _git(repository, "show", f"{base}:{path}")
    if value.returncode:
        raise ValueError(f"cannot read dependency manifest: {path}")
    return value.stdout


def _runtime(manifests: dict[str, bytes]) -> tuple[str, dict, str, list[str], bool]:
    risks: list[str] = []
    unsupported = False
    package = {}
    if "package.json" in manifests:
        try:
            package = json.loads(manifests["package.json"])
        except json.JSONDecodeError:
            risks.append("invalid_root_package_json")
    package_manager = package.get("packageManager") or ""
    engines = package.get("engines") if isinstance(package.get("engines"), dict) else {}
    python = any(path in manifests for path in
                 ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt"))
    if "pyproject.toml" in manifests:
        try:
            pyproject = manifests["pyproject.toml"].decode()
            match = re.search(r"(?m)^\s*requires-python\s*=\s*['\"]([^'\"]+)['\"]", pyproject)
            if match:
                engines = {**engines, "python": match.group(1)}
        except UnicodeDecodeError:
            risks.append("invalid_pyproject_toml")
    if python:
        manager = package_manager or "python+frontend"
        command = "unsupported_without_curated_python_image"
        risks.extend(["python_environment_requires_system_libraries",
                      "python_lock_or_requirement_files_need_recipe_review"])
        unsupported = True
    elif "pnpm-lock.yaml" in manifests:
        manager = package_manager or "pnpm"
        command = "corepack pnpm install --frozen-lockfile"
    elif "yarn.lock" in manifests:
        manager = package_manager or "yarn"
        command = ("corepack yarn install --immutable" if package_manager.startswith("yarn@") and
                   not package_manager.startswith("yarn@1.") else
                   "yarn install --frozen-lockfile")
    elif "package-lock.json" in manifests or "npm-shrinkwrap.json" in manifests:
        manager = package_manager or "npm"
        command = "npm ci"
    else:
        manager = package_manager or "unknown"
        command = "unsupported_no_recognized_lockfile"
        risks.append("no_recognized_dependency_lockfile")
        unsupported = True
    if not engines:
        risks.append("runtime_engine_not_pinned_in_root_manifest")
    return manager, engines, command, risks, unsupported


def _inspect(repository: Path, repository_name: str, base: str) -> dict:
    if not repository.is_dir():
        return {"status": "unsupported", "unsupported": True,
                "risks": ["local_repository_missing"], "manifests": [],
                "package_manager": "unknown", "engines": {},
                "recommended_install_command": "unsupported_local_repository_missing"}
    paths = _dependency_paths(repository, base)
    manifests = {path: _blob(repository, base, path) for path in paths}
    bindings = [{"path": path, "sha256": hashlib.sha256(content).hexdigest(),
                 "size_bytes": len(content)} for path, content in manifests.items()]
    fingerprint_input = "".join(f"{item['path']}\0{item['sha256']}\n" for item in bindings)
    fingerprint = hashlib.sha256(fingerprint_input.encode()).hexdigest()
    manager, engines, command, risks, unsupported = _runtime(manifests)
    if repository_name == "apache/superset":
        unsupported = True
        command = "unsupported_without_curated_superset_image"
        risks.extend(["superset_mixed_python_node_stack", "superset_requires_curated_system_services"])
    return {"status": "unsupported" if unsupported else "planned",
            "unsupported": unsupported, "manifests": bindings,
            "dependency_fingerprint": fingerprint, "package_manager": manager,
            "engines": engines, "recommended_install_command": command,
            "risks": sorted(set(risks))}


def _render(plan: dict) -> str:
    rows = []
    for item in plan["cases"]:
        manifests = "<br>".join(
            f"<code>{html.escape(value['path'])}</code> {value['sha256'][:10]}"
            for value in item.get("manifests", [])) or "—"
        risks = "<br>".join(html.escape(value) for value in item.get("risks", [])) or "—"
        rows.append(
            f"<tr><td>{html.escape(item['case_id'])}</td><td>{html.escape(item['repository'])}</td>"
            f"<td><code>{html.escape(item['base_commit'][:12])}</code></td>"
            f"<td>{html.escape(item['environment_group'])}</td><td>{html.escape(item['status'])}</td>"
            f"<td>{html.escape(item['package_manager'])}<br><code>{html.escape(item['recommended_install_command'])}</code></td>"
            f"<td>{manifests}</td><td>{risks}</td></tr>")
    groups = "".join(
        f"<li><b>{html.escape(item['group_id'])}</b> · {html.escape(item['repository'])} · "
        f"{item['case_count']} case(s): {html.escape(', '.join(item['case_ids']))}</li>"
        for item in plan["reuse_groups"])
    return f"""<!doctype html><meta charset="utf-8"><title>V4 environment plan</title>
<style>body{{font:13px system-ui;margin:20px;color:#172033}}table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #dbe1ea;padding:6px;vertical-align:top}}th{{background:#f4f6f9;position:sticky;top:0}}
code{{overflow-wrap:anywhere}}.note{{color:#5d697b}}</style><h1>V4 provisional environment plan</h1>
<p class="note">Read-only plan from exact Git objects. Commands are recommendations; no install, image build, model call, or network action was performed.</p>
<p>{plan['case_count']} cases · {plan['reusable_group_count']} reusable groups · {plan['unsupported_count']} unsupported</p>
<h2>Reuse groups</h2><ul>{groups}</ul><h2>Cases</h2><table><thead><tr><th>Case</th><th>Repository</th>
<th>Exact base</th><th>Group</th><th>Status</th><th>Manager / frozen install</th><th>Bound manifests</th><th>Risks</th>
</tr></thead><tbody>{''.join(rows)}</tbody></table>"""


def run(payload_path: Path, repositories: Path, output: Path,
        campaign: Path | None = None) -> dict:
    payload_path = payload_path.resolve(strict=True)
    payload = json.loads(payload_path.read_text())
    cases = payload.get("cases") or []
    case_ids = [item.get("case_id") for item in cases]
    if not cases or None in case_ids or len(case_ids) != len(set(case_ids)):
        raise ValueError("payload cases are empty or duplicate")
    packet_root = _packet_root(campaign)
    cache: dict[tuple[str, str], dict] = {}
    planned = []
    for case in sorted(cases, key=lambda item: item["case_id"]):
        repository_name = case["repository"]
        base, base_source = _base(case, packet_root)
        repository = repositories.resolve() / REPOSITORY_NAMES.get(
            repository_name, repository_name.rsplit("/", 1)[-1])
        key = (repository_name, base)
        try:
            if key not in cache:
                cache[key] = _inspect(repository, repository_name, base)
            environment = cache[key]
        except Exception as exc:
            environment = {"status": "unsupported", "unsupported": True,
                           "risks": [f"{type(exc).__name__}:{exc}"], "manifests": [],
                           "dependency_fingerprint": hashlib.sha256(
                               f"unavailable:{repository_name}:{base}".encode()).hexdigest(),
                           "package_manager": "unknown", "engines": {},
                           "recommended_install_command": "unsupported_exact_base_unavailable"}
        planned.append({"case_id": case["case_id"], "repository": repository_name,
                        "base_commit": base, "base_source": base_source, **environment})
    grouped: dict[tuple[str, str], list[dict]] = {}
    for item in planned:
        grouped.setdefault((item["repository"], item["dependency_fingerprint"]), []).append(item)
    reuse_groups = []
    for index, ((repository_name, fingerprint), members) in enumerate(sorted(grouped.items()), 1):
        group_id = f"env-{index:02d}-{fingerprint[:12]}"
        for member in members:
            member["environment_group"] = group_id
        reuse_groups.append({"group_id": group_id, "repository": repository_name,
                             "dependency_fingerprint": fingerprint,
                             "case_count": len(members),
                             "case_ids": [member["case_id"] for member in members],
                             "reusable": len(members) > 1})
    result = {"schema_version": "v4-provisional-environment-plan-v1",
              "created_at": datetime.now(timezone.utc).isoformat(),
              "payload": str(payload_path), "campaign": str(campaign.resolve()) if campaign else None,
              "case_count": len(planned), "reusable_group_count": len(reuse_groups),
              "multi_case_reuse_group_count": sum(group["reusable"] for group in reuse_groups),
              "unsupported_count": sum(item["unsupported"] for item in planned),
              "reuse_groups": reuse_groups, "cases": planned,
              "boundary": "plan only; no dependency install, image build, model call, network access, or environment readiness claim"}
    output = output.resolve()
    if output.exists():
        raise ValueError(f"output already exists: {output}")
    output.mkdir(parents=True)
    write_json(output / "20_20_01_environment_plan.json", result)
    write_bytes(output / "20_20_02_environment_plan.html", _render(result).encode())
    return result
