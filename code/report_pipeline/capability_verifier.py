"""Run the narrow V4 visual-capability verifier over leak-screened Issue assets."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import threading

from report_pipeline.atomic import write_json
from report_pipeline.pre_review_classification import (
    PROMPT, SCHEMA, _evaluator_contract, _prepare_model_image,
    _validate_evaluator_contract, _validate_request_semantics, _validate_visual,
)
from report_pipeline.pr_image_roles import validate_run as validate_image_role_run

def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _issue_problem_statement(packet: dict, selected_asset_ids: list[str]) -> tuple[str, list[dict]]:
    selected = {item["asset_id"]: item for item in packet.get("assets") or []
                if item.get("asset_id") in selected_asset_ids}
    source_keys = set()
    for asset in selected.values():
        for occurrence in asset.get("occurrences") or []:
            source_id = str(occurrence.get("source_id") or "")
            if source_id.startswith("issue:"):
                source_id = source_id[6:]
            if "#" in source_id:
                source_keys.add(source_id.split(":", 1)[0].lower())
    documents = []
    for document in packet.get("source_documents") or []:
        if document.get("source_kind") != "issue":
            continue
        source_id = str(document.get("source_id") or "")
        if source_id.split(":", 1)[0].lower() in source_keys:
            documents.append(document)
    if not documents:
        raise ValueError("selected assets have no bound Issue problem source")
    parts = []
    bindings = []
    for document in documents:
        text = document.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        parts.append(f"## {document['source_id']}\n\n{text.strip()}")
        bindings.append({
            "source_id": document["source_id"],
            "url": document.get("url"),
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        })
    if not parts:
        raise ValueError("Issue problem statement is empty")
    return "\n\n".join(parts), bindings


def _prepare_case(source: dict, root: Path, index: int) -> dict:
    case_id = source.get("case_id")
    role_run = Path(source.get("image_role_run", "")).resolve(strict=True)
    run = validate_image_role_run(role_run)
    matches = [(position, item) for position, item in enumerate(run["records"], 1)
               if item.get("case_id") == case_id]
    if len(matches) != 1 or matches[0][1].get("status") != "complete":
        raise ValueError(f"{case_id}: image-role record is missing or incomplete")
    role_case_index, role_record = matches[0]
    annotation = role_record["annotation"]
    if annotation.get("source_path_recommendation") != "issue_derived":
        raise ValueError(f"{case_id}: V4 automatic input requires issue_derived evidence")
    selected_ids = source.get("asset_ids") or []
    if not selected_ids or len(selected_ids) != len(set(selected_ids)):
        raise ValueError(f"{case_id}: selected assets are missing or duplicated")
    role_images = {item.get("asset_id"): item for item in annotation.get("images") or []}
    for asset_id in selected_ids:
        image = role_images.get(asset_id)
        if (not image or image.get("observed") is not True
                or image.get("contains_fixed_after") != "no"
                or image.get("contains_solution_evidence") != "no"
                or image.get("task_relationship") != "explicit"
                or image.get("confidence") != "high"):
            raise ValueError(f"{case_id}: selected asset is not a safe high-confidence role candidate")

    role_packet_path = Path(role_record["packet"]).resolve(strict=True)
    if _sha(role_packet_path) != role_record["packet_sha256"]:
        raise ValueError(f"{case_id}: image-role packet changed")
    role_packet = json.loads(role_packet_path.read_text())
    problem_statement, source_bindings = _issue_problem_statement(role_packet, selected_ids)
    archive_path = Path(role_record["source_archive"]).resolve(strict=True)
    if _sha(archive_path) != role_record["source_archive_sha256"]:
        raise ValueError(f"{case_id}: source archive changed")
    archive = json.loads(archive_path.read_text())
    archive_assets = {item.get("sha256"): item
                      for item in archive["sections"]["assets"].get("items") or []}
    packet_assets, image_paths = [], []
    case_root = root / "16_11_01_packets" / f"case_{index:04d}"
    case_root.mkdir(parents=True)
    role_assets = {item.get("asset_id"): item for item in role_packet.get("assets") or []}
    for position, asset_id in enumerate(selected_ids, 1):
        archived = archive_assets.get(asset_id)
        role_asset = role_assets.get(asset_id)
        if not archived or archived.get("status") != "complete" or not role_asset:
            raise ValueError(f"{case_id}: selected asset archive binding is incomplete")
        source_path = (archive_path.parent / "11_http_archive" / archived["local_path"]).resolve(strict=True)
        if _sha(source_path) != asset_id:
            raise ValueError(f"{case_id}: selected asset bytes changed")
        existing_representation = role_asset.get("model_input_representation") or {}
        kind = existing_representation.get("kind")
        if kind in {"video_contact_sheet", "animated_gif_contact_sheet"}:
            model_root = (role_run / "08_04_02_model_inputs"
                          / f"case_{role_case_index:04d}")
            expected_derived = existing_representation.get("derived_sha256")
            matches = [path for path in model_root.glob("*.png")
                       if _sha(path) == expected_derived]
            if len(matches) != 1:
                raise ValueError(f"{case_id}: frozen image-role contact sheet changed")
            prior = matches[0]
            prepared = case_root / f"16_11_01_asset_{position:02d}.png"
            shutil.copy2(prior, prepared)
            representation = existing_representation
        else:
            prepared, representation = _prepare_model_image(
                source_path, case_root / f"16_11_01_asset_{position:02d}.png")
        if prepared is None:
            raise ValueError(f"{case_id}: selected asset cannot be represented to the VLM")
        image_paths.append(prepared)
        packet_assets.append({
            "asset_id": asset_id,
            "attachment_index": position,
            "source_ids": sorted({item.get("source_id")
                                  for item in role_asset.get("occurrences") or []
                                  if item.get("source_id")}),
            "model_input_representation": representation,
        })
    packet = {
        "task_id": case_id,
        "problem_statement": problem_statement,
        "assets": packet_assets,
    }
    packet_path = case_root / "16_11_01_packet.json"
    write_json(packet_path, packet)
    return {
        "case_id": case_id,
        "role_run": str(role_run),
        "role_results_sha256": _sha(role_run / "08_04_03_results.json"),
        "role_packet": str(role_packet_path),
        "role_packet_sha256": _sha(role_packet_path),
        "source_archive": str(archive_path),
        "source_archive_sha256": _sha(archive_path),
        "source_bindings": source_bindings,
        "packet": str(packet_path),
        "packet_sha256": _sha(packet_path),
        "image_paths": [str(path) for path in image_paths],
        "status": "prepared",
        "annotation": None,
        "invocation": None,
        "failures": [],
    }


def _run_case(record: dict, evaluator, root: Path, timeout: int,
              prompt: Path, schema: Path) -> dict:
    packet = json.loads(Path(record["packet"]).read_text())
    for semantic_attempt in (1, 2):
        workdir = root / "16_11_02_model_runs" / record["case_id"] / f"semantic_{semantic_attempt:02d}"
        workdir.mkdir(parents=True)
        request_packet = json.loads(json.dumps(packet))
        if record["failures"]:
            request_packet["previous_output_validation_error"] = record["failures"][-1]["error"]
        try:
            annotation, invocation = evaluator(
                packet=request_packet,
                image_paths=[Path(path) for path in record["image_paths"]],
                system_prompt=prompt, schema=schema, workdir=workdir, timeout=timeout)
            _validate_visual(annotation, packet, schema)
            trace_files = {}
            for path in workdir.iterdir():
                if path.is_file():
                    trace_files[path.name] = {"path": str(path), "sha256": _sha(path)}
            record.update(status="complete", annotation=annotation,
                          invocation={**invocation, "semantic_attempt": semantic_attempt,
                                      "trace_files": trace_files})
            return record
        except Exception as exc:
            record["failures"].append({
                "semantic_attempt": semantic_attempt,
                "error_type": type(exc).__name__,
                "error": str(exc)[:1000],
            })
    record["status"] = "failed"
    return record


def run(config_path: Path, output: Path, *, evaluator=None, timeout: int = 480,
        workers: int = 10) -> dict:
    config_path, output = config_path.resolve(), output.resolve()
    if output.exists():
        raise ValueError(f"output already exists: {output}")
    config = json.loads(config_path.read_text())
    if config.get("schema_version") != "capability-verifier-input-v1":
        raise ValueError("unsupported capability-verifier input config")
    if config.get("classifier_mode", "v4_current") != "v4_current":
        raise ValueError("capability verifier accepts only the current V4 classifier")
    sources = config.get("records")
    if not isinstance(sources, list) or not sources:
        raise ValueError("capability-verifier records are missing")
    case_ids = [item.get("case_id") for item in sources]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("duplicate capability-verifier case")
    output.mkdir(parents=True)
    shutil.copy2(PROMPT, output / "16_11_00_capability.system.md")
    shutil.copy2(SCHEMA, output / "16_11_00_capability.schema.json")
    shutil.copy2(Path(__file__), output / "16_11_00_runner.py")
    frozen_prompt = output / "16_11_00_capability.system.md"
    frozen_schema = output / "16_11_00_capability.schema.json"
    prepared = [_prepare_case(source, output, index)
                for index, source in enumerate(sources, 1)]
    model_contract = _evaluator_contract(evaluator) if evaluator is not None else None
    if evaluator is not None:
        completed = []
        lock = threading.Lock()
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(
                _run_case, record, evaluator, output, timeout,
                frozen_prompt, frozen_schema): record["case_id"]
                       for record in prepared}
            for future in as_completed(futures):
                value = future.result()
                with lock:
                    completed.append(value)
        by_case = {item["case_id"]: item for item in completed}
        records = [by_case[case_id] for case_id in case_ids]
    else:
        records = prepared
    result = {
        "schema_version": "capability-verifier-run-v1",
        "classifier_mode": "v4_current",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "config": str(config_path),
        "config_sha256": _sha(config_path),
        "prompt_sha256": _sha(output / "16_11_00_capability.system.md"),
        "schema_sha256": _sha(output / "16_11_00_capability.schema.json"),
        "runner_sha256": _sha(output / "16_11_00_runner.py"),
        "model_invoked": evaluator is not None,
        "model_contract": model_contract,
        "workers": workers,
        "records": records,
        "counts": {
            "total": len(records),
            "complete": sum(item["status"] == "complete" for item in records),
            "failed": sum(item["status"] == "failed" for item in records),
            "prepared": sum(item["status"] == "prepared" for item in records),
            "semantic_failures": sum(len(item["failures"]) for item in records),
        },
    }
    result_path = output / "16_11_03_capability_results.json"
    write_json(result_path, result)
    write_json(output / "16_11_04_manifest.json", {
        "schema_version": "capability-verifier-manifest-v1",
        "result": result_path.name,
        "result_sha256": _sha(result_path),
        "status": ("complete" if result["counts"]["complete"] == len(records)
                   else "prepared" if evaluator is None else "partial"),
    })
    return result


def validate_run(run_directory: Path) -> dict:
    """Revalidate a V4 capability run and every frozen model/source binding."""
    from pr_crawler.api_engines import extract_annotation

    run_directory = run_directory.resolve(strict=True)
    result_path = run_directory / "16_11_03_capability_results.json"
    manifest_path = run_directory / "16_11_04_manifest.json"
    result = json.loads(result_path.read_text())
    manifest = json.loads(manifest_path.read_text())
    if (result.get("schema_version") != "capability-verifier-run-v1"
            or manifest.get("schema_version") != "capability-verifier-manifest-v1"
            or manifest.get("result") != result_path.name
            or manifest.get("result_sha256") != _sha(result_path)):
        raise ValueError("capability run result/manifest binding changed")

    if result.get("classifier_mode", "v4_current") != "v4_current":
        raise ValueError("capability run is not a current V4 classification")
    prompt = run_directory / "16_11_00_capability.system.md"
    schema = run_directory / "16_11_00_capability.schema.json"
    runner = run_directory / "16_11_00_runner.py"
    config = Path(result.get("config", ""))
    if (not prompt.is_file() or result.get("prompt_sha256") != _sha(prompt)
            or not schema.is_file() or result.get("schema_sha256") != _sha(schema)
            or not runner.is_file() or result.get("runner_sha256") != _sha(runner)
            or not config.is_file() or result.get("config_sha256") != _sha(config)):
        raise ValueError("capability run code/prompt/schema/config binding changed")
    schema_value = json.loads(schema.read_text())
    if schema_value.get("$id") != "visual-capability-classifier-v4":
        raise ValueError("capability classifier mode/schema binding changed")

    records = result.get("records")
    if not isinstance(records, list) or not records:
        raise ValueError("capability run records are missing")
    case_ids = [record.get("case_id") for record in records]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("duplicate capability run case")
    contract = result.get("model_contract")
    if result.get("model_invoked"):
        contract = _validate_evaluator_contract(contract)
    elif contract is not None:
        raise ValueError("prepare-only capability run has a model contract")

    semantic_failure_count = 0
    for record in records:
        role_run = Path(record.get("role_run", ""))
        role_result = validate_image_role_run(role_run)
        role_result_path = role_run / "08_04_03_results.json"
        if (record.get("role_results_sha256") != _sha(role_result_path)
                or len([item for item in role_result["records"]
                        if item.get("case_id") == record["case_id"]]) != 1):
            raise ValueError("capability image-role source binding changed")
        for path_key, hash_key in (
                ("role_packet", "role_packet_sha256"),
                ("source_archive", "source_archive_sha256"),
                ("packet", "packet_sha256")):
            path = Path(record.get(path_key, ""))
            if not path.is_file() or _sha(path) != record.get(hash_key):
                raise ValueError(f"capability {path_key} binding changed")
        packet_path = Path(record["packet"])
        if not packet_path.resolve().is_relative_to(run_directory):
            raise ValueError("capability packet escapes run directory")
        packet = json.loads(packet_path.read_text())
        if packet.get("task_id") != record["case_id"]:
            raise ValueError("capability packet identity changed")
        failures = record.get("failures")
        if not isinstance(failures, list):
            raise ValueError("capability semantic failure ledger is missing")
        semantic_failure_count += len(failures)

        if record.get("status") == "complete":
            annotation = record.get("annotation")
            _validate_visual(annotation, packet, schema)
            invocation = record.get("invocation") or {}
            semantic_attempt = invocation.get("semantic_attempt")
            if (not isinstance(semantic_attempt, int) or semantic_attempt not in {1, 2}
                    or len(failures) != semantic_attempt - 1):
                raise ValueError("capability semantic attempt ledger changed")
            trace_files = invocation.get("trace_files")
            if not isinstance(trace_files, dict):
                raise ValueError("capability trace ledger is missing")
            bound = {}
            for name, binding in trace_files.items():
                path = Path(binding.get("path", ""))
                if (not path.is_file()
                        or not path.resolve().is_relative_to(run_directory)
                        or _sha(path) != binding.get("sha256")):
                    raise ValueError("capability trace binding changed")
                bound[name] = path
            required = {"10_api_request.json", "09_model_raw.json",
                        "10_api_invocation.json"}
            if not required <= set(bound):
                raise ValueError("capability required traces are missing")
            provider_name = Path(invocation.get("provider_response", "")).name
            attempt_number = invocation.get("attempts")
            attempt_name = (f"10_attempt_{attempt_number:02d}.json"
                            if isinstance(attempt_number, int) else "")
            if provider_name not in bound or attempt_name not in bound:
                raise ValueError("capability provider/attempt trace is missing")
            for key, name in (("request", "10_api_request.json"),
                              ("raw_response", "09_model_raw.json"),
                              ("provider_response", provider_name)):
                if (Path(invocation.get(key, "")).resolve() != bound[name].resolve()
                        or invocation.get(f"{key}_sha256") != _sha(bound[name])):
                    raise ValueError(f"capability {key} invocation binding changed")
            request = json.loads(bound["10_api_request.json"].read_text())
            request_invocation = dict(invocation)
            request_invocation["semantic_validation_attempts"] = semantic_attempt
            request_invocation["prior_validation_failures"] = [
                item.get("error", "") for item in failures]
            protocol = contract["provider_profile"]["protocol"]
            _validate_request_semantics(
                request, packet, prompt, schema, protocol,
                contract["requested_model"], request_invocation,
                contract["max_tokens"], allow_legacy_video_probe=True)
            raw = json.loads(bound["09_model_raw.json"].read_text())
            provider = json.loads(bound[provider_name].read_text())
            receipt = json.loads(bound[attempt_name].read_text())
            if (raw != annotation or extract_annotation(provider, protocol) != annotation
                    or invocation.get("backend") != contract["backend"]
                    or invocation.get("requested_model") != contract["requested_model"]
                    or invocation.get("model") not in contract["accepted_response_models"]
                    or not 1 <= attempt_number <= contract["transport_attempt_limit"]
                    or receipt.get("status") != "received"
                    or receipt.get("response_sha256")
                    != invocation.get("provider_response_sha256")):
                raise ValueError("capability provider response or annotation changed")
        elif record.get("status") in {"prepared", "failed"}:
            if record.get("annotation") is not None:
                raise ValueError("non-complete capability record has an annotation")
        else:
            raise ValueError("unsupported capability record status")

    expected_counts = {
        "total": len(records),
        "complete": sum(item["status"] == "complete" for item in records),
        "failed": sum(item["status"] == "failed" for item in records),
        "prepared": sum(item["status"] == "prepared" for item in records),
        "semantic_failures": semantic_failure_count,
    }
    if result.get("counts") != expected_counts:
        raise ValueError("capability run counts changed")
    expected_status = ("complete" if expected_counts["complete"] == len(records)
                       else "prepared" if not result.get("model_invoked") else "partial")
    if manifest.get("status") != expected_status:
        raise ValueError("capability run status changed")
    return result
