"""Select a deterministic, cross-repository batch for visual verification."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from report_pipeline.paths import WORKSPACE_ROOT


ROOT = WORKSPACE_ROOT
DEFAULT_SOURCE = (ROOT / 'crawler-output/multimodal-2025/image-screening/'
                  '06_merged_default_branch_images/'
                  '06_prs_with_non_badge_images_merged_to_default_branch.jsonl')
DEFAULT_OUTPUT = ROOT / 'crawler-output/multimodal-2025/image-screening/08_02_candidate_pool'
DEFAULT_QUOTAS = {
    'automattic/wp-calypso': 9,
    'carbon-design-system/carbon': 11,
    'bpmn-io/bpmn-js': 8,
    'eslint/eslint': 8,
    'grommet/grommet': 8,
    'googlechrome/lighthouse': 8,
    'openlayers/openlayers': 8,
    'prettier/prettier': 8,
    'processing/p5.js': 7,
    'chartjs/chart.js': 7,
    'diegomura/react-pdf': 6,
    'highlightjs/highlight.js': 7,
    'markedjs/marked': 1,
    'prismjs/prism': 4,
}
ISSUE = re.compile(
    r'(?i)(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?|related\s+to)\s*:?#?\s*\d+'
    r'|github\.com/[^/\s]+/[^/\s]+/issues/\d+'
)
VISUAL = re.compile(
    r'(?i)\b(?:visual|screenshot|render|layout|overlap|align(?:ment|ed)?|spacing|'
    r'colou?r|icon|chart|graph|map|canvas|svg|ui|ux|pixel|responsive|style|display)\b'
)
ISSUE_URL_REFERENCE = re.compile(
    r'https://(?:redirect\.)?github\.com/([\w.-]+/[\w.-]+)/issues/(\d+)\b', re.I)
CLOSING_ISSUE_REFERENCE = re.compile(
    r'(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s*'
    r'(?:(?P<repo>[\w.-]+/[\w.-]+))?#(?P<number>\d+)\b'
)
MAX_ASSOCIATED_ISSUES = 10


def digest_bytes(value):
    return hashlib.sha256(value).hexdigest()


def identity(row):
    return f"{row['repo']}#{row['number']}"


def associated_issues(row):
    """Return statically confirmed direct Issue references before archival.

    Ordinary ``#N`` and ``owner/repo#N`` references are deliberately excluded:
    without resolving GitHub objects they may denote PRs or document anchors.
    """
    text = (row.get('title') or '') + '\n' + (row.get('body') or '')
    values = {(repo.lower(), int(number))
              for repo, number in ISSUE_URL_REFERENCE.findall(text)}
    for match in CLOSING_ISSUE_REFERENCE.finditer(text):
        repo = match.group('repo') or row['repo']
        values.add((repo.lower(), int(match.group('number'))))
    values.discard((row['repo'].lower(), row['number']))
    return values


def signals(row):
    title = row.get('title') or ''
    body = row.get('body') or ''
    text = title + '\n' + body
    lowered = text.lower()
    images = [asset for asset in row['image_screening']['assets']
              if asset.get('media_kind') == 'image' and not asset.get('decoration_reason')]
    return {
        'linked_issue_reference': bool(ISSUE.search(text)),
        'before_after_pair': 'before' in lowered and 'after' in lowered,
        'expected_actual_pair': 'expected' in lowered and 'actual' in lowered,
        'visual_term_in_title': bool(VISUAL.search(title)),
        'visual_term_count_capped': min(5, len(VISUAL.findall(text))),
        'image_count_capped': min(3, len(images)),
        'substantive_body': 120 <= len(body) <= 12000,
        'direct_issue_reference_count': len(associated_issues(row)),
        'temporarily_excluded_over_complex': len(associated_issues(row)) > MAX_ASSOCIATED_ISSUES,
    }


def rank(row, seed):
    value = signals(row)
    deterministic_tie = digest_bytes(f'{seed}:{identity(row)}'.encode())
    return (
        -int(value['linked_issue_reference']),
        -int(value['before_after_pair']),
        -int(value['expected_actual_pair']),
        -int(value['visual_term_in_title']),
        -value['visual_term_count_capped'],
        -value['image_count_capped'],
        -int(value['substantive_body']),
        deterministic_tie,
    )


def load_rows(source):
    rows = []
    with Path(source).open('rb') as stream:
        for line in stream:
            if line.strip():
                rows.append((json.loads(line), line))
    identities = [identity(row) for row, _ in rows]
    if len(identities) != len(set(identities)):
        raise ValueError('Duplicate PR in source')
    return rows


def load_kept_ids(archive_runs):
    values = []
    for run in archive_runs:
        manifest = json.loads((Path(run) / '11_manifest.json').read_text())
        values.extend(manifest['pr_ids'])
    if len(values) != len(set(values)):
        raise ValueError('Duplicate PR across keep archives')
    return values


def select(source, quotas, kept_ids, seed):
    rows = load_rows(source)
    by_id = {identity(row): (row, line) for row, line in rows}
    missing = set(kept_ids) - set(by_id)
    if missing:
        raise ValueError('Kept PR absent from source: ' + ', '.join(sorted(missing)))
    unknown = {row['repo'] for row, _ in rows} - set(quotas)
    if unknown:
        raise ValueError('Source contains repositories without quotas: ' + ', '.join(sorted(unknown)))
    eligible_kept = [item for item in kept_ids
                     if len(associated_issues(by_id[item][0])) <= MAX_ASSOCIATED_ISSUES]
    selected = []
    kept_by_repo = Counter(by_id[item][0]['repo'] for item in eligible_kept)
    for repo, quota in quotas.items():
        if kept_by_repo[repo] > quota:
            raise ValueError(f'Quota below kept count for {repo}')
        kept = [by_id[item] for item in eligible_kept if by_id[item][0]['repo'] == repo]
        candidates = [(row, line) for row, line in rows
                      if row['repo'] == repo and identity(row) not in set(eligible_kept)
                      and len(associated_issues(row)) <= MAX_ASSOCIATED_ISSUES]
        candidates.sort(key=lambda item: rank(item[0], seed))
        chosen = kept + candidates[:quota - len(kept)]
        if len(chosen) != quota:
            raise ValueError(f'Insufficient candidates for {repo}: {len(chosen)}/{quota}')
        selected.extend(chosen)
    if len(selected) != sum(quotas.values()):
        raise ValueError('Selected count does not match quota total')
    return selected


def run(source, output_root, archive_runs, quotas=DEFAULT_QUOTAS, seed='visual-candidate-100-v1'):
    if sum(quotas.values()) != 100:
        raise ValueError('The formal expansion batch must contain exactly 100 PRs')
    kept_ids = load_kept_ids(archive_runs)
    selected = select(source, quotas, kept_ids, seed)
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    output = Path(output_root).resolve() / run_id
    output.mkdir(parents=True, exist_ok=False)
    source_bytes = Path(source).read_bytes()
    selected_path = output / '08_02_selected_100_prs.jsonl'
    selected_path.write_bytes(b''.join(line for _, line in selected))
    kept = set(kept_ids)
    ledger = []
    for position, (row, line) in enumerate(selected, 1):
        ledger.append({
            'position': position,
            'pr_id': identity(row),
            'repository': row['repo'],
            'kept_from_previous_28': identity(row) in kept,
            'signals': signals(row),
            'source_line_sha256': digest_bytes(line),
        })
    ledger_path = output / '08_02_selection_ledger.jsonl'
    ledger_path.write_text(''.join(json.dumps(item, ensure_ascii=False) + '\n' for item in ledger))
    selected_ids = {identity(row) for row, _ in selected}
    complexity_exclusions = [{
        'pr_id': identity(row),
        'repository': row['repo'],
        'direct_issue_reference_count': len(associated_issues(row)),
        'reason_code': 'temporarily_excluded_over_complex',
        'threshold': MAX_ASSOCIATED_ISSUES,
        'previously_reviewed': identity(row) in kept,
        'would_otherwise_be_selected': identity(row) in kept_ids and identity(row) not in selected_ids,
        'source_line_sha256': digest_bytes(line),
    } for row, line in load_rows(source)
        if len(associated_issues(row)) > MAX_ASSOCIATED_ISSUES]
    exclusions_path = output / '08_02_complexity_exclusions.jsonl'
    exclusions_path.write_text(''.join(json.dumps(item, ensure_ascii=False) + '\n'
                                       for item in complexity_exclusions))
    manifest = {
        'schema_version': 'cross-repo-candidate-selection-v2',
        'status': 'complete',
        'purpose': 'high-recall input for visual and text-only verifier stages; not benchmark admission',
        'source': str(Path(source).resolve()),
        'source_sha256': digest_bytes(source_bytes),
        'source_count': len(load_rows(source)),
        'selected_file': selected_path.name,
        'selected_sha256': digest_bytes(selected_path.read_bytes()),
        'selection_ledger': ledger_path.name,
        'selection_ledger_sha256': digest_bytes(ledger_path.read_bytes()),
        'complexity_exclusions': exclusions_path.name,
        'complexity_exclusions_sha256': digest_bytes(exclusions_path.read_bytes()),
        'complexity_exclusion_count': len(complexity_exclusions),
        'complexity_rule': {'maximum_direct_issue_references': MAX_ASSOCIATED_ISSUES,
                            'counting_boundary': ('statically confirmed /issues/N URLs and '
                                                  'Fixes/Closes/Resolves references only'),
                            'exclusion_reason': 'temporarily_excluded_over_complex'},
        'selected_count': len(selected),
        'previously_reviewed_count': sum(item['kept_from_previous_28'] for item in ledger),
        'new_count': sum(not item['kept_from_previous_28'] for item in ledger),
        'seed': seed,
        'repository_quotas': quotas,
        'repository_counts': dict(Counter(row['repo'] for row, _ in selected)),
        'keep_archive_runs': [str(Path(run).resolve()) for run in archive_runs],
        'selection_boundary': 'Static recall ranking only; model and human decisions happen later.',
    }
    manifest_path = output / '08_02_manifest.json'
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=DEFAULT_SOURCE)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--keep-archive-run', type=Path, action='append', default=[])
    parser.add_argument('--seed', default='visual-candidate-100-v1')
    args = parser.parse_args()
    print(run(args.source, args.output, args.keep_archive_run, seed=args.seed))


if __name__ == '__main__':
    main()
