"""Provisionally measure V4 curator tests on exact Base and reference trees.

This runner never promotes a task and never treats a prediction as F2P/P2P.
Only the same materialized bundle and command, executed on both isolated arms,
can produce provisional transition labels.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid

from report_pipeline.atomic import write_json
from report_pipeline.v4_test_campaign import REPOSITORY_NAMES, _validate_result


TECHNICAL_PATTERNS = re.compile(
    r"command not found|cannot find module|module not found|no such file or directory|"
    r"failed to collect|test suite failed to run|could not resolve|dependency.*missing|"
    r"npm err!|yarn error|pnpm.*err|cannot start chrome|can not find the binary|"
    r"please set env variable chrome_bin|eacces: permission denied",
    re.IGNORECASE,
)
SKIP_PATTERNS = re.compile(r"\b(skip(?:ped)?|pending|todo|ignored)\b", re.IGNORECASE)
FAIL_PATTERNS = re.compile(r"(?:\b(?:fail(?:ed|ure)?|error)\b|not ok|[✕×])", re.IGNORECASE)
PASS_PATTERNS = re.compile(r"(?:\b(?:pass(?:ed)?|ok)\b|[✓✔])", re.IGNORECASE)
SUITE_TOTAL_PATTERN = re.compile(
    r"TOTAL:\s*(?P<failed>\d+)\s+FAILED,\s*(?P<passed>\d+)\s+SUCCESS",
    re.IGNORECASE,
)
ALLOWED_EXECUTABLES = {
    "npm", "npx", "yarn", "pnpm", "node", "python", "python3", "pytest",
    "vitest", "jest", "mocha", "bash", "sh", "make", "cargo", "go",
    "mvn", "gradle", "gradlew", "test",
}
BACKENDS = {"clone", "docker"}


class PreparationError(RuntimeError):
    """A case could not reach test execution for technical/evidence reasons."""


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _binding(path: Path) -> dict:
    return {"path": str(path.resolve()), "sha256": _sha(path),
            "size_bytes": path.stat().st_size}


def _safe_relative(value: str, *, allow_dot: bool = False) -> bool:
    path = PurePosixPath(value)
    if allow_dot and value in {"", "."}:
        return True
    return bool(value and not path.is_absolute() and ".." not in path.parts)


def _validate_command(command: str) -> None:
    if not command.strip() or any(token in command for token in ("\n", "\r", ";", "|", "`", "$(", ">", "<")):
        raise ValueError("unsafe_or_empty_test_command")
    for segment in command.split("&&"):
        tokens = shlex.split(segment)
        if not tokens:
            raise ValueError("empty_test_command_segment")
        executable = tokens[0]
        name = Path(executable).name
        if name not in ALLOWED_EXECUTABLES and not (
                executable.startswith("./") and _safe_relative(executable[2:])):
            raise ValueError(f"unapproved_test_executable:{name}")


def _run(command: list[str], *, cwd: Path, timeout: int,
         input_text: str | None = None, env: dict | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, input=input_text, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=timeout, check=False, env=env)


def _write_process(output: Path, index: str,
                   completed: subprocess.CompletedProcess[str]) -> dict:
    stdout = output / f"{index}_stdout.txt"
    stderr = output / f"{index}_stderr.txt"
    stdout.write_text(completed.stdout)
    stderr.write_text(completed.stderr)
    return {"return_code": completed.returncode, "stdout": _binding(stdout),
            "stderr": _binding(stderr)}


def _clone_arm(repository: Path, target: Path, commit: str, output: Path,
               prefix: str, timeout: int) -> dict:
    clone = _run(["git", "clone", "--shared", "--no-checkout", str(repository),
                  str(target)], cwd=repository.parent, timeout=timeout)
    records = {"clone": _write_process(output, f"20_19_02_{prefix}_clone", clone)}
    if clone.returncode:
        raise PreparationError(f"{prefix}_clone_failed")
    checkout = _run(["git", "checkout", "--detach", "--force", commit],
                    cwd=target, timeout=timeout)
    records["checkout"] = _write_process(
        output, f"20_19_02_{prefix}_checkout", checkout)
    if checkout.returncode:
        raise PreparationError(f"{prefix}_checkout_failed")
    head = _run(["git", "rev-parse", "HEAD"], cwd=target, timeout=timeout)
    if head.returncode or head.stdout.strip() != commit:
        raise PreparationError(f"{prefix}_exact_commit_check_failed")
    return records


def _apply_reference(root: Path, patch: str, output: Path, timeout: int) -> dict:
    if not patch.strip():
        raise PreparationError("reference_diff_missing")
    applied = _run(["git", "apply", "--whitespace=nowarn", "-"], cwd=root,
                   timeout=timeout, input_text=patch)
    record = _write_process(output, "20_19_02_gold_reference_apply", applied)
    if applied.returncode:
        raise PreparationError("gold_reference_diff_apply_failed")
    return record


def _materialize_bundle(root: Path, bundle: dict) -> list[dict]:
    result = []
    root = root.resolve()
    for item in bundle["files"]:
        path = item["path"]
        if not _safe_relative(path):
            raise PreparationError("unsafe_bundle_file_path")
        target = root / path
        if not target.resolve(strict=False).is_relative_to(root):
            raise PreparationError("bundle_file_escapes_checkout")
        if any(parent.is_symlink() for parent in [target, *target.parents]
               if parent != root and parent.is_relative_to(root)):
            raise PreparationError("bundle_file_uses_symlink_path")
        operation = item["operation"]
        if operation == "add" and target.exists():
            raise PreparationError("bundle_add_target_exists")
        if operation == "modify" and not target.is_file():
            raise PreparationError("bundle_modify_target_missing")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(item["content"])
        result.append({"path": path, "operation": operation, "sha256": _sha(target)})
    return result


def _line_evidence(test_id: str, combined: str) -> list[str]:
    return [line[:1000] for line in combined.splitlines() if test_id in line][:8]


def _test_status(test_id: str, combined: str, return_code: int | None,
                 execution_status: str) -> dict:
    evidence = _line_evidence(test_id, combined)
    if execution_status in {"timeout", "technical_error"}:
        status = "error"
    elif not evidence:
        status = "missing"
    elif any(SKIP_PATTERNS.search(line) for line in evidence):
        status = "skip"
    elif any(FAIL_PATTERNS.search(line) for line in evidence):
        status = "fail"
    elif any(PASS_PATTERNS.search(line) for line in evidence):
        status = "pass"
    else:
        status = "pass" if return_code == 0 else "fail"
    return {"test_id": test_id, "status": status, "evidence_lines": evidence}


def _suite_total(combined: str) -> dict | None:
    matches = list(SUITE_TOTAL_PATTERN.finditer(combined))
    if not matches:
        return None
    match = matches[-1]
    return {"failed": int(match.group("failed")), "passed": int(match.group("passed")),
            "line": match.group(0)}


def _reconcile_hidden_gold_pass(base_test: dict, gold_test: dict,
                                gold_run: dict) -> None:
    """Resolve only fail->hidden-pass when Base proves the test was collected.

    Some progress reporters print failing titles but suppress passing titles.
    The generated file and command are identical across arms, so a Base title
    proves collection.  We still require a completed, non-empty Gold suite and
    never use this rule to infer P2P when neither arm prints the test identity.
    """
    suite = gold_run.get("suite_total")
    if (base_test["status"] == "fail" and base_test.get("evidence_lines")
            and gold_test["status"] == "missing"
            and gold_run.get("execution_status") == "completed"
            and suite and suite["passed"] > 0):
        gold_test.update(
            status="pass",
            evidence_lines=[suite["line"]],
            evidence_kind="inferred_from_identical_bundle_and_base_collection",
        )


def _execute_arm(root: Path, side: str, bundle: dict, output: Path,
                 timeout: int) -> dict:
    command = bundle["test_command"]
    _validate_command(command)
    working = bundle["working_directory"] or "."
    if not _safe_relative(working, allow_dot=True):
        raise PreparationError("unsafe_working_directory")
    cwd = (root / working).resolve()
    if not cwd.is_dir() or not cwd.is_relative_to(root.resolve()):
        raise PreparationError("working_directory_missing_or_escaped")
    started = datetime.now(timezone.utc).isoformat()
    begin = time.monotonic()
    env = dict(os.environ)
    env.update({"CI": "1", "NO_COLOR": "1", "FORCE_COLOR": "0"})
    return_code = None
    try:
        completed = _run(["/bin/sh", "-lc", command], cwd=cwd,
                         timeout=timeout, env=env)
        return_code = completed.returncode
        stdout_text, stderr_text = completed.stdout, completed.stderr
        combined = stdout_text + "\n" + stderr_text
        ids_seen = any(test_id in combined for test_id in bundle["stable_test_ids"])
        if completed.returncode != 0 and not ids_seen and TECHNICAL_PATTERNS.search(combined):
            execution_status = "technical_error"
            failure_class = "test_infrastructure_error"
        else:
            execution_status = "completed"
            failure_class = None
    except subprocess.TimeoutExpired as exc:
        stdout_text = exc.stdout or ""
        stderr_text = exc.stderr or ""
        if isinstance(stdout_text, bytes):
            stdout_text = stdout_text.decode(errors="replace")
        if isinstance(stderr_text, bytes):
            stderr_text = stderr_text.decode(errors="replace")
        combined = stdout_text + "\n" + stderr_text
        execution_status = "timeout"
        failure_class = "timeout"
    except OSError as exc:
        stdout_text, stderr_text = "", f"{type(exc).__name__}: {exc}"
        combined = stderr_text
        execution_status = "technical_error"
        failure_class = "process_start_error"
    stdout = output / f"20_19_03_{side}_stdout.txt"
    stderr = output / f"20_19_03_{side}_stderr.txt"
    stdout.write_text(stdout_text)
    stderr.write_text(stderr_text)
    tests = [_test_status(test_id, combined, return_code, execution_status)
             for test_id in bundle["stable_test_ids"]]
    if execution_status == "completed" and any(item["status"] == "missing" for item in tests):
        evidence_status = "missing_test_ids"
    elif execution_status == "completed" and any(item["status"] == "fail" for item in tests):
        evidence_status = "semantic_test_failure"
    elif execution_status == "completed":
        evidence_status = "tests_observed"
    else:
        evidence_status = execution_status
    record = {
        "schema_version": "v4-provisional-arm-run-v1", "side": side,
        "started_at": started, "elapsed_seconds": round(time.monotonic() - begin, 3),
        "working_directory": working, "test_command": command,
        "execution_status": execution_status, "evidence_status": evidence_status,
        "failure_class": failure_class, "return_code": return_code,
        "suite_total": _suite_total(combined),
        "stdout": _binding(stdout), "stderr": _binding(stderr), "tests": tests,
    }
    path = output / f"20_19_04_{side}_run.json"
    write_json(path, record)
    return {"record": record, "binding": _binding(path)}


def _normalise_case_id(case_id: str) -> str:
    """Match the image tag convention used by environment_builder."""
    return case_id.lower().replace("__", "-").replace("_", "-")


def _docker_image(image_prefix: str, case_id: str) -> str:
    return f"{image_prefix}:{_normalise_case_id(case_id)}"


def _inspect_docker_base(image: str, base_commit: str, output: Path,
                         timeout: int) -> dict:
    inspect = _run(["docker", "image", "inspect", image], cwd=output,
                   timeout=timeout)
    inspect_record = _write_process(output, "20_19_02_docker_inspect", inspect)
    if inspect.returncode:
        raise PreparationError("docker_base_image_inspect_failed")
    try:
        payload = json.loads(inspect.stdout)
        image_id = payload[0]["Id"]
    except (json.JSONDecodeError, IndexError, KeyError, TypeError) as exc:
        raise PreparationError("docker_base_image_inspect_invalid") from exc
    head = _run([
        "docker", "run", "--rm", "--network", "none", "--entrypoint", "git",
        image_id, "-C", "/app", "rev-parse", "HEAD",
    ], cwd=output, timeout=timeout)
    head_record = _write_process(output, "20_19_02_docker_head", head)
    if head.returncode or head.stdout.strip() != base_commit:
        raise PreparationError("docker_base_exact_commit_check_failed")
    return {"requested_image": image, "image_id": image_id,
            "inspect": inspect_record, "head": head_record,
            "verified_head": head.stdout.strip()}


def _write_docker_build_context(context: Path, image_id: str, bundle: dict,
                                reference_diff: str | None) -> list[dict]:
    context.mkdir(parents=True)
    lines = [
        f"FROM {image_id}",
        "RUN printf '%s\\n' '#!/bin/sh' 'exec /usr/bin/chromium --no-sandbox \"$@\"' "
        "> /usr/local/bin/chromium-no-sandbox && chmod 0755 /usr/local/bin/chromium-no-sandbox",
    ]
    if reference_diff is not None:
        patch = context / "reference.diff"
        patch.write_text(reference_diff)
        lines.extend([
            'COPY ["reference.diff", "/tmp/v4-reference.diff"]',
            "RUN git -C /app apply --whitespace=nowarn /tmp/v4-reference.diff "
            "&& rm /tmp/v4-reference.diff",
        ])
    files = context / "files"
    files.mkdir()
    records = []
    for index, item in enumerate(bundle["files"]):
        path = item["path"]
        if not _safe_relative(path):
            raise PreparationError("unsafe_bundle_file_path")
        source = files / f"{index:04d}"
        source.write_text(item["content"])
        destination = "/app/" + path
        quoted = shlex.quote(destination)
        if item["operation"] == "add":
            lines.append(f"RUN test ! -e {quoted}")
        elif item["operation"] == "modify":
            lines.append(f"RUN test -f {quoted}")
        else:
            raise PreparationError("unsupported_bundle_file_operation")
        lines.append("COPY " + json.dumps(
            [f"files/{index:04d}", destination], ensure_ascii=True))
        records.append({"path": path, "operation": item["operation"],
                        "sha256": _sha(source)})
    (context / "Dockerfile").write_text("\n".join(lines) + "\n")
    return records


def _execute_docker_arm(tag: str, side: str, bundle: dict, output: Path,
                        timeout: int) -> dict:
    working = bundle["working_directory"] or "."
    if not _safe_relative(working, allow_dot=True):
        raise PreparationError("unsafe_working_directory")
    container_working = "/app" if working == "." else f"/app/{working}"
    command = [
        "docker", "run", "--rm", "--network", "none",
        "--env", "CI=1", "--env", "NO_COLOR=1", "--env", "FORCE_COLOR=0",
        "--env", "CHROME_BIN=/usr/local/bin/chromium-no-sandbox",
        "--env", "PUPPETEER_EXECUTABLE_PATH=/usr/local/bin/chromium-no-sandbox",
        "--workdir", container_working, "--entrypoint", "/bin/sh", tag,
        "-lc", bundle["test_command"],
    ]
    # Reuse the status parser and raw-log contract by presenting a temporary
    # root whose working directory invokes the already argv-safe docker call.
    _validate_command(bundle["test_command"])
    started = datetime.now(timezone.utc).isoformat()
    begin = time.monotonic()
    return_code = None
    try:
        completed = _run(command, cwd=output, timeout=timeout)
        return_code = completed.returncode
        stdout_text, stderr_text = completed.stdout, completed.stderr
        combined = stdout_text + "\n" + stderr_text
        ids_seen = any(test_id in combined for test_id in bundle["stable_test_ids"])
        if completed.returncode != 0 and not ids_seen and TECHNICAL_PATTERNS.search(combined):
            execution_status, failure_class = "technical_error", "test_infrastructure_error"
        else:
            execution_status, failure_class = "completed", None
    except subprocess.TimeoutExpired as exc:
        stdout_text, stderr_text = exc.stdout or "", exc.stderr or ""
        if isinstance(stdout_text, bytes):
            stdout_text = stdout_text.decode(errors="replace")
        if isinstance(stderr_text, bytes):
            stderr_text = stderr_text.decode(errors="replace")
        combined = stdout_text + "\n" + stderr_text
        execution_status, failure_class = "timeout", "timeout"
    except OSError as exc:
        stdout_text, stderr_text = "", f"{type(exc).__name__}: {exc}"
        combined = stderr_text
        execution_status, failure_class = "technical_error", "process_start_error"
    stdout = output / f"20_19_03_{side}_stdout.txt"
    stderr = output / f"20_19_03_{side}_stderr.txt"
    stdout.write_text(stdout_text)
    stderr.write_text(stderr_text)
    tests = [_test_status(test_id, combined, return_code, execution_status)
             for test_id in bundle["stable_test_ids"]]
    if execution_status == "completed" and any(item["status"] == "missing" for item in tests):
        evidence_status = "missing_test_ids"
    elif execution_status == "completed" and any(item["status"] == "fail" for item in tests):
        evidence_status = "semantic_test_failure"
    elif execution_status == "completed":
        evidence_status = "tests_observed"
    else:
        evidence_status = execution_status
    record = {
        "schema_version": "v4-provisional-arm-run-v1", "side": side,
        "backend": "docker", "image": tag, "started_at": started,
        "elapsed_seconds": round(time.monotonic() - begin, 3),
        "working_directory": working, "test_command": bundle["test_command"],
        "docker_argv": command, "network": "none",
        "execution_status": execution_status, "evidence_status": evidence_status,
        "failure_class": failure_class, "return_code": return_code,
        "suite_total": _suite_total(combined),
        "stdout": _binding(stdout), "stderr": _binding(stderr), "tests": tests,
    }
    path = output / f"20_19_04_{side}_run.json"
    write_json(path, record)
    return {"record": record, "binding": _binding(path)}


def _measure_docker_arms(case_id: str, base_commit: str, reference_diff: str,
                         bundle: dict, image_prefix: str, temporary_root: Path,
                         output: Path, timeout: int, setup_timeout: int) -> tuple[dict, dict, dict]:
    base = _inspect_docker_base(
        _docker_image(image_prefix, case_id), base_commit, output, setup_timeout)
    suffix = uuid.uuid4().hex[:12]
    tags = {side: f"v4-provisional-measure:{_normalise_case_id(case_id)}-{side}-{suffix}"
            for side in ("source", "base", "gold")}
    setup = {"backend": "docker", "base_image": base, "temporary_tags": tags}
    caught = None
    base_run = gold_run = None
    try:
        tagged = _run(["docker", "image", "tag", base["image_id"], tags["source"]],
                      cwd=output, timeout=setup_timeout)
        setup["source_tag"] = _write_process(
            output, "20_19_02_docker_source_tag", tagged)
        if tagged.returncode:
            raise PreparationError("docker_source_tag_failed")
        manifests = {}
        for side in ("base", "gold"):
            context = temporary_root / f"docker-{side}"
            manifests[side] = _write_docker_build_context(
                context, tags["source"], bundle,
                reference_diff if side == "gold" else None)
            built = _run([
                "docker", "build", "--network", "none", "--progress=plain",
                "--tag", tags[side], str(context),
            ], cwd=output, timeout=setup_timeout)
            setup[f"{side}_build"] = _write_process(
                output, f"20_19_02_docker_{side}_build", built)
            if built.returncode:
                raise PreparationError(f"docker_{side}_build_failed")
        if manifests["base"] != manifests["gold"]:
            raise PreparationError("test_bundle_differs_between_arms")
        setup["materialized_test_files"] = manifests["base"]
        base_run = _execute_docker_arm(tags["base"], "base", bundle, output, timeout)
        gold_run = _execute_docker_arm(tags["gold"], "gold", bundle, output, timeout)
    except Exception as exc:
        caught = exc
    finally:
        cleanup_failed = []
        for side, tag in tags.items():
            cleaned = _run(["docker", "image", "rm", "--force", tag], cwd=output,
                           timeout=setup_timeout)
            setup[f"{side}_cleanup"] = _write_process(
                output, f"20_19_02_docker_{side}_cleanup", cleaned)
            if cleaned.returncode:
                cleanup_failed.append(side)
    if caught is not None:
        raise caught
    if cleanup_failed:
        raise PreparationError("docker_temporary_tag_cleanup_failed:" + ",".join(cleanup_failed))
    return setup, base_run, gold_run


def classify_transition(base: str, gold: str) -> str:
    if base == "fail" and gold == "pass":
        return "provisional_f2p"
    if base == "pass" and gold == "pass":
        return "provisional_p2p"
    if "error" in {base, gold}:
        return "error"
    if "missing" in {base, gold}:
        return "missing"
    if "skip" in {base, gold}:
        return "skip"
    if base == "fail" and gold == "fail":
        return "fail_to_fail"
    if base == "pass" and gold == "fail":
        return "pass_to_fail"
    return "unclassified"


def _case_inputs(campaign: Path, record: dict) -> tuple[Path, Path, dict, dict]:
    case_id = record["case_id"]
    directory = campaign / "20_17_02_model_runs" / case_id
    packet_path = directory / "20_17_01_packet.json"
    result_path = directory / "20_17_06_final.json"
    if not packet_path.is_file() or not result_path.is_file():
        raise PreparationError("campaign_case_artifacts_missing")
    packet = json.loads(packet_path.read_text())
    result = json.loads(result_path.read_text())
    if packet.get("task_id") != case_id:
        raise PreparationError("campaign_packet_identity_mismatch")
    return packet_path, result_path, packet, result


def _measure_case(campaign: Path, campaign_record: dict, repositories: Path | None,
                  output: Path, timeout: int, setup_timeout: int,
                  backend: str, image_prefix: str) -> dict:
    case_id = campaign_record["case_id"]
    case_output = output / "20_19_01_case_runs" / case_id
    case_output.mkdir(parents=True, exist_ok=True)
    record = {"case_id": case_id, "repository": campaign_record.get("repository"),
              "status": "technical_failure", "failure_ledger": "technical"}
    try:
        packet_path, result_path, packet, result = _case_inputs(campaign, campaign_record)
        record["inputs"] = {"packet": _binding(packet_path), "model_result": _binding(result_path)}
        repository_name = packet["repository"]
        local_name = REPOSITORY_NAMES.get(repository_name)
        if not local_name:
            raise PreparationError("repository_mapping_missing")
        repository = ((repositories / local_name).resolve() if repositories is not None
                      else Path("/").resolve())
        _validate_result(result, case_id, repository)
        bundle = result.get("test_bundle")
        if result["status"] != "test_bundle_proposed" or not bundle:
            record.update(status="not_measured", failure_ledger="construction",
                          reason=f"constructor_status:{result['status']}")
            write_json(case_output / "20_19_05_transitions.json", record)
            return record
        _validate_command(bundle["test_command"])
        if backend == "clone" and not (repository / ".git").exists():
            raise PreparationError("repository_checkout_missing")
        measurement_input = {
            "schema_version": "v4-provisional-measurement-input-v1",
            "task_id": case_id, "repository": repository_name,
            "base_commit": packet["base_commit"],
            "reference_head": packet.get("reference_head"),
            "reference_diff_sha256": hashlib.sha256(
                packet["reference_diff"].encode()).hexdigest(),
            "backend": backend,
            "requested_base_image": (_docker_image(image_prefix, case_id)
                                     if backend == "docker" else None),
            "bundle": bundle,
            "bundle_sha256": hashlib.sha256(
                json.dumps(bundle, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
            "boundary": "provisional only; identical test bundle is measured on Base and Base+reference diff",
        }
        input_path = case_output / "20_19_01_measurement_input.json"
        write_json(input_path, measurement_input)
        record["measurement_input"] = _binding(input_path)
        with tempfile.TemporaryDirectory(prefix="v4-measurement-") as temporary:
            temporary_root = Path(temporary)
            if backend == "docker":
                setup, base_run, gold_run = _measure_docker_arms(
                    case_id, packet["base_commit"], packet["reference_diff"], bundle,
                    image_prefix, temporary_root, case_output, timeout, setup_timeout)
            else:
                base_root, gold_root = temporary_root / "base", temporary_root / "gold"
                setup = {
                    "backend": "clone",
                    "base": _clone_arm(repository, base_root, packet["base_commit"],
                                       case_output, "base", setup_timeout),
                    "gold": _clone_arm(repository, gold_root, packet["base_commit"],
                                       case_output, "gold", setup_timeout),
                }
                setup["reference_apply"] = _apply_reference(
                    gold_root, packet["reference_diff"], case_output, setup_timeout)
                base_files = _materialize_bundle(base_root, bundle)
                gold_files = _materialize_bundle(gold_root, bundle)
                if base_files != gold_files:
                    raise PreparationError("test_bundle_differs_between_arms")
                setup["materialized_test_files"] = base_files
                base_run = _execute_arm(base_root, "base", bundle, case_output, timeout)
                gold_run = _execute_arm(gold_root, "gold", bundle, case_output, timeout)
            setup_path = case_output / "20_19_02_setup.json"
            write_json(setup_path, setup)
        base_by_id = {item["test_id"]: item for item in base_run["record"]["tests"]}
        gold_by_id = {item["test_id"]: item for item in gold_run["record"]["tests"]}
        transitions = []
        for test_id in bundle["stable_test_ids"]:
            _reconcile_hidden_gold_pass(
                base_by_id[test_id], gold_by_id[test_id], gold_run["record"])
            base_status = base_by_id[test_id]["status"]
            gold_status = gold_by_id[test_id]["status"]
            transitions.append({"test_id": test_id, "base_status": base_status,
                                "gold_status": gold_status,
                                "classification": classify_transition(base_status, gold_status)})
        classes = {item["classification"] for item in transitions}
        if "error" in classes:
            overall = {"status": "technical_failure", "failure_ledger": "technical",
                       "failure_class": "arm_test_infrastructure_error", "retryable": True}
        elif classes & {"missing", "skip"}:
            overall = {"status": "measurement_rejected", "failure_ledger": "evidence",
                       "failure_class": "target_test_not_observed", "retryable": True}
        elif classes <= {"provisional_f2p", "provisional_p2p"}:
            overall = {"status": "measured_provisional", "failure_ledger": None}
        else:
            overall = {"status": "measured_unresolved", "failure_ledger": "semantic"}
        record.update(
            **overall,
            base_run=base_run["binding"], gold_run=gold_run["binding"],
            transitions=transitions,
            provisional_FAIL_TO_PASS=[item["test_id"] for item in transitions
                                      if item["classification"] == "provisional_f2p"],
            provisional_PASS_TO_PASS=[item["test_id"] for item in transitions
                                      if item["classification"] == "provisional_p2p"],
            unresolved=[item for item in transitions if item["classification"] not in
                        {"provisional_f2p", "provisional_p2p"}],
        )
    except subprocess.TimeoutExpired:
        record.update(status="technical_failure", failure_ledger="technical",
                      failure_class="setup_timeout", retryable=True)
    except Exception as exc:
        record.update(status="technical_failure", failure_ledger="technical",
                      failure_class=type(exc).__name__, reason=str(exc), retryable=True)
    path = case_output / "20_19_05_transitions.json"
    write_json(path, record)
    record["result"] = _binding(path)
    return record


def run(campaign: Path, repositories: Path | None, output: Path, *, workers: int = 4,
        timeout: int = 1800, setup_timeout: int = 600, backend: str = "clone",
        image_prefix: str = "visual-env-build") -> dict:
    if backend not in BACKENDS:
        raise ValueError("backend must be clone or docker")
    if backend == "clone" and repositories is None:
        raise ValueError("clone backend requires repositories")
    campaign = campaign.resolve(strict=True)
    summary_path = campaign / "20_17_08_summary.json"
    if not summary_path.is_file():
        raise ValueError("20_17 campaign summary is missing")
    summary = json.loads(summary_path.read_text())
    if summary.get("schema_version") != "v4-test-construction-campaign-v1":
        raise ValueError("unsupported 20_17 campaign summary")
    records = summary.get("records") or []
    case_ids = [item.get("case_id") for item in records]
    if not records or None in case_ids or len(case_ids) != len(set(case_ids)):
        raise ValueError("campaign records are empty or duplicate")
    output = output.resolve()
    if output.exists():
        raise ValueError(f"measurement output already exists: {output}")
    output.mkdir(parents=True)
    write_json(output / "20_19_00_campaign_binding.json", {
        "schema_version": "v4-provisional-measurement-campaign-binding-v1",
        "campaign": _binding(summary_path), "case_count": len(records),
        "workers": workers, "timeout_seconds": timeout,
        "setup_timeout_seconds": setup_timeout, "backend": backend,
        "image_prefix": image_prefix if backend == "docker" else None,
    })
    measured = []
    with ThreadPoolExecutor(max_workers=min(workers, len(records))) as pool:
        futures = {pool.submit(_measure_case, campaign, item, repositories,
                               output, timeout, setup_timeout, backend,
                               image_prefix): item["case_id"]
                   for item in records}
        for future in as_completed(futures):
            try:
                item = future.result()
            except Exception as exc:
                item = {"case_id": futures[future], "status": "technical_failure",
                        "failure_ledger": "technical", "failure_class": type(exc).__name__,
                        "reason": str(exc), "retryable": True}
            measured.append(item)
            print(json.dumps(item, ensure_ascii=False), flush=True)
    measured.sort(key=lambda item: item["case_id"])
    counts = {}
    for item in measured:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    result = {
        "schema_version": "v4-provisional-base-gold-measurement-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "campaign": _binding(summary_path), "counts": counts,
        "records": measured,
        "boundary": "provisional measurement only; no human test approval, frozen task, or Harbor admission is implied",
    }
    write_json(output / "20_19_06_summary.json", result)
    return result
