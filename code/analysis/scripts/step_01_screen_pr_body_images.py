"""Deterministic, offline PR-body image screening with stage-named outputs."""

import argparse
from collections import Counter, defaultdict
from contextlib import ExitStack
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import sys
import tempfile
from report_pipeline.paths import CODE_ROOT, WORKSPACE_ROOT

ROOT = WORKSPACE_ROOT
sys.path.insert(0, str(CODE_ROOT))
from pr_crawler.image_screening import RULE_VERSION, classify, discover_body
from pr_crawler.store import now

PARTITIONS = {
    "non_badge_image_evidence": "02_pr_body_non_badge_images/02_prs_with_non_badge_image_evidence.jsonl",
    "only_badge_or_decoration_image_evidence": "02_pr_body_non_badge_images/02_prs_with_only_badge_or_decoration_images.jsonl",
    "untyped_attachment_without_image_evidence": "03_pr_body_pending_media_types/03_prs_with_untyped_attachments_no_image_evidence.jsonl",
    "video_without_image_evidence": "03_pr_body_pending_media_types/03_prs_with_video_evidence_no_image_evidence.jsonl",
    "no_detected_media_in_pr_body": "03_pr_body_pending_media_types/03_pr_ids_without_detected_media.jsonl",
}
ALL_IMAGES = "01_pr_body_media_discovery/01_prs_with_image_evidence_including_badges.jsonl"
ALL_EVIDENCE = "01_pr_body_media_discovery/01_all_prs_media_evidence.jsonl"


def screen(source, output, temporary):
    source, output, temporary = Path(source).resolve(), Path(output).resolve(), Path(temporary).resolve()
    output.mkdir(parents=True, exist_ok=True)
    temporary.mkdir(parents=True, exist_ok=True)
    if temporary == output or output in temporary.parents:
        raise ValueError("Temporary files must be outside the result directory")
    names = list(PARTITIONS.values()) + [ALL_IMAGES, ALL_EVIDENCE]
    counts, repositories, years, asset_kinds, decorations = Counter(), defaultdict(Counter), defaultdict(Counter), Counter(), Counter()
    unique_assets, identities, source_hash = set(), set(), hashlib.sha256()
    with tempfile.TemporaryDirectory(prefix="body-screening-", dir=temporary) as staging, ExitStack() as stack:
        paths = {name: Path(staging) / name for name in names}
        for path in paths.values():
            path.parent.mkdir(parents=True, exist_ok=True)
        streams = {name: stack.enter_context(path.open("w", encoding="utf-8")) for name, path in paths.items()}
        def write(name, row):
            streams[name].write(json.dumps(row, ensure_ascii=False) + "\n")
        with source.open("rb") as stream:
            for number, raw in enumerate(stream, 1):
                source_hash.update(raw)
                row = json.loads(raw)
                identity = (row["repo"], row["number"])
                if identity in identities:
                    raise ValueError("Duplicate input PR identity")
                identities.add(identity)
                body = row.get("body") or ""
                assets = discover_body(body)
                category = classify(assets)
                evidence = {"rule_version": RULE_VERSION, "category": category,
                    "body_sha256": hashlib.sha256(body.encode()).hexdigest(), "assets": assets,
                    "source_coverage": {"pr_body": "scanned", "linked_issues": "not_collected", "pr_comments": "not_collected", "test_files": "not_collected"},
                    "visual_required": "not_assessed", "network_validation": "not_performed"}
                compact = {"repo": row["repo"], "number": row["number"], "id": row["id"], "html_url": row.get("html_url"),
                    "created_at": row["created_at"], "input_line": number, "source_run_id": row.get("source_run_id"), "image_screening": evidence}
                full = {**row, "image_screening": evidence}
                write(ALL_EVIDENCE, compact)
                write(PARTITIONS[category], compact if category == "no_detected_media_in_pr_body" else full)
                if any(a["media_kind"] == "image" for a in assets):
                    write(ALL_IMAGES, full)
                    counts["all_image_evidence_including_badges"] += 1
                counts[category] += 1
                counts["input_prs"] += 1
                repositories[row["repo"]][category] += 1
                years[row["created_at"][:4]][category] += 1
                for asset in assets:
                    asset_kinds[asset["media_kind"]] += 1
                    unique_assets.add(asset["asset_id"])
                    if asset["decoration_reason"]:
                        decorations[asset["decoration_reason"]] += 1
        for stream in streams.values():
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
        hashes = {}
        for name, path in paths.items():
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            hashes[name] = {"sha256": digest.hexdigest(), "bytes": path.stat().st_size}
            target = output / name
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(path, target)
        summary = {"rule_version": RULE_VERSION, "generated_at": now(), "input": str(source), "input_sha256": source_hash.hexdigest(),
            "scope": "PR body only; image references, not proven availability or visual necessity", "temporary_directory": str(temporary),
            "parser": {"name": "markdown-it-py", "version": version("markdown-it-py")}, "counts": dict(counts),
            "repositories": dict(sorted(repositories.items())), "years": dict(sorted(years.items())),
            "asset_occurrences_by_kind": dict(asset_kinds), "unique_asset_urls": len(unique_assets),
            "decoration_reasons": dict(decorations), "outputs": hashes}
        summary_path = Path(staging) / "summary.json"
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
        os.replace(summary_path, output / "00_pr_body_image_screening_summary.json")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=str(ROOT / "crawler-output/multimodal-2025/prs_2025_plus.jsonl"))
    parser.add_argument("--output", default=str(ROOT / "crawler-output/multimodal-2025/image-screening"))
    parser.add_argument("--tmp", default=str(ROOT / "tmp/multimodal-2025/02_pr_body_image_screening"))
    args = parser.parse_args()
    print(json.dumps(screen(args.input, args.output, args.tmp), ensure_ascii=False), flush=True)
