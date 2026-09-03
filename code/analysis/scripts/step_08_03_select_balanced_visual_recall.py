"""Select a deterministic dual-source recall batch before archival/VLM review.

The assigned bucket is only a collection heuristic. It is never copied into the
V3 classification result and cannot satisfy a category quota by itself.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from report_pipeline.paths import WORKSPACE_ROOT


SOURCE = (WORKSPACE_ROOT / "crawler-output/multimodal-2025/image-screening/"
          "06_merged_default_branch_images/"
          "06_prs_with_non_badge_images_merged_to_default_branch.jsonl")
BUCKETS = (
    "外观与渲染属性理解",
    "空间布局与几何理解",
    "元素结构与视觉状态理解",
    "动态交互与时序理解",
    "图形符号与领域语义理解",
    "混合视觉能力",
)
PATTERNS = {
    BUCKETS[0]: re.compile(
        r"\b(?:appearance|visual|colo(?:u)?r|font|opacity|shadow|border|theme|"
        r"dark mode|light mode|stroke|fill|background|gradient|contrast|style)\b", re.I),
    BUCKETS[1]: re.compile(
        r"\b(?:layout|align(?:ment|ed)?|spacing|margin|padding|overlap|position|"
        r"width|height|responsive|wrap|overflow|crop|zoom|geometry)\b", re.I),
    BUCKETS[2]: re.compile(
        r"\b(?:menu|dialog|modal|button|card|panel|sidebar|header|footer|tooltip|"
        r"dropdown|icon|selected|disabled|focus|hover|empty state)\b", re.I),
    BUCKETS[3]: re.compile(
        r"\b(?:animation|transition|drag(?:ging)?|drop|scroll|swipe|gesture|"
        r"video|gif|interaction|playback|carousel|seek|panning|hovering)\b", re.I),
    BUCKETS[4]: re.compile(
        r"\b(?:chart|graph|map|geospatial|canvas|svg|diagram|axis|legend|marker|"
        r"route|coordinate|projection|bpmn|syntax highlight|flowchart|sankey|"
        r"pie|gantt|journey|arrow|connector|viewport|cull(?:ing)?)\b", re.I),
}
MAINTENANCE = re.compile(
    r"^(?:docs?|test(?:\([^)]*\))?|chore\(deps\)|build\(deps\)|ci)(?:\s*[:(])|^bump\b",
    re.I,
)
DESIGN = re.compile(
    r"\b(?:redesign|new design|design update|update [^\n]{0,60} design|"
    r"design [^\n]{0,30} update|match (?:the )?design|figma|visual refresh|"
    r"visual overhaul|visual tweaks?|new ui|ui update|revamp|refinements?|"
    r"restyle|rework|ui improvements?)\b", re.I)
ISSUE_URL = re.compile(r"https://(?:redirect\.)?github\.com/([\w.-]+/[\w.-]+)/issues/(\d+)\b", re.I)
CLOSING = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s*"
    r"(?:(?P<repo>[\w.-]+/[\w.-]+))?#(?P<number>\d+)\b", re.I)
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _identity(row: dict) -> str:
    return f"{row['repo']}#{row['number']}"


def _issues(row: dict) -> set[tuple[str, int]]:
    text = (row.get("title") or "") + "\n" + (row.get("body") or "")
    values = {(repo.lower(), int(number)) for repo, number in ISSUE_URL.findall(text)}
    for match in CLOSING.finditer(text):
        values.add(((match.group("repo") or row["repo"]).lower(),
                    int(match.group("number"))))
    values.discard((row["repo"].lower(), int(row["number"])))
    return values


def _image_count(row: dict) -> int:
    return sum(
        item.get("media_kind") == "image" and not item.get("decoration_reason")
        for item in (row.get("image_screening") or {}).get("assets", []))


def _source_eligible(row: dict) -> tuple[bool, list[str]]:
    reasons = []
    if MAINTENANCE.search((row.get("title") or "").strip()):
        reasons.append("maintenance_only_title")
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


def _signal(row: dict, bucket: str) -> dict:
    title = row.get("title") or ""
    body = row.get("body") or ""
    atomic_title = {name: len(pattern.findall(title)) for name, pattern in PATTERNS.items()}
    atomic_body = {name: min(5, len(pattern.findall(body)))
                   for name, pattern in PATTERNS.items()}
    design = len(DESIGN.findall(title + "\n" + body))
    title_design = bool(DESIGN.search(title))
    title_contributing = sum(bool(atomic_title[name]) for name in PATTERNS)
    text = title + "\n" + body
    paired = bool(re.search(r"(?is)\bbefore\b.{0,1200}\bafter\b|"
                            r"\b(?:expected|desired)\b.{0,1200}\b(?:actual|current)\b", text))
    compositional = paired and (
        title_contributing >= 2 or (title_contributing >= 1 and
                                    sum(bool(atomic_title[name] or atomic_body[name])
                                        for name in PATTERNS) >= 3))
    if bucket == BUCKETS[-1]:
        contributing = sum(bool(atomic_title[name] or atomic_body[name]) for name in PATTERNS)
        eligible_signal = (title_design or (bool(design) and title_contributing >= 2)
                           or compositional)
        strength = (12 * title_design + 5 * min(2, design)
                    + 4 * min(3, contributing) + 6 * compositional)
    else:
        eligible_signal = bool(atomic_title[bucket]) or (
            atomic_body[bucket] >= 2 and paired)
        strength = 10 * atomic_title[bucket] + atomic_body[bucket]
    strength = strength + 3 * paired + min(3, _image_count(row)) if eligible_signal else 0
    return {
        "eligible_recall_signal": eligible_signal,
        "strength": strength,
        "title_match_count": atomic_title.get(bucket, 0),
        "body_match_count_capped": atomic_body.get(bucket, 0),
        "design_signal_count_capped": min(3, design),
        "atomic_signal_count": sum(bool(atomic_title[name] or atomic_body[name])
                                   for name in PATTERNS),
        "before_after_or_expected_actual": paired,
        "compositional_multi_signal": compositional,
        "image_count_capped": min(5, _image_count(row)),
        "source_route_recall": ("pr_or_issue_image" if _image_count(row)
                                else "issue_probe_required"),
        "direct_issue_count": len(_issues(row)),
    }


def _rank(row: dict, bucket: str, seed: str) -> tuple:
    signal = _signal(row, bucket)
    tie = hashlib.sha256(f"{seed}:{bucket}:{_identity(row)}".encode()).hexdigest()
    return (-signal["strength"], -signal["title_match_count"],
            -signal["before_after_or_expected_actual"], -signal["image_count_capped"], tie)


def _read_rows(path: Path) -> list[tuple[dict, bytes]]:
    rows = []
    with path.open("rb") as stream:
        for line in stream:
            if line.strip():
                rows.append((json.loads(line), line))
    identities = [_identity(row) for row, _ in rows]
    if len(identities) != len(set(identities)):
        raise ValueError("source contains duplicate PR identities")
    return rows


def _excluded_ids(paths: list[Path]) -> set[str]:
    values = set()
    for path in paths:
        for row, _ in _read_rows(path):
            values.add(_identity(row))
    return values


def _excluded_repositories(values: tuple[str, ...] | list[str]) -> set[str]:
    repositories = set()
    for value in values:
        normalized = value.strip().lower()
        if not REPOSITORY.fullmatch(normalized):
            raise ValueError(f"invalid excluded repository: {value!r}")
        repositories.add(normalized)
    return repositories


def _excluded_category_audit_ids(paths: list[Path]) -> set[str]:
    identities = set()
    for supplied in paths:
        path = supplied.resolve(strict=True)
        value = json.loads(path.read_text())
        if value.get("schema_version") != "visual-category-distribution-v3":
            raise ValueError(f"unsupported category audit: {path}")
        for row in value.get("rows", []):
            qualification = row.get("source_qualification") or {}
            source_result = Path(qualification.get("source_result", ""))
            if not source_result.is_absolute():
                source_result = WORKSPACE_ROOT / source_result
            source_result = source_result.resolve(strict=True)
            if qualification.get("source_result_sha256") != _sha(source_result):
                raise ValueError(f"category audit source result changed: {source_result}")
            result = json.loads(source_result.read_text())
            pr_id = result.get("pr_id")
            if not isinstance(pr_id, str) or "#" not in pr_id:
                raise ValueError(f"category audit source result lacks PR identity: {source_result}")
            identities.add(pr_id)
    return identities


def select(rows: list[tuple[dict, bytes]], excluded: set[str], quota: int,
           seed: str, max_per_repo_per_bucket: int = 3,
           buckets: tuple[str, ...] = BUCKETS,
           excluded_repos: set[str] | None = None,
           ) -> tuple[list[tuple[dict, bytes, str, dict]], dict]:
    if quota < 1:
        raise ValueError("quota must be positive")
    if max_per_repo_per_bucket < 1:
        raise ValueError("max per repository per bucket must be positive")
    if (not buckets or len(buckets) != len(set(buckets))
            or any(bucket not in BUCKETS for bucket in buckets)):
        raise ValueError("target recall buckets must be a unique nonempty subset")
    excluded_repos = {repo.lower() for repo in (excluded_repos or set())}
    eligible = [(row, line) for row, line in rows
                if (_identity(row) not in excluded
                    and row["repo"].lower() not in excluded_repos
                    and _source_eligible(row)[0])]
    ranked = {bucket: sorted(
        [(row, line) for row, line in eligible
         if _signal(row, bucket)["eligible_recall_signal"]],
        key=lambda item: _rank(item[0], bucket, seed)) for bucket in buckets}
    chosen, used, counts, repository_counts = [], set(), Counter(), Counter()
    while any(counts[bucket] < quota for bucket in buckets):
        open_buckets = [bucket for bucket in buckets if counts[bucket] < quota]
        open_buckets.sort(key=lambda bucket: sum(
            _identity(row) not in used for row, _ in ranked[bucket]))
        progressed = False
        for bucket in open_buckets:
            candidate = next(((row, line) for row, line in ranked[bucket]
                              if (_identity(row) not in used
                                  and repository_counts[(bucket, row["repo"])]
                                  < max_per_repo_per_bucket)), None)
            if candidate is None:
                continue
            row, line = candidate
            used.add(_identity(row))
            counts[bucket] += 1
            repository_counts[(bucket, row["repo"])] += 1
            chosen.append((row, line, bucket, _signal(row, bucket)))
            progressed = True
        if not progressed:
            break
    diagnostics = {
        "source_eligible_count": len(eligible),
        "excluded_repository_source_count": sum(
            row["repo"].lower() in excluded_repos for row, _ in rows),
        "candidate_counts_before_deduplication": {
            bucket: len(ranked[bucket]) for bucket in buckets},
        "selected_counts": {bucket: counts[bucket] for bucket in buckets},
        "selected_repository_counts": {
            bucket: dict(sorted((repo, count) for (assigned, repo), count
                                in repository_counts.items() if assigned == bucket))
            for bucket in buckets},
        "deficits": {bucket: max(0, quota - counts[bucket]) for bucket in buckets},
    }
    return chosen, diagnostics


def run(source: Path, excludes: list[Path], output: Path, quota: int, seed: str,
        max_per_repo_per_bucket: int = 3,
        buckets: tuple[str, ...] = BUCKETS,
        exclude_repos: tuple[str, ...] | list[str] = (),
        exclude_category_audits: tuple[Path, ...] | list[Path] = ()) -> dict:
    source, output = source.resolve(), output.resolve()
    if output.exists():
        raise ValueError(f"output already exists: {output}")
    rows = _read_rows(source)
    excluded = _excluded_ids([path.resolve() for path in excludes])
    audit_excluded = _excluded_category_audit_ids(list(exclude_category_audits))
    excluded.update(audit_excluded)
    excluded_repositories = _excluded_repositories(exclude_repos)
    chosen, diagnostics = select(rows, excluded, quota, seed,
                                 max_per_repo_per_bucket, buckets,
                                 excluded_repositories)
    output.mkdir(parents=True)
    selected = output / "08_03_selected_balanced_recall_prs.jsonl"
    selected.write_bytes(b"".join(line for _, line, _, _ in chosen))
    ledger = output / "08_03_selection_ledger.jsonl"
    ledger.write_text("".join(json.dumps({
        "position": position,
        "pr_id": _identity(row),
        "repository": row["repo"],
        "assigned_recall_bucket": bucket,
        "recall_only_not_v3_classification": True,
        "signals": signal,
        "source_line_sha256": hashlib.sha256(line).hexdigest(),
    }, ensure_ascii=False) + "\n" for position, (row, line, bucket, signal)
        in enumerate(chosen, 1)))
    manifest = {
        "schema_version": "balanced-visual-recall-selection-v1",
        "status": "complete" if not any(diagnostics["deficits"].values()) else "partial",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "purpose": "recall-only six-bucket expansion before source archival and V3 VLM classification",
        "source": str(source), "source_sha256": _sha(source), "source_count": len(rows),
        "excluded_sources": [{"path": str(path.resolve()), "sha256": _sha(path.resolve())}
                             for path in excludes],
        "excluded_category_audits": [
            {"path": str(path.resolve()), "sha256": _sha(path.resolve())}
            for path in exclude_category_audits],
        "excluded_category_audit_identity_count": len(audit_excluded),
        "excluded_identity_count": len(excluded),
        "excluded_repositories": sorted(excluded_repositories),
        "quota_per_recall_bucket": quota,
        "target_recall_buckets": list(buckets),
        "max_per_repository_per_recall_bucket": max_per_repo_per_bucket,
        "selected_file": selected.name, "selected_sha256": _sha(selected),
        "selection_ledger": ledger.name, "selection_ledger_sha256": _sha(ledger),
        "selected_count": len(chosen), "seed": seed,
        **diagnostics,
        "boundary": ("repository exclusions apply only to this recall run and never delete existing candidates; "
                     "assigned_recall_bucket is not a V3 label, admission decision, or quota count; "
                     "a PR image is not required because linked Issues may carry the visual problem; "
                     "only source-qualified image-role plus V3 output may enter the six final buckets"),
    }
    (output / "08_03_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--exclude", type=Path, action="append", default=[])
    parser.add_argument(
        "--exclude-category-audit", type=Path, action="append", default=[],
        help="Repeat to exclude every hash-bound PR already present in a V3 category audit.")
    parser.add_argument(
        "--exclude-repo", action="append", default=[], metavar="OWNER/REPO",
        help="Repeat to prevent new recall from a repository; existing candidates are untouched.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--quota", type=int, default=12)
    parser.add_argument("--seed", default="six-bucket-recall-v1")
    parser.add_argument("--max-per-repo-per-bucket", type=int, default=3)
    parser.add_argument("--bucket", action="append", choices=BUCKETS,
                        help="Repeat to target only current deficit buckets")
    args = parser.parse_args()
    result = run(args.source, args.exclude, args.output, args.quota, args.seed,
                 args.max_per_repo_per_bucket,
                 tuple(args.bucket) if args.bucket else BUCKETS,
                 args.exclude_repo, args.exclude_category_audit)
    print(json.dumps({"output": str(args.output.resolve()), "status": result["status"],
                      "selected_count": result["selected_count"],
                      "counts": result["selected_counts"],
                      "deficits": result["deficits"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
