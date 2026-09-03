"""Assemble a hash-bound, bounded dependency context from a base git tree.

The public interface intentionally accepts an already collected packet and one
repository object database.  Fetching, cloning and model invocation remain
outside this module; its only job is to turn exact git blobs into an auditable
input bundle or explain why that cannot be done safely.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import posixpath


CONTEXT_SCHEMA_VERSION = "repository-test-context-v1"
DEFAULT_MAX_FILES = 160
DEFAULT_MAX_BYTES = 2_500_000

_CODE_SUFFIXES = (".js", ".jsx", ".ts", ".tsx", ".d.ts", ".mjs", ".cjs", ".json",
                  ".css", ".scss", ".sass", ".less")
_IMPORT_PATTERNS = (
    re.compile(r"(?:\bfrom\s+|\bimport\s*\(|\brequire\s*\(|"
               r"\bjest\.(?:mock|requireActual)\s*\()\s*['\"](\.[^'\"]+)['\"]"),
    re.compile(r"\bimport\s*['\"](\.[^'\"]+)['\"]"),
    re.compile(r"@(?:use|forward|import)\s+['\"](\.[^'\"]+)['\"]"),
)
_TEST_PATH = re.compile(r"(?:^|/)(?:test|tests|spec|__tests__)(?:/|$)|"
                        r"(?:^|[-_.])(?:test|spec)\.[^.]+$", re.IGNORECASE)
_CONFIG_PATH = re.compile(r"(?:^|/)(?:package\.json|[^/]*(?:jest|vitest|karma|"
                          r"playwright|cypress|webpack|babel|tsconfig)[^/]*)$",
                          re.IGNORECASE)


class GitSnapshot:
    """Read immutable bytes from one commit without changing the checkout."""

    def __init__(self, repository: Path, commit: str):
        self.repository = repository.resolve(strict=True)
        self.commit = commit
        self._paths: list[str] | None = None
        self._path_set: set[str] | None = None
        self._blobs: dict[str, bytes | None] = {}
        self._verify_commit()

    def _run(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        process = subprocess.run(
            ["git", "-C", str(self.repository), *arguments],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if check and process.returncode:
            raise ValueError(
                f"git snapshot read failed: {' '.join(arguments[:2])}: "
                f"{process.stderr.decode(errors='replace')[:300]}")
        return process

    def _verify_commit(self) -> None:
        self._run("cat-file", "-e", f"{self.commit}^{{commit}}")

    def read(self, path: str) -> bytes | None:
        clean = _clean_path(path)
        if clean is None:
            return None
        if clean in self._blobs:
            return self._blobs[clean]
        if not self.exists(clean):
            self._blobs[clean] = None
            return None
        process = self._run("show", f"{self.commit}:{clean}", check=False)
        self._blobs[clean] = process.stdout if process.returncode == 0 else None
        return self._blobs[clean]

    def exists(self, path: str) -> bool:
        clean = _clean_path(path)
        if clean is None:
            return False
        if self._path_set is None:
            self.paths()
        return clean in self._path_set

    def paths(self) -> list[str]:
        if self._paths is None:
            output = self._run("ls-tree", "-r", "--name-only", self.commit).stdout
            self._paths = output.decode(errors="replace").splitlines()
            self._path_set = set(self._paths)
        return self._paths


def _clean_path(value: str) -> str | None:
    value = value.split("?", 1)[0].split("#", 1)[0].replace("\\", "/")
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts:
        return None
    return path.as_posix().removeprefix("./")


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _relative_modules(content: str) -> list[str]:
    return sorted({match for pattern in _IMPORT_PATTERNS
                   for match in pattern.findall(content)})


def _resolution_candidates(importer: str, module: str) -> list[str]:
    parent = PurePosixPath(importer).parent
    clean_module = module.split("?", 1)[0].split("#", 1)[0]
    base = posixpath.normpath(posixpath.join(parent.as_posix(), clean_module))
    suffix = PurePosixPath(base).suffix
    candidates = [base]
    if suffix not in _CODE_SUFFIXES:
        candidates.extend(base + item for item in _CODE_SUFFIXES)
        candidates.extend(f"{base}/index{item}" for item in _CODE_SUFFIXES)
    elif suffix == ".js":
        candidates.extend(base[:-3] + item for item in (".ts", ".tsx", ".d.ts", ".jsx"))
    if PurePosixPath(importer).suffix in {".scss", ".sass"}:
        name = PurePosixPath(base).name
        parent_path = PurePosixPath(base).parent
        candidates.extend((parent_path / f"_{name}{item}").as_posix()
                          for item in (".scss", ".sass"))
    return list(dict.fromkeys(filter(None, (_clean_path(item) for item in candidates))))


def _role(path: str, production: set[str], initial: set[str], discovered: bool) -> str:
    if path in production:
        return "sut"
    if _CONFIG_PATH.search(path):
        return "test_config"
    if _TEST_PATH.search(path):
        return "test_dependency" if discovered else "test_template"
    if path in initial:
        return "curator_selected_context"
    return "sut_dependency"


def _command_paths(context: dict) -> list[str]:
    result = []
    for item in context.get("allowed_test_commands") or []:
        try:
            tokens = shlex.split(item.get("command", ""))
        except ValueError:
            tokens = []
        for token in tokens:
            clean = _clean_path(token)
            if clean and ("/" in clean or PurePosixPath(clean).suffix):
                result.append(clean)
    return result


def _nearest_manifests(snapshot: GitSnapshot, paths: set[str]) -> list[str]:
    result = {"package.json"} if snapshot.exists("package.json") else set()
    for value in paths:
        parent = PurePosixPath(value).parent
        while parent.parts:
            candidate = (parent / "package.json").as_posix()
            if snapshot.exists(candidate):
                result.add(candidate)
                break
            parent = parent.parent
    return sorted(result)


def _nearby_tests(snapshot: GitSnapshot, production: set[str], existing: set[str],
                  *, scope: str = ".", limit: int = 4) -> list[str]:
    if any(_TEST_PATH.search(path) and not _CONFIG_PATH.search(path) for path in existing):
        return []
    scored = []
    for path in snapshot.paths():
        if scope != "." and not path.startswith(scope.rstrip("/") + "/"):
            continue
        if not _TEST_PATH.search(path) or _CONFIG_PATH.search(path):
            continue
        score = 0
        for source in production:
            source_path = PurePosixPath(source)
            candidate = PurePosixPath(path)
            if source_path.stem.lower() in candidate.stem.lower():
                score += 8
            common = 0
            for left, right in zip(source_path.parts, candidate.parts):
                if left != right:
                    break
                common += 1
            score += common
        if score:
            scored.append((-score, len(path), path))
    return [item[2] for item in sorted(scored)[:limit]]


def assemble_repository_test_context(packet: dict, repository: Path, *,
                                     base_commit: str | None = None,
                                     max_files: int = DEFAULT_MAX_FILES,
                                     max_bytes: int = DEFAULT_MAX_BYTES,
                                     max_dependency_depth: int = 1) -> dict:
    """Return a copied packet enriched with exact Base blobs and a completeness gate."""
    value = json.loads(json.dumps(packet))
    context = value.setdefault("repository_test_context", {})
    base_commit = (base_commit or value.get("production_change_summary", {}).get("base_commit")
                   or context.get("environment_inputs", {}).get("base_commit"))
    if not base_commit:
        raise ValueError("base commit is absent from the Verifier packet")
    snapshot = GitSnapshot(repository, base_commit)
    working_directory = context.get("working_directory") or "."
    scope = (_clean_path(working_directory)
             if not str(working_directory).startswith("/") else ".") or "."

    def canonical(path: str | None) -> str | None:
        clean = _clean_path(path or "")
        if clean is None:
            return None
        if snapshot.exists(clean):
            return clean
        scoped = _clean_path(posixpath.join(scope, clean)) if scope != "." else clean
        # A curator-generated test may not exist at Base. In a package-scoped
        # command its logical path still belongs below that working directory.
        return scoped if scope != "." else clean

    change = value.setdefault("production_change_summary", {})
    production_paths = list(change.get("paths") or [])
    if not production_paths:
        production_paths = [match.group(1) for match in re.finditer(
            r"^diff --git a/(.+?) b/(.+?)$", change.get("patch", ""), re.MULTILINE)
            if not _TEST_PATH.search(match.group(1))
            and PurePosixPath(match.group(1)).suffix.lower() not in {".md", ".mdx", ".txt"}
            and not match.group(1).startswith(".changeset/")]
        change["paths"] = production_paths
        change["paths_inferred_from_patch_headers"] = True
    production = {canonical(path) for path in production_paths}
    production.discard(None)
    supplied = value.setdefault("existing_tests", {}).setdefault("files", [])
    initial = {canonical(item.get("path", "")) for item in supplied}
    initial.discard(None)
    command_paths = {canonical(path) for path in _command_paths(context)}
    command_paths = {path for path in command_paths if path and snapshot.exists(path)}
    nearby = set(_nearby_tests(snapshot, production, initial, scope=scope))
    seeds = set(production) | set(initial) | command_paths | nearby
    seeds.update(_nearest_manifests(snapshot, seeds))

    records: dict[str, dict] = {}
    blockers: list[dict] = []
    warnings: list[dict] = []
    edges = []
    supplied_by_path = {canonical(item["path"]): item for item in supplied if item.get("path")}
    pending = [(path, None, None, 0) for path in sorted(seeds)]
    total_bytes = 0
    while pending:
        path, requested_by, import_specifier, depth = pending.pop(0)
        if path in records:
            if requested_by:
                edges.append({"from": requested_by, "specifier": import_specifier,
                              "to": path, "status": "resolved"})
            continue
        supplied_item = supplied_by_path.get(path)
        payload = snapshot.read(path)
        source = "base_git_blob"
        base_match = True
        if supplied_item is not None and isinstance(supplied_item.get("content"), str):
            supplied_payload = supplied_item["content"].encode()
            declared = supplied_item.get("sha256")
            if declared and declared != _sha(supplied_payload):
                blockers.append({"code": "supplied_hash_mismatch", "path": path})
            if payload is None or payload != supplied_payload:
                payload = supplied_payload
                source = "packet_supplied_nonbase"
                base_match = False
        if payload is None:
            blockers.append({"code": "required_blob_missing", "path": path,
                             "requested_by": requested_by})
            if requested_by:
                edges.append({"from": requested_by, "specifier": import_specifier,
                              "to": None, "status": "missing"})
            continue
        try:
            content = payload.decode("utf-8")
        except UnicodeDecodeError:
            warnings.append({"code": "binary_blob_omitted", "path": path,
                             "sha256": _sha(payload), "size_bytes": len(payload)})
            continue
        if len(records) >= max_files or total_bytes + len(payload) > max_bytes:
            blockers.append({"code": "context_limit_exceeded", "path": path,
                             "max_files": max_files, "max_bytes": max_bytes})
            break
        total_bytes += len(payload)
        records[path] = {
            "path": path, "sha256": _sha(payload), "size_bytes": len(payload),
            "content": content, "role": _role(path, production, initial, requested_by is not None),
            "source": source, "base_blob_matches": base_match,
            "requested_by": requested_by, "dependency_depth": depth,
        }
        if requested_by:
            edges.append({"from": requested_by, "specifier": import_specifier,
                          "to": path, "status": "resolved"})
        if depth >= max_dependency_depth:
            continue
        for module in _relative_modules(content):
            resolved = next((candidate for candidate in _resolution_candidates(path, module)
                             if snapshot.exists(candidate)), None)
            if resolved is None:
                diagnostic = {"code": "relative_import_unresolved", "path": path,
                              "specifier": module}
                if records[path]["role"] in {"sut", "test_template"}:
                    blockers.append(diagnostic)
                else:
                    warnings.append(diagnostic)
                edges.append({"from": path, "specifier": module, "to": None,
                              "status": "unresolved"})
            else:
                pending.append((resolved, path, module, depth + 1))

    for path in sorted(production):
        if path not in records:
            blockers.append({"code": "sut_source_missing", "path": path})
    commands = context.get("allowed_test_commands") or []
    if not commands or any(not item.get("command") for item in commands):
        blockers.append({"code": "frozen_test_command_missing"})
    if not context.get("test_collection_roots"):
        blockers.append({"code": "test_collection_roots_missing"})
    if not any(record["role"] == "test_template" for record in records.values()):
        warnings.append({"code": "nearby_test_template_not_found",
                         "effect": "Verifier must use framework config and supplied source APIs only"})

    command_evidence = []
    for command in commands:
        text = command.get("command", "")
        try:
            tokens = shlex.split(text)
        except ValueError:
            tokens = []
        script_name = None
        explicit_script_reference = False
        binary_name = None
        if len(tokens) >= 3 and tokens[0] in {"npm", "pnpm"} and tokens[1] == "run":
            script_name = tokens[2]
            explicit_script_reference = True
        elif len(tokens) >= 2 and tokens[0] == "npm" and tokens[1] in {"test", "start"}:
            script_name = tokens[1]
            explicit_script_reference = True
        elif len(tokens) >= 2 and tokens[0] == "yarn" and tokens[1] not in {
                "exec", "workspace", "install"}:
            script_name = tokens[1]
            binary_name = tokens[1]
        elif len(tokens) >= 3 and tokens[0] in {"pnpm", "yarn"} and tokens[1] == "exec":
            binary_name = tokens[2]
        manifest_path = canonical("package.json")
        script_value = None
        if manifest_path and snapshot.read(manifest_path) is not None and script_name:
            try:
                script_value = json.loads(snapshot.read(manifest_path))["scripts"].get(script_name)
            except (KeyError, TypeError, json.JSONDecodeError):
                pass
        if explicit_script_reference and script_name and script_value is None:
            blockers.append({"code": "package_script_missing", "command_id": command.get(
                "command_id"), "manifest": manifest_path, "script": script_name})
        dependency_evidence = None
        if binary_name and script_value is None:
            for candidate in dict.fromkeys(filter(None, (manifest_path, "package.json"))):
                payload = snapshot.read(candidate)
                if payload is None:
                    continue
                try:
                    manifest = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                for field in ("devDependencies", "dependencies", "peerDependencies"):
                    version = (manifest.get(field) or {}).get(binary_name)
                    if version:
                        dependency_evidence = {"manifest": candidate, "field": field,
                                               "name": binary_name, "version": version}
                        break
                if dependency_evidence:
                    break
        referenced = []
        for path in _command_paths({"allowed_test_commands": [command]}):
            canonical_path = canonical(path)
            if canonical_path and snapshot.exists(canonical_path):
                payload = snapshot.read(canonical_path)
                referenced.append({"path": canonical_path, "sha256": _sha(payload)})
        provenance_kind = ("repository_package_script" if script_value else
                           "repository_package_dependency" if dependency_evidence else
                           "curator_frozen_with_repository_paths" if referenced else
                           "curator_frozen_only")
        if provenance_kind == "curator_frozen_only" and not script_name:
            warnings.append({"code": "command_has_no_repository_script_or_path_evidence",
                             "command_id": command.get("command_id")})
        command_evidence.append({
            "command_id": command.get("command_id"), "working_directory": command.get(
                "working_directory", working_directory), "command": text,
            "provenance_kind": provenance_kind,
            "package_script": ({"manifest": manifest_path, "name": script_name,
                                "value": script_value} if script_value else None),
            "package_dependency": dependency_evidence,
            "referenced_repository_paths": referenced,
        })

    # Deduplicate deterministic diagnostics without losing their structured fields.
    unique_blockers = list({json.dumps(item, sort_keys=True): item for item in blockers}.values())
    unique_warnings = list({json.dumps(item, sort_keys=True): item for item in warnings}.values())
    context.update({
        "context_schema_version": CONTEXT_SCHEMA_VERSION,
        "source_tree": {"repository": str(snapshot.repository), "base_commit": base_commit,
                        "working_tree_scope": scope,
                        "read_method": "git-show-without-checkout"},
        "context_files": [records[path] for path in sorted(records)],
        "dependency_resolution": {"seed_paths": sorted(seeds), "edges": edges,
                                  "nearby_test_candidates": sorted(nearby)},
        "command_evidence": command_evidence,
        "limits": {"max_files": max_files, "max_bytes": max_bytes,
                   "max_dependency_depth": max_dependency_depth,
                   "actual_files": len(records), "actual_bytes": total_bytes},
        "completeness": {"status": "complete" if not unique_blockers else "incomplete",
                         "blockers": unique_blockers, "warnings": unique_warnings},
    })
    # Keep the legacy field as the single byte-bearing input consumed by the V3
    # prompt and validator, but make every file's role and provenance explicit.
    value["existing_tests"]["files"] = context["context_files"]
    value["existing_tests"]["file_hashes"] = {
        item["path"]: item["sha256"] for item in context["context_files"]}
    return value
