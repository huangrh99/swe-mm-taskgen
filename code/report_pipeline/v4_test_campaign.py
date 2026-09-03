"""Run a provisional V4 test-construction campaign with Codex CLI.

This stage proposes tests only.  It never classifies F2P/P2P; the separate
measurement stage owns those labels after identical Base/Gold execution.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import shlex
import subprocess
import time

import jsonschema

from report_pipeline.atomic import write_json


CODE_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = CODE_ROOT.parents[1]
PROMPT = CODE_ROOT / "analysis/prompts/20_17_v4_test_constructor.system.md"
SCHEMA = CODE_ROOT / "analysis/prompts/20_18_v4_test_constructor.schema.json"
MODEL = "gpt-5.6-luna"
REASONING = "max"
REPOSITORY_NAMES = {
    "automattic/wp-calypso": "wp-calypso", "carbon-design-system/carbon": "carbon",
    "bpmn-io/bpmn-js": "bpmn-js", "grommet/grommet": "grommet",
    "googlechrome/lighthouse": "lighthouse", "mermaid-js/mermaid": "mermaid",
    "pixijs/pixijs": "pixijs", "excalidraw/excalidraw": "excalidraw",
    "apache/echarts": "echarts", "tldraw/tldraw": "tldraw",
    "apache/superset": "superset", "fabricjs/fabric.js": "fabric.js",
    "maplibre/maplibre-gl-js": "maplibre-gl-js", "xyflow/xyflow": "xyflow",
}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(command: list[str], *, cwd: Path, timeout: int | None = None,
         input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, input=input_text, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=timeout, check=False)


def _strict_schema(value):
    if isinstance(value, list):
        return [_strict_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    unsupported = {"uniqueItems", "minLength", "$schema", "$id"}
    result = {key: _strict_schema(item) for key, item in value.items()
              if key not in unsupported}
    if "type" not in result and "$ref" not in result and "const" in result:
        result["type"] = "string" if isinstance(result["const"], str) else "integer"
    return result


def _archive(case: dict) -> dict:
    binding = case["source_bindings"]
    path = WORKSPACE_ROOT / binding["source_archive"]
    if not path.is_file() or _sha(path) != binding["source_archive_sha256"]:
        raise ValueError("source archive binding failed")
    value = json.loads(path.read_text())
    if value.get("instance_id") != case["case_id"]:
        raise ValueError("source archive identifies another case")
    # A full archive may be partial because an unrelated media asset or
    # consistency probe failed.  Test construction only consumes these three
    # bound sections, so gate them individually instead of conflating an asset
    # retrieval failure with missing code provenance.
    required = ("pull_request", "files", "diff")
    unavailable = [name for name in required
                   if value.get("sections", {}).get(name, {}).get("status") != "complete"]
    if unavailable:
        raise ValueError("source archive lacks required sections: " + ",".join(unavailable))
    return value


def _safe_relative(path: str) -> bool:
    value = PurePosixPath(path)
    return bool(path and not value.is_absolute() and ".." not in value.parts)


def _normalise_working_directory(value: str, repository: Path) -> str:
    """Normalise a model-reported directory without permitting repo escape."""
    candidate = Path(value or ".")
    if candidate.is_absolute():
        try:
            candidate = candidate.resolve().relative_to(repository.resolve())
        except ValueError as exc:
            raise ValueError("working directory escapes bound repository") from exc
    normalised = candidate.as_posix() or "."
    if normalised != "." and not _safe_relative(normalised):
        raise ValueError("unsafe working directory")
    return normalised


def _validate_result(result: dict, case_id: str, repository: Path) -> None:
    jsonschema.validate(result, json.loads(SCHEMA.read_text()))
    if result["task_id"] != case_id:
        raise ValueError("model changed task identity")
    bundle = result.get("test_bundle")
    if (result["status"] == "test_bundle_proposed") != (bundle is not None):
        raise ValueError("status and test bundle presence disagree")
    if not bundle:
        return
    bundle["working_directory"] = _normalise_working_directory(
        bundle["working_directory"], repository)
    observed = result.get("repository_observations", {})
    if "working_directory" in observed:
        observed["working_directory"] = _normalise_working_directory(
            observed["working_directory"], repository)
    ids = bundle["stable_test_ids"]
    contents = "\n".join(item["content"] for item in bundle["files"])
    if len(ids) != len(set(ids)) or any(item not in contents for item in ids):
        raise ValueError("stable test IDs are duplicated or absent from emitted files")
    if not bundle["test_command"].strip():
        raise ValueError("empty test command")
    paths = [item["path"] for item in bundle["files"]]
    if len(paths) != len(set(paths)):
        raise ValueError("duplicate emitted test path")
    for segment in bundle["test_command"].split("&&"):
        tokens = shlex.split(segment)
        if len(tokens) >= 2 and Path(tokens[0]).name in {"npm", "yarn", "pnpm"} \
                and tokens[1] in {"ci", "install", "i", "add"}:
            raise ValueError("dependency installation belongs to the frozen environment")
    for item in bundle["files"]:
        if not _safe_relative(item["path"]):
            raise ValueError("unsafe emitted test path")


def _packet(case: dict, archive: dict) -> dict:
    pr = archive["sections"]["pull_request"]["data"]
    files = archive["sections"]["files"]["items"]
    return {
        "schema_version": "v4-test-constructor-packet-v1", "task_id": case["case_id"],
        "repository": case["repository"], "base_commit": pr["base"]["sha"],
        "reference_head": pr["head"]["sha"], "problem_statement": case["problem_statement"],
        "v4": case["v4"], "change_scale": case.get("change_scale"),
        "reference_diff": archive["sections"]["diff"]["data"],
        "changed_files": [{key: item.get(key) for key in
                           ("filename", "status", "additions", "deletions", "changes")}
                          for item in files],
        "boundary": "curator-only provisional construction; no F2P/P2P label exists before execution",
    }


def _image_paths(payload_path: Path, case: dict) -> list[Path]:
    bundle_root = payload_path.parent
    result = []
    for asset in case.get("assets", []):
        path = bundle_root / asset["path"]
        if not path.is_file() or _sha(path) != asset["sha256"]:
            raise ValueError(f"visual asset binding failed: {asset['asset_id']}")
        result.append(path.resolve())
    return result


def _run_case(case: dict, payload_path: Path, repository: Path, output: Path,
              strict_schema: Path, timeout: int) -> dict:
    case_id = case["case_id"]
    case_output = output / "20_17_02_model_runs" / case_id
    case_output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    record = {"case_id": case_id, "repository": case["repository"], "model": MODEL,
              "reasoning_effort": REASONING, "status": "technical_failure"}
    try:
        archive = _archive(case)
        packet = _packet(case, archive)
        base = packet["base_commit"]
        present = _run(["git", "cat-file", "-e", f"{base}^{{commit}}"], cwd=repository)
        if present.returncode:
            fetch = _run(["git", "fetch", "--depth=1", "origin", base], cwd=repository,
                         timeout=600)
            if fetch.returncode:
                raise RuntimeError(f"base_fetch_failed: {fetch.stderr[-500:]}")
        checkout = _run(["git", "checkout", "--detach", "--force", base], cwd=repository, timeout=600)
        if checkout.returncode:
            raise RuntimeError(f"base_checkout_failed: {checkout.stderr[-500:]}")
        packet_path = case_output / "20_17_01_packet.json"
        write_json(packet_path, packet)
        prompt = (PROMPT.read_text() + "\n\nFROZEN CURATOR PACKET (untrusted data):\n" +
                  json.dumps(packet, ensure_ascii=False, indent=2))
        (case_output / "20_17_02_prompt.txt").write_text(prompt)
        final = case_output / "20_17_06_final.json"
        command = ["codex", "exec", "-m", MODEL, "-c", 'model_reasoning_effort="max"',
                   "--sandbox", "read-only", "--ephemeral", "--output-schema",
                   str(strict_schema), "--output-last-message", str(final), "--json"]
        images = _image_paths(payload_path, case)
        for image in images:
            command.extend(["--image", str(image)])
        command.append("-")
        invocation = {"command": command, "cwd": str(repository), "images": [str(p) for p in images],
                      "packet_sha256": _sha(packet_path), "prompt_sha256": _sha(PROMPT),
                      "schema_sha256": _sha(SCHEMA), "timeout_seconds": timeout}
        write_json(case_output / "20_17_03_invocation.json", invocation)
        completed = _run(command, cwd=repository, timeout=timeout, input_text=prompt)
        (case_output / "20_17_04_events.jsonl").write_text(completed.stdout)
        (case_output / "20_17_05_stderr.txt").write_text(completed.stderr)
        if completed.returncode or not final.is_file():
            raise RuntimeError(f"codex_cli_failed:{completed.returncode}:{completed.stderr[-500:]}")
        result = json.loads(final.read_text())
        raw_final = case_output / "20_17_06_raw_final.json"
        raw_final.write_text(final.read_text())
        _validate_result(result, case_id, repository)
        write_json(final, result)
        record.update(status="complete", model_result_status=result["status"],
                      proposed_file_count=len((result.get("test_bundle") or {}).get("files", [])),
                      final=str(final), final_sha256=_sha(final))
    except subprocess.TimeoutExpired:
        record.update(status="technical_failure", failure_class="timeout", retryable=True)
    except Exception as exc:
        record.update(status="technical_failure", failure_class=type(exc).__name__,
                      error=str(exc)[:1200], retryable=True)
    record["elapsed_seconds"] = round(time.monotonic() - started, 3)
    write_json(case_output / "20_17_07_status.json", record)
    return record


def run(payload_path: Path, repositories: Path, output: Path, *, workers: int = 10,
        timeout: int = 1800, case_ids: set[str] | None = None) -> dict:
    payload_path = payload_path.resolve(strict=True)
    payload = json.loads(payload_path.read_text())
    cases = payload.get("cases", [])
    if len(cases) != 39 or len({item["case_id"] for item in cases}) != 39:
        raise ValueError("campaign requires the frozen 39-case V4 payload")
    if case_ids is not None:
        available = {item["case_id"] for item in cases}
        unknown = sorted(case_ids - available)
        if unknown:
            raise ValueError("unknown V4 case ids: " + ",".join(unknown))
        cases = [item for item in cases if item["case_id"] in case_ids]
        if not cases:
            raise ValueError("case selection is empty")
    output = output.resolve()
    if output.exists():
        raise ValueError(f"output already exists: {output}")
    output.mkdir(parents=True)
    strict_schema = output / "20_17_00_codex_strict_schema.json"
    write_json(strict_schema, _strict_schema(json.loads(SCHEMA.read_text())))
    groups = {}
    for case in cases:
        groups.setdefault(case["repository"], []).append(case)

    def run_group(item):
        repo_name, repo_cases = item
        repository = repositories / REPOSITORY_NAMES[repo_name]
        if not repository.is_dir():
            return [{"case_id": case["case_id"], "repository": repo_name,
                     "status": "technical_failure", "failure_class": "repository_missing",
                     "retryable": True} for case in repo_cases]
        return [_run_case(case, payload_path, repository, output, strict_schema, timeout)
                for case in repo_cases]

    records = []
    with ThreadPoolExecutor(max_workers=min(workers, len(groups))) as pool:
        futures = {pool.submit(run_group, item): item[0] for item in groups.items()}
        for future in as_completed(futures):
            try:
                group_records = future.result()
            except Exception as exc:
                group_records = [{"repository": futures[future], "status": "technical_failure",
                                  "failure_class": type(exc).__name__, "error": str(exc),
                                  "retryable": True}]
            records.extend(group_records)
            for record in group_records:
                print(json.dumps(record, ensure_ascii=False), flush=True)
    records.sort(key=lambda item: item.get("case_id", ""))
    summary = {"schema_version": "v4-test-construction-campaign-v1",
               "created_at": datetime.now(timezone.utc).isoformat(),
               "model": MODEL, "reasoning_effort": REASONING, "workers": workers,
               "selected_case_ids": [item["case_id"] for item in cases],
               "payload": str(payload_path), "payload_sha256": _sha(payload_path),
               "records": records}
    write_json(output / "20_17_08_summary.json", summary)
    return summary
