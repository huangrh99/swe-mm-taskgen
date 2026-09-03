"""Select a cross-repository linked-Issue probe without visual text heuristics."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path

from analysis.scripts.step_08_03_select_balanced_visual_recall import _issues
from report_pipeline.paths import WORKSPACE_ROOT


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _score(seed: str, value: str) -> str:
    return hashlib.sha256(f"{seed}\0{value}".encode()).hexdigest()


def _eligible(row: dict) -> tuple[bool, list[str]]:
    reasons = []
    if not row.get("merged_at"):
        reasons.append("not_merged")
    base = row.get("base") or {}
    default = (base.get("repo") or {}).get("default_branch")
    if not default or base.get("ref") != default:
        reasons.append("not_merged_to_collected_default_branch")
    if str(row.get("created_at", "")) < "2025-01-01T00:00:00Z":
        reasons.append("created_before_2025")
    issue_count = len(_issues(row))
    if issue_count == 0:
        reasons.append("no_statically_confirmed_issue")
    elif issue_count > 10:
        reasons.append("over_complex_issue_scope")
    return not reasons, reasons


def run(source: Path, excludes: list[Path], output: Path, limit: int,
        per_repo: int, seed: str) -> dict:
    if limit < 0 or per_repo < 1:
        raise ValueError("limit must be non-negative and per-repository quota positive")
    source = source.resolve(strict=True)
    excluded = set()
    for path in excludes:
        for line in Path(path).resolve(strict=True).read_text().splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            identity = item.get("pr_id") or f'{item["repo"]}#{item["number"]}'
            if identity in excluded:
                raise ValueError("duplicate PR across exclusion inputs")
            excluded.add(identity)

    rows, ledger, by_repo = [], [], defaultdict(list)
    with source.open() as stream:
        for line in stream:
            if not line.strip():
                continue
            row = json.loads(line)
            identity = f'{row["repo"]}#{row["number"]}'
            eligible, reasons = _eligible(row)
            entry = {"pr_id": identity, "repo": row["repo"], "number": row["number"],
                     "eligible": eligible, "reasons": reasons,
                     "linked_issues": [f"{repo}#{number}" for repo, number in sorted(_issues(row))],
                     "visual_text_heuristics_used": False,
                     "selection_route": "linked_issue_media_probe"}
            if identity in excluded:
                entry.update(eligible=False, reasons=["excluded_previous_identity"])
            if entry["eligible"]:
                by_repo[row["repo"]].append((row, entry))
            ledger.append(entry)

    selected = []
    ordered_repos = sorted(by_repo, key=lambda repo: _score(seed, repo))
    for repo in ordered_repos:
        by_repo[repo].sort(key=lambda item: _score(seed, item[1]["pr_id"]))
    if limit == 0:
        # Exhaustive mode is the production path: every source-qualified PR
        # with a statically linked Issue is archived so Issue media can be
        # observed before any visual-text heuristic is applied.
        selected = [item for repo in ordered_repos for item in by_repo[repo]]
    else:
        # A bounded, repository-balanced sample remains useful for smoke runs.
        for rank in range(per_repo):
            for repo in ordered_repos:
                if len(selected) >= limit:
                    break
                if rank < len(by_repo[repo]):
                    selected.append(by_repo[repo][rank])
            if len(selected) >= limit:
                break
    selected_ids = {entry["pr_id"] for _, entry in selected}
    for entry in ledger:
        entry["selected"] = entry["pr_id"] in selected_ids
    rows = [row for row, _ in selected]

    output = output.resolve()
    if output.exists():
        raise ValueError(f"output already exists: {output}")
    output.mkdir(parents=True)
    selected_path = output / "08_03_01_linked_issue_probe_prs.jsonl"
    ledger_path = output / "08_03_02_linked_issue_probe_ledger.jsonl"
    audit_path = output / "08_03_03_linked_issue_probe_audit.jsonl"
    selected_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
    ledger_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n"
                                   for row in ledger if row["selected"]))
    audit_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in ledger))
    manifest = {
        "schema_version": "linked-issue-media-probe-v2", "status": "complete",
        "selection_exhaustive": limit == 0, "semantic_qualification": False,
        "purpose": "discover Issue media before visual-semantic filtering",
        "source": str(source), "source_sha256": digest(source),
        "selected_file": selected_path.name, "selected_sha256": digest(selected_path),
        "selection_ledger": ledger_path.name,
        "selection_ledger_sha256": digest(ledger_path),
        "audit_file": audit_path.name, "audit_sha256": digest(audit_path),
        "selected_count": len(rows), "eligible_count": sum(bool(x["eligible"]) for x in ledger),
        "limit": limit,
        "per_repository_quota": None if limit == 0 else per_repo,
        "seed": seed,
        "repository_counts": dict(sorted(Counter(row["repo"] for row in rows).items())),
        "boundary": {
            "pr_visual_keywords_required": False,
            "issue_linked_prs_selected_before_media_observation": True,
            "linked_issue_media_not_yet_observed": True,
            "issue_media_presence_requires_stage11_archive": True,
            "no_selected_pr_is_yet_a_visual_candidate": True,
        },
    }
    manifest_path = output / "08_03_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--exclude", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Maximum selected PRs; 0 exhaustively selects every eligible linked-Issue PR.")
    parser.add_argument("--per-repo", type=int, default=15)
    parser.add_argument("--seed", default="linked-issue-media-probe-v1")
    args = parser.parse_args()
    result = run(args.source, args.exclude, args.output, args.limit, args.per_repo, args.seed)
    print(json.dumps({"output": str(args.output.resolve()),
                      "selected_count": result["selected_count"],
                      "eligible_count": result["eligible_count"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
