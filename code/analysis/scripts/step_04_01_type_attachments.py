"""Type ambiguous body attachments, then publish an explicitly named final image subset."""

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from report_pipeline.paths import CODE_ROOT, WORKSPACE_ROOT

ROOT = WORKSPACE_ROOT
sys.path.insert(0, str(CODE_ROOT))
from pr_crawler.image_screening import classify
from pr_crawler.media_probe import bounded_probe
from pr_crawler.store import now
from analysis.scripts.step_01_screen_pr_body_images import ALL_EVIDENCE


def load_evidence(output):
    with (output / ALL_EVIDENCE).open() as stream:
        return {(r["repo"], r["number"]): r for r in map(json.loads, stream)}


def type_assets(evidence, temporary, workers=8):
    assets = {a["asset_id"]: a for r in evidence.values() for a in r["image_screening"]["assets"]
              if a["media_kind"] in {"untyped_attachment", "conflicting"}}
    temporary.mkdir(parents=True, exist_ok=True)
    cache = temporary / "03_attachment_media_type_probe_cache.jsonl"
    results = {}
    if cache.exists():
        with cache.open() as stream:
            for line in stream:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue  # Interrupted append: retry that asset, never accept it.
                if r["status"] in {"typed", "unavailable", "unresolved"}:
                    results[r["asset_id"]] = r
    results = {k: v for k, v in results.items() if k in assets}
    todo = [a for k, a in assets.items() if k not in results]
    print(f"Attachment URLs: {len(assets)}; cached: {len(results)}; to probe: {len(todo)}", flush=True)
    with cache.open("a", encoding="utf-8") as stream, ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(bounded_probe, entry, temporary / "probe_staging"): entry for entry in todo}
        for future in as_completed(futures):
            entry = futures[future]
            result = future.result()
            result.setdefault("asset_id", entry["asset_id"])
            result.setdefault("media_kind", None)
            results[entry["asset_id"]] = result
            stream.write(json.dumps(result, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
            if len(results) % 50 == 0 or len(results) == len(assets):
                print(f"Typed/probed {len(results)}/{len(assets)}: {dict(Counter(r['status'] for r in results.values()))}", flush=True)
    return results


def apply_types(evidence, results):
    value = json.loads(json.dumps(evidence))
    value["pre_type_check_category"] = value["category"]
    value["network_validation"] = "ambiguous_attachment_prefix_only; other image URLs not fetched"
    for asset in value["assets"]:
        result = results.get(asset["asset_id"])
        if result:
            asset["media_kind_before_probe"] = asset["media_kind"]
            asset["type_probe"] = {k: v for k, v in result.items() if k != "prefix_base64"}
            asset["availability"] = result["status"]
            if result["status"] == "typed" and result.get("media_kind") in {"image", "video"}:
                asset["media_kind"] = result["media_kind"]
    value["category"] = classify(value["assets"])
    return value


def export_final(output, temporary, evidence, results):
    source_summary = json.loads((output / "00_pr_body_image_screening_summary.json").read_text())
    names = ["04_prs_with_non_badge_images.jsonl", "04_prs_with_image_evidence_including_badges.jsonl",
             "04_additional_non_badge_image_prs_from_attachment_typing.jsonl", "04_all_prs_classification_ledger.jsonl",
             "04_prs_with_video_evidence_no_image_evidence.jsonl", "04_pr_ids_with_unresolved_media_type_no_image_evidence.jsonl"]
    destination = output / "04_pr_body_images_after_attachment_typing"
    counts, repos, years, identities = Counter(), defaultdict(Counter), defaultdict(Counter), set()
    digest = hashlib.sha256()
    with tempfile.TemporaryDirectory(prefix="typed-image-export-", dir=temporary) as stage, ExitStack() as stack:
        paths = {name: Path(stage) / name for name in names}
        streams = {name: stack.enter_context(path.open("w", encoding="utf-8")) for name, path in paths.items()}
        def write(name, value):
            streams[name].write(json.dumps(value, ensure_ascii=False) + "\n")
        with Path(source_summary["input"]).open("rb") as stream:
            for raw in stream:
                digest.update(raw)
                row = json.loads(raw)
                key = (row["repo"], row["number"])
                if key in identities:
                    raise ValueError("Duplicate source identity")
                identities.add(key)
                before = evidence[key]["image_screening"]
                if hashlib.sha256((row.get("body") or "").encode()).hexdigest() != before["body_sha256"]:
                    raise ValueError("PR body changed after discovery")
                typed = apply_types(before, results)
                category = typed["category"]
                full = {**row, "image_screening": typed}
                counts["input_prs"] += 1
                counts[category] += 1
                repos[row["repo"]][category] += 1
                years[row["created_at"][:4]][category] += 1
                if any(a["media_kind"] == "image" for a in typed["assets"]):
                    write(names[1], full)
                    counts["all_image_evidence_including_badges"] += 1
                if category == "non_badge_image_evidence":
                    write(names[0], full)
                    if before["category"] != category:
                        write(names[2], full)
                        counts["additional_non_badge_image_prs_from_attachment_typing"] += 1
                if category == "video_without_image_evidence":
                    write(names[4], full)
                if category == "untyped_attachment_without_image_evidence":
                    write(names[5], {"repo": row["repo"], "number": row["number"], "id": row["id"], "image_screening": typed})
                write(names[3], {"repo": row["repo"], "number": row["number"], "id": row["id"],
                    "created_at": row["created_at"], "category": category, "pre_type_check_category": before["category"],
                    "asset_ids": [a["asset_id"] for a in typed["assets"]], "body_sha256": typed["body_sha256"],
                    "source_coverage": typed["source_coverage"]})
        if digest.hexdigest() != source_summary["input_sha256"] or identities != set(evidence):
            raise ValueError("Source file/evidence identity mismatch")
        for stream in streams.values():
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
        checks_path = Path(stage) / "03_attachment_media_type_checks.jsonl"
        checks_path.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for _, r in sorted(results.items())))
        paths[checks_path.name] = checks_path
        outputs = {}
        destination.mkdir(parents=True, exist_ok=True)
        for name, path in paths.items():
            sha = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    sha.update(chunk)
            outputs[name] = {"sha256": sha.hexdigest(), "bytes": path.stat().st_size}
            os.replace(path, destination / name)
        summary = {"generated_at": now(), "rule_version": "pr-body-images-v1+ambiguous-prefix-types-v1",
            "input": source_summary["input"], "input_sha256": digest.hexdigest(), "counts": dict(counts),
            "repositories": dict(sorted(repos.items())), "years": dict(sorted(years.items())),
            "probe_status_counts": dict(Counter(r["status"] for r in results.values())),
            "probe_media_type_counts": dict(Counter(r.get("media_kind") or "unknown" for r in results.values())),
            "scope": "PR-body image references, plus MIME/signature typing of ambiguous attachments only; not full image availability/decoding or visual necessity",
            "outputs": outputs}
        summary_path = Path(stage) / "04_image_screening_summary.json"
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
        os.replace(summary_path, destination / summary_path.name)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(ROOT / "crawler-output/multimodal-2025/image-screening"))
    parser.add_argument("--tmp", default=str(ROOT / "tmp/multimodal-2025/02_pr_body_image_screening"))
    parser.add_argument("--workers", type=int, choices=(1, 4, 8, 12), default=8)
    args = parser.parse_args()
    output, temporary = Path(args.output).resolve(), Path(args.tmp).resolve()
    evidence = load_evidence(output)
    results = type_assets(evidence, temporary, args.workers)
    print(json.dumps(export_final(output, temporary, evidence, results), ensure_ascii=False), flush=True)
