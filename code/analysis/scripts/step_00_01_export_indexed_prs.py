"""Export a date-bounded, provenance-bearing PR JSONL from a collector index run."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from pr_crawler.core import select
from pr_crawler.store import Store
def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_rows(documents: dict, run_id: str, start: str, end: str | None) -> tuple[list[dict], list[dict]]:
    indexes = sorted((value for key, value in documents.items() if key.startswith("index/")),
                     key=lambda value: value["repo"])
    if not indexes:
        raise ValueError("collector run contains no repository indexes")
    rows = []
    for index in indexes:
        for item in select(index.get("items", []), "created_at", start, end):
            row = dict(item)
            row["repo"] = index["repo"]
            row["source_run_id"] = run_id
            row["collection_index_status"] = index.get("status")
            row["collection_index_cutoff"] = index.get("cutoff")
            row["collection_index_observed_at"] = index.get("observed_at")
            rows.append(row)
    rows.sort(key=lambda item: (item["repo"], item["number"]))
    identities = [(item["repo"], item["number"]) for item in rows]
    if len(identities) != len(set(identities)):
        raise ValueError("collector indexes contain duplicate PR identities")
    provenance = [{
        "repo": index["repo"], "status": index.get("status"),
        "consistency": index.get("consistency"), "cutoff": index.get("cutoff"),
        "observed_at": index.get("observed_at"), "indexed_count": len(index.get("items", [])),
    } for index in indexes]
    return rows, provenance


def run(archive: Path, run_id: str, output: Path, start: str, end: str | None) -> dict:
    archive, output = archive.resolve(), output.resolve()
    if output.exists():
        raise ValueError(f"indexed PR export output exists: {output}")
    store = Store(archive)
    try:
        run_record = store.run(run_id)
        documents = store.documents(run_id)
    finally:
        store.close()
    rows, provenance = build_rows(documents, run_id, start, end)
    output.mkdir(parents=True)
    data = output / "00_01_indexed_prs.jsonl"
    data.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in rows))
    manifest = {
        "schema_version": "date-bounded-indexed-pr-export-v1",
        "status": ("complete" if run_record.get("status") == "complete"
                   and all(item["status"] == "complete" for item in provenance)
                   else "partial"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "archive": str(archive), "collector_run_id": run_id,
        "collector_run_status": run_record.get("status"),
        "axis": "created_at", "start_inclusive": start, "end_exclusive": end,
        "repositories": provenance, "row_count": len(rows),
        "data": data.name, "data_sha256": _sha(data),
        "boundary": ("Index export only: PR body media are not yet typed, availability-checked, "
                     "source-archived, merged-default-branch verified, or V3 classified."),
    }
    (output / "00_01_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--start", default="2025-01-01T00:00:00Z")
    parser.add_argument("--end")
    args = parser.parse_args()
    result = run(args.archive, args.run_id, args.output, args.start, args.end)
    print(json.dumps({"output": str(args.output.resolve()), "status": result["status"],
                      "row_count": result["row_count"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
