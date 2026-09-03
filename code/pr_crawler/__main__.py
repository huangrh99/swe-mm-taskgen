"""Internal collection CLI implementation used by ``report/run.py``."""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from .api import API, credential
from .core import collect_pr, index_repository, repository, select
from .store import Store, dumps, now


def atomic_text(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temp = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def report(store, run_id):
    run = store.run(run_id)
    documents = store.documents(run_id)
    indexes = [v for k, v in documents.items() if k.startswith("index/")]
    selection = documents.get("selection", {"items": []})
    records = [v for k, v in documents.items() if k.startswith("pr/")]
    counts = {}
    incomplete = []
    for record in records:
        counts[record["status"]] = counts.get(record["status"], 0) + 1
        for name, part in record["sections"].items():
            if part["status"] != "complete":
                incomplete.append({"repo": record["repo"], "number": record["number"], "section": name,
                                   "status": part["status"], "reason": part.get("reason", part.get("reasons"))})
    expected = 0 if run["settings"]["command"] == "index" else len(selection["items"])
    result = {"schema_version": 1, "run": run, "generated_at": now(),
              "indexes": [{k: v for k, v in x.items() if k != "items"} | {"count": len(x["items"])} for x in indexes],
              "selected_count": len(selection["items"]), "expected_detail_count": expected,
              "archived_detail_count": len(records), "record_status_counts": counts, "incomplete_sections": incomplete,
              "selection_status": selection.get("status", "not_requested"),
              "limitations": ["Observational snapshots, not atomic or historical reconstruction",
                              "Only API-visible resources; no benchmark test validation",
                              "Media are not mirrored unless download is requested"]}
    lines = ["# PR archive completeness", "", f"Run: `{run_id}`", f"Status: **{run['status']}**", "",
             f"Selected PRs: {result['selected_count']}; detail records: {len(records)}/{expected}", "",
             "## Repository indexes", ""]
    for idx in result["indexes"]:
        lines.append(f"- {idx['repo']}: {idx['count']} PRs, {idx['status']} ({idx['consistency']}); cutoff {idx['cutoff']}")
    lines += ["", "## Non-complete sections", ""]
    lines += [f"- {x['repo']}#{x['number']} / {x['section']}: {x['status']} — {x['reason'] or 'see normalized record'}" for x in incomplete] or ["None recorded. Check run and index status before interpreting this as complete."]
    lines += ["", "## Limitations", ""] + ["- " + x for x in result["limitations"]]
    destination = store.directory / "exports" / run_id
    atomic_text(destination / "report.json", json.dumps(result, ensure_ascii=False, indent=2))
    atomic_text(destination / "report.md", "\n".join(lines) + "\n")
    for name, value in documents.items():
        # Names are generated solely from validated repository identifiers/numbers.
        if name.startswith(("index/", "pr/")) or name == "selection":
            atomic_text(destination / (name + ".json"), json.dumps(value, ensure_ascii=False, indent=2))
    return result


def choose(indexes, settings):
    selected = []
    requested = set(settings.get("prs") or [])
    observed = set()
    for idx in indexes:
        for row in select(idx["items"], settings.get("axis", "created_at"), settings.get("start"), settings.get("end")):
            key = f"{idx['repo']}#{row['number']}"
            observed.add(key)
            if not requested or key in requested:
                selected.append({"repo": idx["repo"], "number": row["number"], "id": row["id"],
                                 "created_at": row.get("created_at"), "updated_at": row.get("updated_at"),
                                 "merged_at": row.get("merged_at")})
    missing = sorted(requested - observed)
    return {"items": selected, "status": "partial" if missing or any(x["status"] != "complete" for x in indexes) else "complete",
            "missing_requested_prs": missing, "parameters": {k: settings.get(k) for k in ("axis", "start", "end", "prs")}}


def execute(store, run_id, token=None):
    settings = store.run(run_id)["settings"]
    api = API(store, run_id, token)
    indexes = []
    for repo in settings["repos"]:
        if settings["command"] == "enrich":
            idx = store.get(settings["source_run"], "index/" + repo)
            if idx is None:
                raise ValueError("Source run does not contain required repository index")
            store.put(run_id, "index/" + repo, idx)
        else:
            idx = index_repository(api, repo, settings.get("page_workers", 1))
        indexes.append(idx)
        print(f"Index {repo}: {len(idx['items'])} ({idx['status']})", flush=True)
    selection = choose(indexes, settings)
    store.put(run_id, "selection", selection)
    statuses = [i["status"] for i in indexes] + [selection["status"]]
    if settings["command"] != "index":
        for item in selection["items"]:
            record = collect_pr(api, item["repo"], item["number"],
                                settings["download_assets"], settings["max_asset_bytes"],
                                settings.get("asset_workers", 1))
            statuses.append(record["status"])
            print(f"PR {item['repo']}#{item['number']}: {record['status']}", flush=True)
    status = "complete" if all(s == "complete" for s in statuses) else "partial"
    store.finish(run_id, status)
    report(store, run_id)
    return 0 if status == "complete" else 2


def parser():
    root = argparse.ArgumentParser(description="Read-only, resumable GitHub PR archives (Python standard library)")
    sub = root.add_subparsers(dest="command", required=True)
    for command in ("index", "crawl", "enrich", "resume", "select", "report"):
        p = sub.add_parser(command)
        p.add_argument("--output", required=True, help="Archive directory containing archive.sqlite3")
        if command in ("index", "crawl"):
            p.add_argument("repos", nargs="+", type=repository)
            p.add_argument("--page-workers", type=int, choices=(1, 2, 4, 8), default=1,
                           help="Bounded parallel index pages; independent checkpoints, default serial")
        if command == "enrich":
            p.add_argument("--source-run", required=True)
            p.add_argument("--pr", action="append", dest="prs", help="Optional bounded selection owner/repo#number; repeatable")
        if command in ("resume", "select", "report"):
            p.add_argument("--run", required=True)
        if command in ("crawl", "enrich", "select"):
            p.add_argument("--axis", choices=("created_at", "updated_at", "merged_at"), default="created_at")
            p.add_argument("--start")
            p.add_argument("--end")
        if command in ("crawl", "enrich"):
            p.add_argument("--download-assets", action="store_true")
            p.add_argument("--max-asset-bytes", type=int, default=20 * 1024 * 1024)
            p.add_argument("--asset-workers", type=int, choices=(1, 2, 4, 8), default=1,
                           help="Bounded credential-free asset downloads; cache writes stay serial")
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    settings = vars(args).copy()
    # Validate before creating archive state or making any network call.
    select([], settings.get("axis", "created_at"), settings.get("start"), settings.get("end"))
    if settings.get("max_asset_bytes", 1) <= 0:
        raise ValueError("max-asset-bytes must be positive")
    if settings.get("prs"):
        normalized = []
        for value in settings["prs"]:
            repo, number = value.rsplit("#", 1)
            if not number.isdecimal() or int(number) < 1:
                raise ValueError("PR selector must be owner/repo#positive-number")
            normalized.append(repository(repo) + "#" + str(int(number)))
        settings["prs"] = sorted(set(normalized))
    store = Store(args.output)
    run_id = None
    try:
        if args.command == "report":
            print(dumps(report(store, args.run)))
            return 0
        if args.command == "select":
            indexes = [v for k, v in store.documents(args.run).items() if k.startswith("index/")]
            store.run(args.run)
            if not indexes:
                raise ValueError("No persisted index; resume collection first")
            print(dumps(choose(indexes, settings)))
            return 0
        if args.command == "resume":
            run_id = args.run
            store.run(run_id)
        else:
            if args.command == "enrich":
                source = store.run(args.source_run)
                settings["repos"] = source["settings"]["repos"]
            else:
                settings["repos"] = sorted(set(args.repos))
            settings.pop("output", None)
            run_id = store.new_run(settings)
        print("Run ID: " + run_id, flush=True)
        return execute(store, run_id, credential())
    except KeyboardInterrupt:
        if run_id:
            store.finish(run_id, "interrupted")
            report(store, run_id)
            print("Checkpoint saved. Resume run " + run_id, file=sys.stderr)
        return 130
    finally:
        store.close()


if __name__ == "__main__":
    print("Unsupported direct entrypoint; use `python3 report/run.py collect ...`", file=sys.stderr)
    raise SystemExit(2)
