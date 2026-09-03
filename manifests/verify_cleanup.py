#!/usr/bin/env python3
"""Machine-check the formal cleanup and migration claims."""

import ast
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = Path(__file__).with_name("cleanup_migration.json")


def _check_test_reference(reference: str) -> None:
    parts = reference.split(".")
    if len(parts) != 4 or parts[0] != "tests":
        raise ValueError(f"invalid replacement test reference: {reference}")
    path = ROOT / "code/tests" / f"{parts[1]}.py"
    if not path.is_file():
        raise ValueError(f"replacement test module is missing: {reference}")
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == parts[2]:
            if any(isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == parts[3]
                   for child in node.body):
                return
    raise ValueError(f"replacement test does not exist: {reference}")


def _active_imports() -> set[str]:
    names: set[str] = set()
    for path in (ROOT / "code").rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
    return names


def validate() -> int:
    data = json.loads(MANIFEST.read_text())
    if data.get("schema_version") != "cleanup-migration-v1":
        raise ValueError("unexpected cleanup schema")
    caches = [path for path in ROOT.rglob("*")
              if ".runtime" not in path.parts
              and (path.name == "__pycache__" or path.suffix == ".pyc")]
    if caches:
        raise ValueError(f"compiled Python cache remains in formal report: {caches[0]}")
    for item in data["deleted"]:
        if (ROOT / item["path"]).exists():
            raise ValueError(f"claimed deletion still exists: {item['path']}")
        replacement_test = item.get("replacement_test", "")
        if not replacement_test:
            raise ValueError(f"deleted item lacks replacement test: {item['path']}")
        _check_test_reference(replacement_test)
    imported = _active_imports()
    for item in data["deleted"]:
        path = item["path"]
        if path.startswith("report/code/") and path.endswith(".py"):
            module = path.removeprefix("report/code/").removesuffix(".py").replace("/", ".")
            if module in imported:
                raise ValueError(f"deleted module remains imported: {module}")
    listing = subprocess.run([sys.executable, str(ROOT / "run.py"), "list"],
                             text=True, capture_output=True, check=True).stdout
    if "legacy-bpmn" in listing:
        raise ValueError("legacy bpmn command remains public")
    for item in data["modified"]:
        if not (ROOT / item["path"]).is_file():
            raise ValueError(f"modified retained module is missing: {item['path']}")
        replacement = item.get("replacement")
        if replacement is not None:
            command = replacement.removeprefix("report-pipeline ").split()[0]
            if command not in listing:
                raise ValueError(f"declared replacement command is not public: {replacement}")
    prompt_bytes = []
    for item in data["prompt_inventory"]:
        for key in ("system", "schema"):
            path = ROOT / item[key]
            if not path.is_file():
                raise ValueError(f"missing retained prompt artifact: {item[key]}")
            prompt_bytes.append(path.read_bytes())
    for item in data.get("agent_prompt_inventory", []):
        for artifact in item["artifacts"]:
            path = ROOT / artifact
            if not path.is_file():
                raise ValueError(f"missing retained agent prompt artifact: {artifact}")
            prompt_bytes.append(path.read_bytes())
    if len(prompt_bytes) != len(set(prompt_bytes)):
        raise ValueError("prompt inventory contains byte-identical duplicates")
    if not (ROOT / "run.py").is_file():
        raise ValueError("isolated formal wrapper is missing")
    return len(prompt_bytes)


if __name__ == "__main__":
    print(f"cleanup-ok prompt_artifacts={validate()}")
