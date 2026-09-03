"""Read-only integration audit of a completed run, including raw and media hashes."""

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pr_crawler.store import read_document


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def audit(directory, run_id, require_media=False, require_review=False, require_issue=False):
    root = Path(directory).resolve()
    db = sqlite3.connect((root / "archive.sqlite3").as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        run = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        require(run is not None and run["status"] == "complete", "Run not complete")
        settings = json.loads(run["settings"])
        documents = {r["name"]: read_document(db, run_id, r["name"], r["data"]) for r in db.execute(
            "SELECT * FROM documents WHERE run_id=?", (run_id,))}
        responses = {r["id"]: dict(r) for r in db.execute("SELECT * FROM responses WHERE run_id=?", (run_id,))}
        for row in responses.values():
            require(hashlib.sha256(row["body"]).hexdigest() == row["sha256"], "Raw response hash mismatch")
            require("authorization" not in json.loads(row["headers"]), "Credential header persisted")
            require(bool(row["fetched_at"]), "Missing response timestamp")
        indexes = [documents["index/" + repo] for repo in settings["repos"]]
        for index in indexes:
            require(index["status"] == "complete", "Incomplete index")
            require(len({i["id"] for i in index["items"]}) == len(index["items"]), "Duplicate PR index ID")
            require(all(p["status"] == "complete" for p in index["passes"]), "Incomplete index pass")
        selection = documents["selection"]
        require(selection["status"] == "complete" and not selection["missing_requested_prs"], "Incomplete selection")
        records = [v for k, v in documents.items() if k.startswith("pr/")]
        expected = set() if settings["command"] == "index" else {(x["repo"], x["number"]) for x in selection["items"]}
        require(expected == {(x["repo"], x["number"]) for x in records}, "Selection/details mismatch")
        media_count, review_count, issue_count, details = 0, 0, 0, []
        for record in records:
            require(record["status"] == "complete", "Partial PR record")
            ids = record["provenance"]["response_ids"]
            require(ids and all(i in responses for i in ids), "Missing raw provenance")
            sections = record["sections"]
            required = {"pull_request", "labels", "comments", "reviews", "review_comments", "commits", "files",
                        "diff", "patch", "closing_issues", "linked_issues", "review_threads", "consistency", "assets"}
            require(required <= sections.keys(), "Missing required section")
            require(all(v["status"] == "complete" for k, v in sections.items() if k != "assets"), "Incomplete section")
            require(sections["assets"]["manifest_status"] == "complete", "Incomplete media manifest")
            material = sections["consistency"]["material_response_ids"]
            require(hashlib.sha256(json.dumps(material).encode()).hexdigest() ==
                    sections["consistency"]["material_fingerprint"], "Invalid material fingerprint")
            require(set(material) <= set(ids), "Missing material provenance")
            probe = responses[sections["consistency"]["end_pull_request"]["response_id"]]
            require(probe["id"] > max(material), "Boundary probe predates material")
            require(sections["consistency"]["material_fingerprint"] in probe["cache_key"], "Boundary probe not bound to material")
            asset_sizes = []
            for asset in sections["assets"]["items"]:
                if asset["status"] != "complete":
                    require(not settings.get("download_assets"), "Incomplete requested media")
                    continue
                path = (root / asset["local_path"]).resolve()
                require(path.is_relative_to(root / "assets"), "Unsafe asset path")
                data = path.read_bytes()
                require(len(data) == asset["bytes"], "Asset size mismatch")
                require(hashlib.sha256(data).hexdigest() == asset["sha256"], "Asset hash mismatch")
                media_count += 1
                asset_sizes.append(len(data))
            review_count += len(sections["review_comments"]["items"])
            issue_count += sum(i["kind"] == "issue" and i["relationship"] == "closes" for i in sections["linked_issues"]["items"])
            export = root / "exports" / run_id / "pr" / record["repo"] / f"{record['number']}.json"
            require(json.loads(export.read_text()) == record, "Stale normalized export")
            details.append({"repo": record["repo"], "number": record["number"], "raw_response_refs": len(ids),
                            "linked_issues": len(sections["linked_issues"]["items"]),
                            "reviews": len(sections["reviews"]["items"]),
                            "inline_comments": len(sections["review_comments"]["items"]),
                            "threads": len(sections["review_threads"]["items"]), "downloaded_asset_bytes": asset_sizes})
        require(not require_media or media_count > 0, "No verified media")
        require(not require_review or review_count > 0, "No inline review comments")
        require(not require_issue or issue_count > 0, "No GitHub-reported closing Issue")
        report = json.loads((root / "exports" / run_id / "report.json").read_text())
        require(report["run"]["status"] == "complete" and report["archived_detail_count"] == len(records), "Stale report")
        return {"run_id": run_id, "audit": "passed", "raw_responses_checked": len(responses),
                "historical_error_responses": sum(not r["reusable"] for r in responses.values()),
                "index_counts": {x["repo"]: len(x["items"]) for x in indexes},
                "index_pages": {x["repo"]: [p["pages"] for p in x["passes"]] for x in indexes},
                "details": details, "media_verified": media_count}
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--require-media", action="store_true")
    parser.add_argument("--require-review", action="store_true")
    parser.add_argument("--require-issue", action="store_true")
    args = parser.parse_args()
    print(json.dumps(audit(args.output, args.run, args.require_media, args.require_review, args.require_issue), indent=2))
