"""Deterministically export a measured visual PR as a Harbor task.

The exporter deliberately separates candidate admission from both human gates:
high-confidence verifier evidence may reach this stage, while visual-necessity
and F2P/P2P semantic calibration remain explicit metadata until a human signs
them off.  Only Issue-derived assets already marked safe in the dossier are
copied into the agent environment.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from report_pipeline.atomic import assert_no_symlink_chain, write_json as _safe_write_json
from report_pipeline.paths import TMP_ROOT


MAX_PAYLOAD_FILES = 512
MAX_PAYLOAD_FILE_BYTES = 64 * 1024 * 1024
MAX_PAYLOAD_TOTAL_BYTES = 256 * 1024 * 1024


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json(path: Path) -> dict:
    return json.loads(path.resolve().read_text())


def _run(*args: str) -> str:
    result = subprocess.run(args, text=True, capture_output=True, check=False)
    if result.returncode:
        raise ValueError(f"command failed ({' '.join(args)}): {result.stderr.strip()}")
    return result.stdout.strip()


def _git_diff_preserve(repo: Path, base: str, reference: str,
                       filenames: list[str]) -> str:
    """Return an applyable patch without stripping significant context lines."""
    result = subprocess.run(
        ["git", "-C", str(repo.resolve()), "diff", "--binary", base, reference,
         "--", *filenames],
        text=True, capture_output=True, check=False,
    )
    if result.returncode:
        raise ValueError(f"command failed (git diff): {result.stderr.strip()}")
    patch = result.stdout
    if patch and not patch.endswith("\n"):
        patch += "\n"
    return patch


def _safe_relative(path: str) -> Path:
    value = Path(path)
    if value.is_absolute() or ".." in value.parts or not value.parts:
        raise ValueError(f"unsafe task path: {path}")
    return value


def _payload_inventory(root: Path) -> list[dict]:
    files: list[dict] = []
    total_bytes = 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or (not path.is_file() and not path.is_dir()):
            raise ValueError("hidden test payload contains an unsafe entry")
        if not path.is_file():
            continue
        size = path.stat().st_size
        if size > MAX_PAYLOAD_FILE_BYTES:
            raise ValueError("hidden test payload file exceeds size limit")
        total_bytes += size
        files.append({"path": path.relative_to(root).as_posix(),
                      "sha256": digest(path), "size_bytes": size})
        if len(files) > MAX_PAYLOAD_FILES or total_bytes > MAX_PAYLOAD_TOTAL_BYTES:
            raise ValueError("hidden test payload exceeds export budget")
    if not files:
        raise ValueError("hidden test payload is empty")
    return files


def _snapshot_payload(source: Path, destination: Path, expected: list[dict]) -> None:
    """Copy once, without following links, and bind all later work to the copy."""
    shutil.copytree(source, destination, symlinks=True)
    try:
        actual = _payload_inventory(destination)
        if actual != expected:
            raise ValueError("hidden test payload changed while being copied")
    except Exception:
        shutil.rmtree(destination)
        raise


def _export_manifest_path(output: Path) -> Path:
    """Keep curator metadata beside the provisional task under tmp/."""
    return output.parent / f"{output.name}.export_manifest.json"


def _publication_paths(output: Path) -> tuple[Path, Path]:
    sidecar = _export_manifest_path(output)
    return (sidecar.with_name(f".{sidecar.name}.transaction.json"),
            sidecar.with_name(f"{sidecar.name}.commit.json"))


def _material_sha(root: Path) -> str:
    files = {path.relative_to(root).as_posix(): digest(path)
             for path in sorted(root.rglob("*")) if path.is_file()}
    entries = [{"path": name, "sha256": value} for name, value in sorted(files.items())]
    return hashlib.sha256(json.dumps(entries, separators=(",", ":")).encode()).hexdigest()


def _atomic_json(path: Path, value: dict) -> None:
    _safe_write_json(path, value)


def _recover_incomplete_publication(output: Path, sidecar: Path, staging: Path,
                                    sidecar_staging: Path, transaction: Path,
                                    commit: Path) -> None:
    """Roll back only hash-bound artifacts from an interrupted pre-commit publish."""
    if commit.exists():
        validate_publication(output, allow_transaction=True)
        if transaction.exists():
            commit_value = json.loads(commit.read_text())
            if commit_value.get("transaction_sha256") != digest(transaction):
                raise ValueError("committed export transaction changed; manual recovery required")
            transaction.unlink()
        return
    if not transaction.exists():
        return
    value = json.loads(transaction.read_text())
    if value.get("schema_version") != "visual-harbor-export-transaction-v1":
        raise ValueError("export publication transaction is invalid")
    if output.exists():
        if not output.is_dir() or _material_sha(output) != value.get("task_material_sha256"):
            raise ValueError("interrupted export task changed; manual recovery required")
        shutil.rmtree(output)
    if sidecar.exists():
        if not sidecar.is_file() or digest(sidecar) != value.get("sidecar_sha256"):
            raise ValueError("interrupted export sidecar changed; manual recovery required")
        sidecar.unlink()
    if staging.exists():
        if staging.is_dir():
            shutil.rmtree(staging)
        else:
            raise ValueError("interrupted export staging is not a directory")
    sidecar_staging.unlink(missing_ok=True)
    transaction.unlink()


def validate_publication(output: Path, *, allow_transaction: bool = False) -> dict:
    """Verify the external sidecar and atomic commit for a published Harbor task."""
    sidecar = _export_manifest_path(output)
    transaction, commit = _publication_paths(output)
    if (transaction.exists() or transaction.is_symlink()) and not allow_transaction:
        raise ValueError("export publication is incomplete")
    if (output.is_symlink() or not output.is_dir() or sidecar.is_symlink()
            or not sidecar.is_file() or commit.is_symlink() or not commit.is_file()):
        raise ValueError("export publication evidence is missing or unsafe")
    value = json.loads(commit.read_text())
    if (value.get("schema_version") != "visual-harbor-export-commit-v1"
            or value.get("task_material_sha256") != _material_sha(output)
            or value.get("sidecar_sha256") != digest(sidecar)):
        raise ValueError("export publication commit does not match task and sidecar")
    record = json.loads(sidecar.read_text())
    if (record.get("schema_version") != "visual-harbor-export-v1"
            or record.get("task_material_sha256") != value["task_material_sha256"]):
        raise ValueError("export manifest does not match publication commit")
    return record


def _rebuild_candidate(bindings: dict) -> dict:
    from report_pipeline.candidate import build
    classification = bindings.get("classification_path")
    return build(Path(bindings["verifier_path"]), Path(bindings["archive_path"]),
                 Path(classification) if classification else None)


def _inspect_base_image(base_image: str) -> dict:
    inspect = json.loads(_run("docker", "image", "inspect", base_image))[0]
    image_id = inspect.get("Id", "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise ValueError("base image is not content addressed")
    repo_digests = [value for value in inspect.get("RepoDigests") or []
                    if re.fullmatch(r"[^@\s]+@sha256:[0-9a-f]{64}", value)]
    repo_digest = (next((value for value in repo_digests if value.startswith("visual-harbor-base@")),
                        sorted(repo_digests)[0]) if repo_digests else None)
    prefix = ("docker", "run", "--rm", "--network", "none", "--entrypoint", "git",
              image_id, "-C", "/app")
    app_head = _run(*prefix, "rev-parse", "HEAD")
    app_tree = _run(*prefix, "rev-parse", "HEAD^{tree}")
    if not re.fullmatch(r"[0-9a-f]{40}", app_head) or not re.fullmatch(r"[0-9a-f]{40}", app_tree):
        raise ValueError("base image /app lacks a valid Git HEAD/tree")
    if _run(*prefix, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("base image /app contains tracked or untracked workspace changes")
    if _run(*prefix, "remote"):
        raise ValueError("base image /app retains Git remotes")
    leak_scan = _run(
        "docker", "run", "--rm", "--network", "none", "--entrypoint", "/bin/sh", image_id,
        "-lc", "find /app -path /app/.git -prune -o -path /app/node_modules -prune "
        "-o -path /app/.yarn/patches -prune -o -type d -name .git -print "
        "-o -type f \\( -name .env -o -name .env.local -o -name gold.patch "
        "-o -name reference.patch -o -name '*.bundle' \\) -print",
    )
    if leak_scan:
        raise ValueError("base image /app contains nested Git or sensitive artifact names")
    return {"image_id": image_id, "build_reference": image_id, "repo_digest": repo_digest,
            "app_head": app_head, "app_tree": app_tree}


def _validate_inputs(dossier_path: Path, tests_path: Path, measurement_path: Path,
                     repo: Path, instruction: Path,
                     functional_runner: Path | None) -> tuple[dict, dict, dict]:
    dossier, tests, measurement = map(_json, (dossier_path, tests_path, measurement_path))
    candidate = dossier["candidate_id"]
    bindings = dossier.get("source_bindings", {})
    binding_names = ["archive", "verifier", "packet", "curator_assets"]
    if "classification_path" in bindings or "classification_sha256" in bindings:
        binding_names.append("classification")
    for name in binding_names:
        path_value, sha_value = bindings.get(f"{name}_path"), bindings.get(f"{name}_sha256")
        if not path_value or not sha_value or not Path(path_value).resolve().is_file():
            raise ValueError(f"missing dossier source binding: {name}")
        if digest(Path(path_value).resolve()) != sha_value:
            raise ValueError(f"dossier source binding changed: {name}")
    rebuilt = _rebuild_candidate(bindings)
    for key in ("candidate_id", "status", "repository", "pr_number", "url", "title", "git",
                "changed_files", "source_bindings", "leakage_policy"):
        if dossier.get(key) != rebuilt.get(key):
            raise ValueError(f"dossier differs from source-derived candidate: {key}")
    source_admission_keys = (
        "decision", "admission_scope", "selection_policy",
        "model_evidence_is_not_human_confirmation", "visual_bucket",
        "text_only_bucket", "confidence", "reason", "v3_classification",
        "raw_model_evidence",
    )
    dossier_admission = dossier.get("visual_admission", {})
    rebuilt_admission = rebuilt.get("visual_admission", {})
    if any(dossier_admission.get(key) != rebuilt_admission.get(key)
           for key in source_admission_keys):
        raise ValueError("dossier differs from source-derived candidate: visual_admission")
    if dossier["status"] != "admitted_to_test_construction":
        raise ValueError("candidate was not admitted to test construction")
    if tests["candidate_id"] != candidate:
        raise ValueError("candidate/test identity mismatch")
    if not measurement.get("all_transitions_match"):
        raise ValueError("F2P/P2P measurement does not match the frozen transitions")
    oracle_quality = measurement.get("oracle_quality_validation")
    if measurement.get("mode") == "real":
        if not isinstance(oracle_quality, dict):
            raise ValueError("real measurement lacks oracle-quality validation")
        quality_path = Path(oracle_quality.get("path", ""))
        if (not quality_path.is_absolute()
                or not quality_path.is_file()
                or digest(quality_path) != oracle_quality.get("sha256")):
            raise ValueError("oracle-quality validation binding changed")
        quality = _json(quality_path)
        if (quality.get("status") != "passed"
                or quality.get("instance_id") != candidate
                or quality.get("solver_visible") is not False):
            raise ValueError("oracle-quality validation has not passed")
    measured_ids = [item["test_id"] for item in measurement["transitions"]]
    frozen_ids = [item["test_id"] for item in tests["tests"]]
    if measured_ids != frozen_ids or len(frozen_ids) != len(set(frozen_ids)):
        raise ValueError("measurement/test inventory mismatch")
    for test in tests["tests"]:
        _safe_relative(str(test["path"]))
    if measurement.get("semantic_calibration") != "pending_human_review":
        raise ValueError("unexpected semantic calibration state")
    execution = tests.get("execution")
    if execution:
        if (execution.get("kind") != "command_json_v1"
                or execution.get("command") != ["/usr/bin/python3", "-I", "/tests/functional_runner.py"]
                or functional_runner is None or not functional_runner.resolve().is_file()):
            raise ValueError("invalid or missing functional runner binding")
    elif functional_runner is not None:
        raise ValueError("functional runner requires manifest execution")
    elif measurement.get("oracle_kind") == "chromium_computed_style":
        raise ValueError("Chromium measurement requires a functional Harbor runner")
    if dossier["visual_admission"]["human_calibration_state"] not in {"pending", "approved"}:
        raise ValueError("invalid visual human-calibration state")
    if dossier["test_calibration"]["human_semantic_calibration_state"] not in {"pending", "approved"}:
        raise ValueError("invalid test human-calibration state")
    if not instruction.is_file() or not instruction.read_text().strip():
        raise ValueError("missing agent instruction")
    expected_head = dossier["git"]["baseline_sha"]
    actual_head = _run("git", "-C", str(repo.resolve()), "rev-parse", "HEAD")
    if actual_head != expected_head:
        raise ValueError(f"repository is not at frozen baseline: {actual_head}")
    changed = _run("git", "-C", str(repo.resolve()), "status", "--porcelain", "--untracked-files=all")
    if changed:
        raise ValueError("baseline workspace contains tracked or untracked changes")
    return dossier, tests, measurement


def _write_integrity_launcher(output: Path, manifest_path: Path, inventory_path: Path,
                              verify_path: Path) -> None:
    integrity_path = output / "tests/integrity.py"
    protected = {
        "/tests/sweb_grade.py": digest(verify_path),
        "/tests/test_manifest.json": digest(manifest_path),
        "/tests/frozen_inventory.json": digest(inventory_path),
        "/tests/config.json": digest(output / "tests/config.json"),
        "/tests/test.patch": digest(output / "tests/test.patch"),
    }
    runner = output / "tests/functional_runner.py"
    if runner.is_file():
        protected["/tests/functional_runner.py"] = digest(runner)
    payload = output / "tests/payload"
    if payload.is_dir():
        for path in sorted(payload.rglob("*")):
            if path.is_file():
                relative = path.relative_to(output / "tests").as_posix()
                protected[f"/tests/{relative}"] = digest(path)
    integrity_manifest = output / "tests/integrity_manifest.json"
    integrity_manifest.write_text(json.dumps({
        "schema_version": "frozen-test-integrity-v1",
        "files": protected,
    }, ensure_ascii=False, indent=2) + "\n")
    integrity_manifest_sha256 = digest(integrity_manifest)
    (output / "tests/test.sh").write_text(
        "#!/bin/bash\nset -eu\nmkdir -p /logs/verifier\n"
        "if ! /usr/bin/python3 -I /tests/integrity.py "
        f"/tests/integrity_manifest.json {integrity_manifest_sha256}; then exit 0; fi\n"
        "if [ -s /tests/test.patch ]; then "
        "/usr/bin/git -C /testbed apply --whitespace=nowarn /tests/test.patch; fi\n"
        "/usr/bin/python3 -I /tests/sweb_grade.py\n"
    )
    (output / "tests/test.sh").chmod(0o755)


def refresh_test_contract(output: Path, expected_tests: list[dict] | None = None) -> None:
    """Refresh coherent hashes after an intentional control-only manifest edit."""
    manifest_path = output / "tests/test_manifest.json"
    inventory_path = output / "tests/frozen_inventory.json"
    verify_path = output / "tests/sweb_grade.py"
    manifest = _json(manifest_path)
    inventory = _json(inventory_path)
    inventory["test_manifest_sha256"] = digest(manifest_path)
    if expected_tests is not None:
        inventory["expected_tests"] = expected_tests
        inventory["expected_count"] = len(expected_tests)
    inventory_path.write_text(json.dumps(inventory, ensure_ascii=False, indent=2) + "\n")
    _write_integrity_launcher(output, manifest_path, inventory_path, verify_path)


def _write_verifier(output: Path, tests: dict, forbidden_git_commits: list[str]) -> None:
    manifest_path = output / "tests/test_manifest.json"
    expected = [{"test_id": item["test_id"], "class": item["class"]} for item in tests["tests"]]
    inventory = {
        "schema_version": "harbor-frozen-test-inventory-v1",
        "candidate_id": tests["candidate_id"],
        "test_manifest_sha256": digest(manifest_path),
        "expected_tests": expected,
        "expected_count": len(expected),
        "forbidden_git_commits": forbidden_git_commits,
        "reward_contract": "exact expected inventory; every required test must execute and pass",
    }
    inventory_path = output / "tests/frozen_inventory.json"
    inventory_path.write_text(json.dumps(inventory, ensure_ascii=False, indent=2) + "\n")
    verifier = '''#!/usr/bin/env python3
import hashlib
import json
import os
import signal
import subprocess
from pathlib import Path

def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def quiesce_uid(uid):
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return []
    for sig in (signal.SIGSTOP, signal.SIGKILL):
        for _ in range(3):
            for status_path in proc_root.glob("[0-9]*/status"):
                try:
                    fields = status_path.read_text().splitlines()
                    observed = next(line for line in fields if line.startswith("Uid:"))
                    if int(observed.split()[1]) == uid:
                        os.kill(int(status_path.parent.name), sig)
                except (FileNotFoundError, ProcessLookupError):
                    pass
                except Exception as exc:
                    return [f"{type(exc).__name__}: {exc}"]
    remaining = []
    for status_path in proc_root.glob("[0-9]*/status"):
        try:
            observed = next(line for line in status_path.read_text().splitlines()
                            if line.startswith("Uid:"))
            if int(observed.split()[1]) == uid:
                remaining.append(status_path.parent.name)
        except (FileNotFoundError, ProcessLookupError, StopIteration):
            pass
    return remaining

tests_root = Path(os.environ.get("HARBOR_TEST_ROOT", "/tests"))
app_root = Path(os.environ.get("HARBOR_APP_ROOT", "/testbed"))
transport_root = Path(os.environ.get("HARBOR_TRANSPORT_ROOT", "/opt/benchmark-transport"))
logs = Path("/logs/verifier")
logs.mkdir(parents=True, exist_ok=True)
os.chmod(logs, 0o700)
manifest_path = tests_root / "test_manifest.json"
inventory = json.loads((tests_root / "frozen_inventory.json").read_text())
manifest = json.loads(manifest_path.read_text())
expected = inventory["expected_tests"]
expected_ids = [item["test_id"] for item in expected]
actual = manifest.get("tests", [])
actual_ids = [item.get("test_id") for item in actual]
contract_errors = []
agent_patch = transport_root / "agent.patch"
try:
    status_path = transport_root / "status"
    if status_path.is_symlink() or not status_path.is_file() or status_path.read_text() != "ok\\n":
        raise ValueError("root collect hook did not produce a valid success marker")
    if agent_patch.is_symlink() or not agent_patch.is_file():
        raise ValueError("agent patch transport is missing or unsafe")
    if agent_patch.stat().st_size:
        applied = subprocess.run(
            ["git", "-c", f"safe.directory={app_root}", "-C", str(app_root),
             "apply", "--whitespace=nowarn", str(agent_patch)],
            text=True, capture_output=True, check=False,
        )
        if applied.returncode:
            raise ValueError(f"git apply exited {applied.returncode}: {applied.stderr[-4000:]}")
except Exception as exc:
    contract_errors.append({"code": "agent_patch_transport_failed",
                            "error": f"{type(exc).__name__}: {exc}"})
if manifest.get("candidate_id") != inventory.get("candidate_id"):
    contract_errors.append({"code": "candidate_id_mismatch"})
if sha256(manifest_path) != inventory.get("test_manifest_sha256"):
    contract_errors.append({"code": "manifest_hash_mismatch"})
if len(actual_ids) != len(set(actual_ids)):
    contract_errors.append({"code": "duplicate_test_id", "actual_ids": actual_ids})
if actual_ids != expected_ids:
    contract_errors.append({"code": "test_inventory_mismatch", "expected_ids": expected_ids,
                            "actual_ids": actual_ids})
for commit in inventory.get("forbidden_git_commits", []):
    probe = subprocess.run(["git", "-c", f"safe.directory={app_root}", "-C", str(app_root),
                            "cat-file", "-e", f"{commit}^{{commit}}"],
                           text=True, capture_output=True, check=False)
    if probe.returncode == 0:
        contract_errors.append({"code": "source_git_history_leak", "commit": commit})
remotes = subprocess.run(["git", "-c", f"safe.directory={app_root}", "-C", str(app_root),
                          "remote"], text=True,
                         capture_output=True, check=False)
if remotes.returncode != 0:
    contract_errors.append({"code": "source_git_remote_check_failed",
                            "error": remotes.stderr[-4000:]})
elif remotes.stdout.strip():
    contract_errors.append({"code": "source_git_remote_leak",
                            "remotes": remotes.stdout.splitlines()})

functional_results = {}
functional_error = None
execution = manifest.get("execution")
if execution:
    try:
        completed = subprocess.run(execution["command"], text=True, capture_output=True,
                                   timeout=150, check=False)
        if completed.returncode:
            functional_error = f"runner exited {completed.returncode}: {completed.stderr[-4000:]}"
        else:
            payload = json.loads(completed.stdout)
            runner_items = payload.get("results", [])
            runner_ids = [item.get("test_id") for item in runner_items]
            expected_functional_ids = [item["test_id"] for item in actual
                                       if item.get("kind") == "functional_result"]
            if runner_ids != expected_functional_ids or len(runner_ids) != len(set(runner_ids)):
                contract_errors.append({"code": "functional_result_inventory_mismatch",
                                        "expected_ids": expected_functional_ids,
                                        "actual_ids": runner_ids})
            functional_results = {item["test_id"]: item for item in runner_items
                                  if item.get("test_id") is not None}
    except Exception as exc:
        functional_error = f"{type(exc).__name__}: {exc}"
    if functional_error:
        contract_errors.append({"code": "functional_runner_failed", "error": functional_error})
    leaked_processes = quiesce_uid(10002)
    os.chmod(logs, 0o700)
    if leaked_processes:
        contract_errors.append({"code": "functional_runner_process_leak",
                                "processes": leaked_processes})

by_id = {item.get("test_id"): item for item in actual if item.get("test_id") is not None}
results = []
for frozen in expected:
    test_id = frozen["test_id"]
    test = by_id.get(test_id)
    if test is None:
        results.append({"test_id": test_id, "class": frozen["class"], "status": "missing",
                        "failure_class": "missing_test_id", "missing": [], "forbidden": [],
                        "error": "required test ID is absent from the manifest"})
        continue
    if test.get("class") != frozen["class"]:
        contract_errors.append({"code": "test_class_mismatch", "test_id": test_id,
                                "expected": frozen["class"], "actual": test.get("class")})
        results.append({"test_id": test_id, "class": frozen["class"], "status": "error",
                        "failure_class": "test_class_mismatch", "missing": [], "forbidden": [],
                        "error": "test class differs from the frozen inventory"})
        continue
    if test.get("enabled", True) is not True:
        results.append({"test_id": test_id, "class": frozen["class"], "status": "skip",
                        "failure_class": "required_test_disabled", "missing": [], "forbidden": [],
                        "error": "required test was disabled and did not execute"})
        continue
    if test.get("kind") == "functional_result":
        observed = functional_results.get(test_id)
        status = observed.get("status") if observed else "error"
        if status not in {"pass", "fail", "skip", "error"}:
            status = "error"
        results.append({"test_id": test_id, "class": frozen["class"], "status": status,
                        "failure_class": None if status == "pass" else (
                            "functional_assertion_mismatch" if status == "fail" else
                            "functional_test_skipped" if status == "skip" else "functional_runner_error"),
                        "missing": [], "forbidden": [],
                        "error": None if status in {"pass", "fail", "skip"} else functional_error})
        continue
    relative = Path(test["path"])
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        results.append({"test_id": test_id, "class": frozen["class"], "status": "error",
                        "failure_class": "unsafe_test_path", "missing": [], "forbidden": [],
                        "error": "test path escapes the frozen /testbed source root"})
        continue
    target = app_root / relative
    try:
        resolved_root = app_root.resolve(strict=True)
        cursor = resolved_root
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ValueError("test path contains a symlink")
        resolved_target = target.resolve(strict=True)
        if not resolved_target.is_relative_to(resolved_root):
            raise ValueError("test path resolves outside /testbed")
        content = resolved_target.read_text().replace("\\r\\n", "\\n")
        missing = [i for i, value in enumerate(test["contains_all"]) if value not in content]
        forbidden = [i for i, value in enumerate(test.get("contains_none", [])) if value in content]
        passed = not missing and not forbidden
        error, failure_class = None, None if passed else "assertion_mismatch"
    except FileNotFoundError as exc:
        passed, missing, forbidden = False, [], []
        error, failure_class = f"{type(exc).__name__}: {exc}", "missing_source_file"
    except Exception as exc:
        passed, missing, forbidden = False, [], []
        error, failure_class = f"{type(exc).__name__}: {exc}", "test_execution_error"
    status = "pass" if passed else ("error" if failure_class in {"missing_source_file", "test_execution_error"} else "fail")
    results.append({"test_id": test_id, "class": frozen["class"], "status": status,
                    "failure_class": failure_class, "missing": missing, "forbidden": forbidden,
                    "error": error})
for test_id in actual_ids:
    if test_id not in expected_ids:
        test = by_id[test_id]
        results.append({"test_id": test_id, "class": test.get("class"), "status": "error",
                        "failure_class": "unexpected_test_id", "missing": [], "forbidden": [],
                        "error": "test ID is not present in the frozen inventory"})

counts = {name: sum(item["status"] == name for item in results)
          for name in ("pass", "fail", "skip", "missing", "error")}
reward = 1 if (not contract_errors and len(results) == len(expected)
               and counts["pass"] == len(expected)) else 0
record = {"schema_version": "harbor-source-verifier-v2", "reward": reward, "results": results,
          "summary": {"expected": len(expected), **counts}, "contract_errors": contract_errors,
          "scope": "frozen executable source assertions; independent human semantic calibration remains external"}
(logs / "test_results.json").write_text(json.dumps(record, indent=2) + "\\n")
(logs / "reward.txt").write_text(f"{reward}\\n")
'''
    verify_path = output / "tests/sweb_grade.py"
    verify_path.write_text(verifier)
    integrity = '''#!/usr/bin/env python3
import hashlib
import json
import stat
import sys
from pathlib import Path

mismatches = []
manifest_path = Path(sys.argv[1])
expected_manifest_sha = sys.argv[2]
try:
    actual_manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    if actual_manifest_sha != expected_manifest_sha:
        mismatches.append({"path": str(manifest_path),
                           "expected_sha256": expected_manifest_sha,
                           "actual_sha256": actual_manifest_sha,
                           "error": "integrity manifest hash mismatch"})
    expected = json.loads(manifest_path.read_text()).get("files", {})
    if not isinstance(expected, dict):
        raise ValueError("integrity manifest files must be an object")
except Exception as exc:
    expected = {}
    mismatches.append({"path": str(manifest_path),
                       "expected_sha256": expected_manifest_sha,
                       "actual_sha256": None,
                       "error": f"{type(exc).__name__}: {exc}"})
for path, expected_sha in expected.items():
    try:
        target = Path(path)
        mode = target.lstat().st_mode
        if target.is_symlink() or not stat.S_ISREG(mode):
            raise ValueError("protected test material is not a regular file")
        actual_sha = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual_sha != expected_sha:
            mismatches.append({"path": path, "expected_sha256": expected_sha,
                               "actual_sha256": actual_sha, "error": None})
    except Exception as exc:
        mismatches.append({"path": path, "expected_sha256": expected_sha,
                           "actual_sha256": None,
                           "error": f"{type(exc).__name__}: {exc}"})
if mismatches:
    logs = Path("/logs/verifier"); logs.mkdir(parents=True, exist_ok=True)
    record = {"schema_version": "harbor-source-verifier-v2", "reward": 0, "results": [],
              "summary": {"expected": 0, "pass": 0, "fail": 0, "skip": 0,
                          "missing": 0, "error": len(mismatches)},
              "contract_errors": [{"code": "frozen_test_tamper", "mismatches": mismatches}],
              "scope": "frozen test integrity check failed before execution"}
    (logs / "test_results.json").write_text(json.dumps(record, indent=2) + "\\n")
    (logs / "reward.txt").write_text("0\\n")
    raise SystemExit(1)
'''
    integrity_path = output / "tests/integrity.py"
    integrity_path.write_text(integrity)
    _write_integrity_launcher(output, manifest_path, inventory_path, verify_path)


def export(dossier_path: Path, tests_path: Path, measurement_path: Path, repo: Path,
           instruction: Path, base_image: str, output: Path,
           functional_runner: Path | None = None,
           test_payload: Path | None = None) -> dict:
    lexical_output = output.absolute()
    if (not lexical_output.is_relative_to(TMP_ROOT.absolute())
            or output.is_symlink()):
        raise ValueError("export output must be a provisional task under tmp")
    final_output = output.resolve()
    assert_no_symlink_chain(final_output.parent)
    sidecar = _export_manifest_path(final_output)
    staging = final_output.with_name(f".{final_output.name}.staging")
    sidecar_staging = sidecar.with_name(f".{sidecar.name}.staging")
    transaction, commit = _publication_paths(final_output)
    _recover_incomplete_publication(
        final_output, sidecar, staging, sidecar_staging, transaction, commit)
    if final_output.exists():
        raise ValueError(f"output already exists: {final_output}")
    if sidecar.exists():
        raise ValueError(f"export manifest sidecar already exists: {sidecar}")
    if commit.exists() or transaction.exists():
        raise ValueError("export publication evidence already exists")
    if staging.is_symlink() or staging.is_file():
        staging.unlink()
    elif staging.exists():
        shutil.rmtree(staging)
    if sidecar_staging.is_symlink() or sidecar_staging.is_file():
        sidecar_staging.unlink()
    output = staging
    dossier, tests, measurement = _validate_inputs(
        dossier_path, tests_path, measurement_path, repo, instruction, functional_runner
    )
    payload_inventory: list[dict] = []
    if test_payload is not None:
        if test_payload.is_symlink():
            raise ValueError("hidden test payload must be a real directory")
        test_payload = test_payload.resolve()
        if functional_runner is None:
            raise ValueError("hidden test payload requires a functional runner")
        if not test_payload.is_dir():
            raise ValueError("hidden test payload must be a real directory")
        if not tests.get("execution") or not any(
            item.get("kind") == "functional_result" for item in tests["tests"]
        ):
            raise ValueError("hidden test payload requires functional manifest tests")
        payload_inventory = _payload_inventory(test_payload)
    if functional_runner is not None:
        if functional_runner.is_symlink() or not functional_runner.is_file():
            raise ValueError("functional runner must be a real file")
        runner_sha256 = digest(functional_runner)
    else:
        runner_sha256 = None
    for directory in ("environment/assets", "solution", "tests"):
        (output / directory).mkdir(parents=True, exist_ok=True)

    # Snapshot curator-provided executable material before Docker inspection or
    # any other slow work. All later hashes and copies use this frozen snapshot.
    if functional_runner is not None:
        runner_target = output / "tests/functional_runner.py"
        shutil.copyfile(functional_runner, runner_target)
        if digest(runner_target) != runner_sha256:
            runner_target.unlink()
            raise ValueError("functional runner changed while being copied")
    if test_payload is not None:
        _snapshot_payload(test_payload, output / "tests/payload", payload_inventory)

    # The written-test interface requires an explicit test.patch. Payload paths
    # are interpreted as repository-relative overlays. Keep the payload as an
    # integrity-bound verifier resource as well, because functional runners may
    # need fixtures directly from /tests/payload.
    test_patch = ""
    if test_payload is not None:
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as patch_tmp:
            patch_repo = Path(patch_tmp)
            _run("git", "init", "-q", str(patch_repo))
            _run("git", "-C", str(patch_repo), "config", "user.name", "Benchmark Test Export")
            _run("git", "-C", str(patch_repo), "config", "user.email", "benchmark@invalid.local")
            for item in payload_inventory:
                relative = _safe_relative(item["path"])
                baseline = repo.resolve() / relative
                if baseline.is_file() and not baseline.is_symlink():
                    target = patch_repo / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(baseline, target)
            _run("git", "-C", str(patch_repo), "add", "-A")
            _run("git", "-C", str(patch_repo), "commit", "--allow-empty", "-qm", "baseline tests")
            for item in payload_inventory:
                relative = _safe_relative(item["path"])
                target = patch_repo / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(test_payload / relative, target)
            _run("git", "-C", str(patch_repo), "add", "-N", "--", ".")
            test_patch = _run("git", "-C", str(patch_repo), "diff", "--binary", "--", ".")

    base = _inspect_base_image(base_image)
    baseline_tree = _run("git", "-C", str(repo.resolve()), "rev-parse", "HEAD^{tree}")
    if base["app_tree"] != baseline_tree:
        raise ValueError(
            f"base image source tree differs from frozen baseline: {base['app_tree']} != {baseline_tree}"
        )
    base["source_baseline_tree"] = baseline_tree

    assets = dossier["leakage_policy"]["safe_agent_assets"]
    if not assets:
        raise ValueError("multimodal task has no safe agent assets")
    copied_assets = []
    for asset in assets:
        source = Path(asset["local_path"]).resolve()
        if (asset["status"] != "available" or digest(source) != asset["sha256"]
                or asset["asset_id"] != asset["sha256"]):
            raise ValueError("safe visual asset failed identity validation")
        safe_sources = set(dossier["leakage_policy"]["safe_agent_source_ids"])
        issue_source = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*:(?:body|title)$")
        if (not asset["source_ids"] or any(not issue_source.fullmatch(item) for item in asset["source_ids"])
                or not set(asset["source_ids"]) <= safe_sources):
            raise ValueError("non-Issue visual source reached agent export")
        name = f'{asset["asset_id"]}.png'
        shutil.copyfile(source, output / "environment/assets" / name)
        (output / "tests/agent-assets").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, output / "tests/agent-assets" / name)
        copied_assets.append({"name": name, "sha256": asset["sha256"], "source_ids": asset["source_ids"]})

    image_id = base["image_id"]
    frozen_ref = f'visual-harbor-base:{image_id.removeprefix("sha256:")}'
    _run("docker", "tag", image_id, frozen_ref)
    (output / "environment/base_image.json").write_text(json.dumps({
        "schema_version": "harbor-base-image-binding-v1",
        **base,
        "build_reference": frozen_ref,
        "source_baseline_sha": dossier["git"]["baseline_sha"],
    }, indent=2) + "\n")
    (output / "environment/Dockerfile").write_text(
        f"FROM {frozen_ref}\n"
        "RUN mv /app /testbed \\\n"
        "    && rm -rf /testbed/.git && git init /testbed \\\n"
        "    && git -C /testbed config user.name 'Benchmark Baseline' \\\n"
        "    && git -C /testbed config user.email 'benchmark@invalid.local' \\\n"
        "    && git -C /testbed config gc.auto 0 \\\n"
        "    && git -C /testbed add -A \\\n"
        "    && GIT_AUTHOR_DATE='2000-01-01T00:00:00Z' GIT_COMMITTER_DATE='2000-01-01T00:00:00Z' \\\n"
        "       git -C /testbed commit -q -m 'benchmark baseline' \\\n"
        "    && git -C /testbed rev-parse HEAD > /opt/benchmark-baseline-sha \\\n"
        "    && chmod 0444 /opt/benchmark-baseline-sha \\\n"
        "    && git -C /testbed reflog expire --expire=now --all \\\n"
        "    && git -C /testbed gc --prune=now\n"
        "RUN groupadd --gid 10001 benchmark && useradd --uid 10001 --gid 10001 --create-home benchmark \\\n"
        "    && mkdir -p /tests /logs && chmod 0755 /tests \\\n"
        "    && chown -R 10001:10001 /testbed /logs /home/benchmark\n"
        "COPY --chown=root:root collect-agent-patch /usr/local/bin/collect-agent-patch\n"
        "COPY --chown=root:root terminate-agent-processes /usr/local/bin/terminate-agent-processes\n"
        "RUN chmod 0555 /usr/local/bin/collect-agent-patch /usr/local/bin/terminate-agent-processes\n"
        "WORKDIR /testbed\nCOPY --chown=10001:10001 assets /testbed/assets\nUSER 10001:10001\n"
    )
    (output / "environment/collect-agent-patch").write_text(
        "#!/bin/bash\nset -euo pipefail\n"
        "umask 077\n"
        "rm -rf /opt/benchmark-transport\n"
        "install -d -o root -g root -m 0700 /opt/benchmark-transport\n"
        "scratch=\n"
        "finish() {\n"
        "  status=$?; trap - EXIT\n"
        "  rm -f /opt/benchmark-transport/status\n"
        "  if [ \"$status\" -eq 0 ]; then\n"
        "    printf 'ok\\n' > /opt/benchmark-transport/status\n"
        "  else\n"
        "    rm -f /opt/benchmark-transport/agent.patch\n"
        "    printf 'failed:%s\\n' \"$status\" > /opt/benchmark-transport/status\n"
        "  fi\n"
        "  chmod 0444 /opt/benchmark-transport/status\n"
        "  chmod 0555 /opt/benchmark-transport\n"
        "  [ -z \"$scratch\" ] || rm -rf \"$scratch\"\n"
        "  exit \"$status\"\n"
        "}\n"
        "trap finish EXIT\n"
        "cd /testbed\n"
        "for proc in /proc/[0-9]*; do\n"
        "  pid=${proc##*/}\n"
        "  [ \"$pid\" = 1 ] && continue\n"
        "  uid=$(awk '/^Uid:/{print $2}' \"$proc/status\" 2>/dev/null || true)\n"
        "  state=$(awk '/^State:/{print $2}' \"$proc/status\" 2>/dev/null || true)\n"
        "  [[ \"$uid\" != 10001 || \"$state\" = Z ]] || exit 70\n"
        "done\n"
        "scratch=$(mktemp -d)\n"
        "baseline=$(cat /opt/benchmark-baseline-sha)\n"
        "file_count=0\n"
        "while IFS= read -r -d '' path; do\n"
        "  file_count=$((file_count + 1))\n"
        "  [ \"$file_count\" -le 1024 ] || exit 72\n"
        "done < <(/usr/bin/git diff --name-only -z \"$baseline\" -- . ':(exclude)assets/**')\n"
        "/usr/bin/git diff --binary --no-ext-diff --no-textconv \"$baseline\" -- . "
        "':(exclude)assets/**' "
        "> \"$scratch/agent.patch\"\n"
        "while IFS= read -r -d '' path; do\n"
        "  [[ \"$path\" = assets/* ]] && continue\n"
        "  file_count=$((file_count + 1))\n"
        "  [ \"$file_count\" -le 1024 ] || exit 72\n"
        "  [[ ! -L \"$path\" && -f \"$path\" ]] || exit 75\n"
        "  [ \"$(stat -c %s -- \"$path\")\" -le 67108864 ] || exit 73\n"
        "  /usr/bin/git diff --binary --no-ext-diff --no-textconv --no-index "
        "/dev/null \"$path\" >> \"$scratch/agent.patch\" || [ \"$?\" -eq 1 ]\n"
        "done < <(/usr/bin/git ls-files --others --exclude-standard -z)\n"
        "[ \"$(stat -c %s -- \"$scratch/agent.patch\")\" -le 268435456 ] || exit 74\n"
        "install -o root -g root -m 0444 \"$scratch/agent.patch\" "
        "/opt/benchmark-transport/agent.patch\n"
    )
    (output / "environment/collect-agent-patch").chmod(0o755)
    (output / "environment/terminate-agent-processes").write_text(
        "#!/bin/bash\nset -euo pipefail\n"
        "self=$$; parent=$PPID\n"
        "for signal in TERM KILL; do\n"
        "  for pass in 1 2 3; do\n"
        "    found=0\n"
        "    for proc in /proc/[0-9]*; do\n"
        "      pid=${proc##*/}\n"
        "      [[ \"$pid\" = 1 || \"$pid\" = \"$self\" || \"$pid\" = \"$parent\" ]] && continue\n"
        "      uid=$(awk '/^Uid:/{print $2}' \"$proc/status\" 2>/dev/null || true)\n"
        "      state=$(awk '/^State:/{print $2}' \"$proc/status\" 2>/dev/null || true)\n"
        "      if [[ \"$uid\" = 10001 && \"$state\" != Z ]]; then\n"
        "        found=1; kill -\"$signal\" \"$pid\" 2>/dev/null || true\n"
        "      fi\n"
        "    done\n"
        "    [ \"$found\" -eq 0 ] && exit 0\n"
        "    sleep 0.2\n"
        "  done\n"
        "done\n"
        "exit 70\n"
    )
    (output / "environment/terminate-agent-processes").chmod(0o755)
    (output / "environment/docker-compose.yaml").write_text(
        "services:\n  main:\n    cap_drop: [ALL]\n"
        "    user: \"10001:10001\"\n    security_opt: [no-new-privileges:true]\n"
        "    pids_limit: 512\n    shm_size: 1gb\n"
    )
    (output / "task.toml").write_text(
        'schema_version = "1.2"\nartifacts = ["/opt/benchmark-transport"]\n\n[task]\n'
        f'name = "swe-bench-multimodal/{dossier["candidate_id"]}"\n'
        f'description = "Resolve {dossier["candidate_id"]} with visual context in /testbed."\n'
        'authors = [{ name = "benchmark-construction" }]\n'
        'keywords = ["swe-bench", "swe-bench-multimodal", "javascript", "agentic"]\n\n'
        '[metadata]\nbenchmark = "SWE-bench Multimodal"\n'
        f'repo = "{dossier["repository"]}"\ninstance_id = "{dossier["candidate_id"]}"\n'
        f'pr_number = {dossier["pr_number"]}\n\n'
        '[environment]\nbuild_timeout_sec = 1800.0\ncpus = 6\nmemory_mb = 12288\n'
        'storage_mb = 20480\nallow_internet = false\n\n'
        '[agent]\ntimeout_sec = 7200.0\noverride_setup_timeout_sec = 1800.0\n\n'
        '[verifier]\ntimeout_sec = 3600.0\nenvironment_mode = "separate"\nuser = 0\n\n'
        '[[verifier.collect]]\ncommand = "/usr/local/bin/terminate-agent-processes"\n'
        'service = "main"\nuser = 10001\ntimeout_sec = 60.0\n\n'
        '[[verifier.collect]]\ncommand = "/usr/local/bin/collect-agent-patch"\n'
        'service = "main"\nuser = 0\ntimeout_sec = 300.0\n'
    )
    instruction_text = instruction.read_text().replace("/visual_context/", "/testbed/assets/")
    (output / "instruction.md").write_text(instruction_text)

    patch = _git_diff_preserve(
        repo,
        dossier["git"]["baseline_sha"],
        dossier["git"]["reference_sha"],
        [item["filename"] for item in dossier["changed_files"]],
    )
    if not patch:
        raise ValueError("reference patch is empty")
    (output / "solution/gold.patch").write_text(patch)
    (output / "solution/solve.sh").write_text(
        "#!/bin/bash\nset -eu\ncd /testbed\ngit apply /solution/gold.patch\n"
    )
    (output / "solution/solve.sh").chmod(0o755)

    # Harbor uploads tests after the agent turn; these files are not part of the
    # agent-visible problem statement.
    (output / "tests/test_manifest.json").write_text(json.dumps(tests, ensure_ascii=False, indent=2) + "\n")
    (output / "tests/config.json").write_text(json.dumps({
        "repo": dossier["repository"],
        "instance_id": dossier["candidate_id"],
        "base_commit": dossier["git"]["baseline_sha"],
        "FAIL_TO_PASS": [item["test_id"] for item in tests["tests"] if item["class"] == "F2P"],
        "PASS_TO_PASS": [item["test_id"] for item in tests["tests"] if item["class"] == "P2P"],
        "log_parser": "report_functional_json_v1",
    }, ensure_ascii=False, indent=2) + "\n")
    (output / "tests/test.patch").write_text(test_patch + ("\n" if test_patch else ""))
    # Harbor 0.22 builds a separate verifier from tests/, and skips runtime
    # test upload in that mode. Bake the hidden tests into a fresh baseline
    # image while keeping them out of the agent image.
    (output / "tests/Dockerfile").write_text(
        f"FROM {frozen_ref}\n"
        "RUN mv /app /testbed \\\n"
        "    && rm -rf /testbed/.git && git init /testbed \\\n"
        "    && git -C /testbed config user.name 'Benchmark Baseline' \\\n"
        "    && git -C /testbed config user.email 'benchmark@invalid.local' \\\n"
        "    && git -C /testbed config gc.auto 0 \\\n"
        "    && git -C /testbed add -A \\\n"
        "    && GIT_AUTHOR_DATE='2000-01-01T00:00:00Z' GIT_COMMITTER_DATE='2000-01-01T00:00:00Z' \\\n"
        "       git -C /testbed commit -q -m 'benchmark baseline' \\\n"
        "    && git -C /testbed reflog expire --expire=now --all \\\n"
        "    && git -C /testbed gc --prune=now \\\n"
        "    && groupadd --gid 10002 verifier-runner \\\n"
        "    && useradd --uid 10002 --gid 10002 --create-home verifier-runner \\\n"
        "    && ln -s /testbed /app \\\n"
        "    && mkdir -p /tests /logs/verifier\n"
        "COPY agent-assets /testbed/assets\n"
        "COPY . /tests\n"
        "RUN find /tests -type d -exec chmod 0700 {} + \\\n"
        "    && find /tests -type f -exec chmod 0400 {} + \\\n"
        "    && chmod 0555 /tests/test.sh\n"
        "WORKDIR /testbed\nUSER 0:0\n"
    )
    forbidden_git_commits = sorted({value for value in dossier["git"].values()
                                    if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value)})
    _write_verifier(output, tests, forbidden_git_commits)

    files = {p.relative_to(output).as_posix(): digest(p)
             for p in sorted(output.rglob("*")) if p.is_file()}
    material = files
    canonical_entries = [{"path": name, "sha256": value} for name, value in sorted(material.items())]
    canonical = hashlib.sha256(json.dumps(canonical_entries, separators=(",", ":")).encode()).hexdigest()
    record = {
        "schema_version": "visual-harbor-export-v1",
        "status": "exported_pending_harbor_controls",
        "candidate_id": dossier["candidate_id"],
        "base_image_id": image_id,
        "base_image_ref": frozen_ref,
        "base_image_app_head": base["app_head"],
        "base_image_app_tree": base["app_tree"],
        "task_material_sha256": canonical,
        "source_bindings": {
            "dossier_sha256": digest(dossier_path), "test_manifest_sha256": digest(tests_path),
            "measurement_sha256": digest(measurement_path), "instruction_sha256": digest(instruction),
            **({"functional_runner_sha256": runner_sha256}
               if runner_sha256 is not None else {}),
            **({
                "test_payload_inventory_sha256": hashlib.sha256(
                    json.dumps(payload_inventory, separators=(",", ":")).encode()
                ).hexdigest(),
                "test_payload_files": payload_inventory,
            } if test_payload is not None else {}),
        },
        "calibration": {
            "visual_necessity_human": dossier["visual_admission"]["human_calibration_state"],
            "f2p_p2p_semantics_human": dossier["test_calibration"]["human_semantic_calibration_state"],
            "auto_admission": dossier["visual_admission"]["decision"],
        },
        "agent_assets": copied_assets,
        "f2p_test_ids": [item["test_id"] for item in tests["tests"] if item["class"] == "F2P"],
        "p2p_test_ids": [item["test_id"] for item in tests["tests"] if item["class"] == "P2P"],
        "files": material,
        "leakage_boundary": "Issue text/images only under /testbed/assets; source Git history/remotes, PR prose, diff, reference and curator assets withheld from agent",
        "test_scope": (
            "hidden executable browser/rendering tests over a frozen verifier-only payload"
            if payload_inventory else
            "compiled SCSS plus real Chromium computed-style reward over the frozen fixture"
            if tests.get("execution") else
            "source-semantic reward for control-only assertions"
        ),
    }
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar_staging.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    transaction_record = {
        "schema_version": "visual-harbor-export-transaction-v1",
        "task_material_sha256": canonical,
        "sidecar_sha256": digest(sidecar_staging),
    }
    _atomic_json(transaction, transaction_record)
    sidecar_published = False
    task_published = False
    try:
        sidecar_staging.rename(sidecar)
        sidecar_published = True
        staging.rename(final_output)
        task_published = True
        _atomic_json(commit, {
            "schema_version": "visual-harbor-export-commit-v1",
            "task_material_sha256": canonical,
            "sidecar_sha256": digest(sidecar),
            "transaction_sha256": digest(transaction),
        })
        transaction.unlink()
    except Exception:
        commit.unlink(missing_ok=True)
        if task_published and final_output.exists() and not staging.exists():
            final_output.rename(staging)
        if sidecar_published:
            sidecar.unlink(missing_ok=True)
        sidecar_staging.unlink(missing_ok=True)
        if staging.exists():
            shutil.rmtree(staging)
        transaction.unlink(missing_ok=True)
        raise
    return record
