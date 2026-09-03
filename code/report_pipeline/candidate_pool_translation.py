"""Translate a frozen capability pool for curator display with auditable retries."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil

from analysis.scripts.step_16_04_translate_human_review import validate
from pr_crawler.api_engines import ApiEvaluator, digest
from report_pipeline.atomic import write_json
from report_pipeline.capability_candidate_pool import (
    _load_translations, _translation_source_sha,
)
from report_pipeline.paths import CODE_ROOT


PROMPT = CODE_ROOT / "analysis/prompts/16_04_01_translation.system.md"
SCHEMA = CODE_ROOT / "analysis/prompts/16_04_02_translation.schema.json"


def _load_pool(source_run: Path) -> tuple[Path, list[dict]]:
    manifest_path = source_run / "16_11_07_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if (manifest.get("schema_version") != "capability-candidate-pool-manifest-v2"
            or manifest.get("data") != "16_11_05_candidate_pool.json"):
        raise ValueError("source capability pool manifest is invalid")
    data_path = source_run / manifest["data"]
    if digest(data_path) != manifest.get("data_sha256"):
        raise ValueError("source capability pool data changed")
    data = json.loads(data_path.read_text())
    records = data.get("records") or []
    if (data.get("schema_version") != "capability-candidate-pool-v2"
            or not records
            or len({row.get("case_id") for row in records}) != len(records)):
        raise ValueError("source capability pool records are invalid")
    return manifest_path, records


def run(source_run: Path, output: Path, existing_paths: list[Path], evaluator,
        *, workers: int = 10, timeout: int = 480) -> dict:
    source_run, output = source_run.resolve(strict=True), output.resolve()
    if output.exists():
        raise ValueError(f"output already exists: {output}")
    if not 1 <= workers <= 16:
        raise ValueError("workers must be between 1 and 16")
    manifest_path, records = _load_pool(source_run)
    existing, existing_sources = _load_translations(existing_paths, records)

    output.mkdir(parents=True)
    shutil.copy2(PROMPT, output / "16_12_01_translation.system.md")
    shutil.copy2(SCHEMA, output / "16_12_02_translation.schema.json")
    calls = output / "16_12_03_calls"
    calls.mkdir()
    by_id = {row["case_id"]: row for row in records}
    missing = [row for row in records if row["case_id"] not in existing]
    results: dict[str, dict] = {}
    attempts: list[dict] = []

    def translate(row: dict) -> tuple[str, dict | None, dict]:
        case_id = row["case_id"]
        directory = calls / case_id
        directory.mkdir()
        source = {
            "case_id": case_id,
            "pr_title": row["archive"]["pr_title"] or "",
            "problem_statement": row["problem_statement"],
        }
        write_json(directory / "00_input.json", source)
        try:
            raw, invocation = evaluator(
                packet={"items": [source]}, image_paths=[], system_prompt=PROMPT,
                schema=SCHEMA, workdir=directory, timeout=timeout)
            values = raw.get("translations") or []
            if len(values) != 1:
                raise ValueError("translation response must contain exactly one item")
            value = values[0]
            validate(source, value)
            translated = {
                **value,
                "source_text_sha256": _translation_source_sha(row),
            }
            write_json(directory / "11_validated_translation.json", translated)
            write_json(directory / "12_invocation.json", invocation)
            return case_id, translated, {
                "case_id": case_id, "status": "complete", **invocation,
            }
        except Exception as exc:
            failure = {
                "case_id": case_id,
                "status": "pending_retry",
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
            write_json(directory / "11_failure.json", failure)
            return case_id, None, failure

    if missing:
        with ThreadPoolExecutor(max_workers=min(workers, len(missing))) as pool:
            futures = [pool.submit(translate, row) for row in missing]
            for future in as_completed(futures):
                case_id, translated, attempt = future.result()
                attempts.append(attempt)
                if translated is not None:
                    results[case_id] = translated

    combined = {}
    for case_id, item in existing.items():
        combined[case_id] = {
            "case_id": case_id,
            "pr_title_zh": item["pr_title_zh"],
            "problem_statement_zh": item["problem_statement_zh"],
            "source_text_sha256": item["source_text_sha256"],
        }
    combined.update(results)
    ordered_items = [combined[row["case_id"]] for row in records
                     if row["case_id"] in combined]
    translation_path = output / "16_12_04_translations_zh.json"
    artifact = {
        "schema_version": "human-review-zh-translations-v1",
        "notice": "Machine translation for curator display only; never benchmark input.",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_pool_manifest": str(manifest_path),
        "source_pool_manifest_sha256": digest(manifest_path),
        "prompt_sha256": digest(PROMPT),
        "schema_sha256": digest(SCHEMA),
        "runner_sha256": digest(Path(__file__)),
        "model_config": {
            "backend": evaluator.backend,
            "profile": evaluator.profile,
            "attempt_limit": evaluator.attempts,
            "workers": workers,
            "timeout": timeout,
        },
        "existing_sources": existing_sources,
        "items": ordered_items,
        "invocations": sorted(attempts, key=lambda item: item["case_id"]),
    }
    write_json(translation_path, artifact)
    failed = sorted(row["case_id"] for row in missing
                    if row["case_id"] not in results)
    audit = {
        "schema_version": "capability-pool-translation-audit-v1",
        "status": "complete" if len(combined) == len(records) else "pending_retry",
        "source_count": len(records),
        "reused_count": len(existing),
        "invoked_count": len(missing),
        "translated_count": len(combined),
        "failed_case_ids": failed,
        "translation_artifact": translation_path.name,
        "translation_artifact_sha256": digest(translation_path),
    }
    write_json(output / "16_12_05_audit.json", audit)
    return audit


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--existing", type=Path, action="append", default=[])
    parser.add_argument("--backend", choices=("gemini", "k3"), default="gemini")
    parser.add_argument("--model")
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--attempts", type=int, choices=(1, 2, 3), default=3)
    parser.add_argument("--timeout", type=int, default=480)
    args = parser.parse_args()
    evaluator = ApiEvaluator(
        args.backend, args.model, args.key_file, args.attempts,
        min_interval=0.0, max_tokens=32768,
        cooldown_path=args.output / "16_12_03_calls/cooldown.json",
    )
    result = run(
        args.source_run, args.output, args.existing, evaluator,
        workers=args.workers, timeout=args.timeout)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["status"] == "complete" else 2)


if __name__ == "__main__":
    main()
