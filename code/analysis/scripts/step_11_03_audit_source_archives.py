"""Validate Stage-11 archives and classify technical readiness in code."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

from pr_crawler.assets import retryable


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _linked_issue_quality(section: dict) -> tuple[bool, list[str]]:
    """Separate required Issue failures from curator-only reference gaps."""
    required_failure = False
    optional_failures = []
    items = section.get("items") or []
    for item in items:
        required = item.get("required_for_source_complete")
        if required is None:
            required = (item.get("relationship") == "closes"
                        and item.get("confidence") == "github_reported")
        names = ["detail", "comments", "labels", "timeline"]
        if "pull_metadata" in item:
            names.append("pull_metadata")
        incomplete = any(
            isinstance(item.get(name), dict)
            and item[name].get("status") not in {None, "complete", "not_required"}
            for name in names
        )
        if not incomplete:
            continue
        identity = f'{item.get("repo", "unknown")}#{item.get("number", "unknown")}'
        if required:
            required_failure = True
        else:
            optional_failures.append(identity)
    # A legacy partial section with no item-level explanation remains blocking.
    unexplained_partial = section.get("status") not in {None, "complete"} and not (
        required_failure or optional_failures)
    return required_failure or unexplained_partial, sorted(set(optional_failures))


def _audit_one(directory: Path) -> dict:
    directory = directory.resolve()
    manifest_path = directory / "11_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("status") not in {"complete", "partial"}:
        raise ValueError(f"unfinished Stage-11 archive: {directory}")
    source = directory / "11_source_prs.jsonl"
    if digest(source) != manifest.get("source_sha256"):
        raise ValueError(f"Stage-11 source hash mismatch: {directory}")
    files = sorted(directory.glob("11_record_*.json"))
    if len(files) != manifest.get("records") or set(manifest.get("files", {})) != {
            path.name for path in files}:
        raise ValueError(f"Stage-11 record inventory mismatch: {directory}")

    rows, identities = [], []
    for path in files:
        if digest(path) != manifest["files"].get(path.name):
            raise ValueError(f"Stage-11 record hash mismatch: {path}")
        record = json.loads(path.read_text())
        identity = f'{record["repo"]}#{record["number"]}'
        identities.append(identity)
        sections = record.get("sections") or {}
        source_failures = []
        optional_reference_failures = []
        for name, section in sections.items():
            if name == "assets" or not isinstance(section, dict):
                continue
            if name == "linked_issues":
                blocking, optional_reference_failures = _linked_issue_quality(section)
                if blocking:
                    source_failures.append(name)
            elif section.get("status") not in {None, "complete"}:
                source_failures.append(name)
        source_failures.sort()
        assets = (sections.get("assets") or {}).get("items") or []
        retryable_assets = [asset.get("url") for asset in assets if retryable(asset)]
        unavailable_assets = [asset.get("url") for asset in assets
                              if asset.get("status") != "complete" and not retryable(asset)]
        if source_failures or retryable_assets:
            decision = "retry_required"
        elif unavailable_assets:
            decision = "ready_with_media_gaps"
        else:
            decision = "ready_for_image_verifier"
        hashes = Counter(asset.get("sha256") for asset in assets
                         if asset.get("status") == "complete" and asset.get("sha256"))
        rows.append({
            "pr_id": identity,
            "record": path.name,
            "record_sha256": digest(path),
            "archive_status": record.get("status"),
            "automatic_decision": decision,
            "source_failures": source_failures,
            "optional_reference_failures": optional_reference_failures,
            "retryable_asset_urls": retryable_assets,
            "unavailable_asset_urls": unavailable_assets,
            "asset_counts": dict(sorted(Counter(
                asset.get("status", "missing") for asset in assets).items())),
            "duplicate_content_groups": sum(count > 1 for count in hashes.values()),
            "semantic_rejection": False,
        })
    if len(identities) != len(set(identities)) or set(identities) != set(manifest["pr_ids"]):
        raise ValueError(f"Stage-11 PR identity mismatch: {directory}")
    return {
        "archive": str(directory),
        "manifest_sha256": digest(manifest_path),
        "counts": dict(sorted(Counter(row["automatic_decision"] for row in rows).items())),
        "rows": rows,
    }


def run(archives, output: Path) -> dict:
    audited = [_audit_one(Path(archive)) for archive in archives]
    rows = [row for archive in audited for row in archive["rows"]]
    identities = [row["pr_id"] for row in rows]
    if len(identities) != len(set(identities)):
        raise ValueError("duplicate PR across Stage-11 archive waves")
    counts = dict(sorted(Counter(row["automatic_decision"] for row in rows).items()))
    result = {
        "schema_version": "stage11-archive-quality-v1",
        "status": "complete" if not counts.get("retry_required") else "partial",
        "automatic_decision": ("ready_for_image_verifier"
                               if not counts.get("retry_required")
                               else "retry_required"),
        "not_a_semantic_qualification": True,
        "archive_count": len(audited),
        "record_count": len(rows),
        "counts": counts,
        "archives": audited,
    }
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.archive, args.output)
    print(json.dumps({"output": str(args.output.resolve()), "status": result["status"],
                      "counts": result["counts"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
