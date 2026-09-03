"""Offline filter: retain image PRs merged directly into the snapshot default branch."""

import argparse
from collections import Counter, defaultdict
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from report_pipeline.paths import WORKSPACE_ROOT

ROOT = WORKSPACE_ROOT
BASE = ROOT / 'crawler-output/multimodal-2025/image-screening'
KEEP = '06_prs_with_non_badge_images_merged_to_default_branch.jsonl'
REJECT = '06_excluded_prs_not_merged_to_default_branch.jsonl'
LEDGER = '06_all_image_prs_merge_decision_ledger.jsonl'
SUMMARY = '06_merge_default_branch_summary.json'
RULE = 'merged-to-snapshot-default-v1'


def decision(row):
    """Fail closed on incomplete/contradictory merge evidence; never infer from closed_at."""
    if 'merged_at' not in row:
        return 'unknown_merge_metadata'
    if not row['merged_at']:
        if row.get('merged') is True:
            return 'inconsistent_merge_metadata'
        return {'closed': 'closed_without_merge', 'open': 'still_open'}.get(
            row.get('state'), 'unknown_merge_metadata')
    try:
        timestamp = datetime.fromisoformat(row['merged_at'].replace('Z', '+00:00'))
        if timestamp.tzinfo is None:
            return 'unknown_merge_metadata'
    except (ValueError, TypeError, AttributeError):
        return 'unknown_merge_metadata'
    if row.get('state') != 'closed' or row.get('merged') is False:
        return 'inconsistent_merge_metadata'
    base = row.get('base') or {}
    target, default = base.get('ref'), (base.get('repo') or {}).get('default_branch')
    if not target or not default:
        return 'unknown_branch_metadata'
    return 'kept' if target == default else 'merged_to_non_default_branch'


def screen(source, output, temporary):
    source, output, temporary = (Path(p).resolve() for p in (source, output, temporary))
    names = (KEEP, REJECT, LEDGER)
    if source in [output / name for name in (*names, SUMMARY)]:
        raise ValueError('Output must not overwrite input')
    if temporary == output or output in temporary.parents:
        raise ValueError('Temporary files must be outside the result directory')
    temporary.mkdir(parents=True, exist_ok=True)
    counts, repositories, years = Counter(), defaultdict(Counter), defaultdict(Counter)
    defaults, identities, source_hash = defaultdict(set), set(), hashlib.sha256()
    with tempfile.TemporaryDirectory(prefix='merge-filter-', dir=temporary) as directory, ExitStack() as stack:
        staging = Path(directory)
        streams = {name: stack.enter_context((staging / name).open('wb')) for name in names}
        with source.open('rb') as stream:
            for line_number, raw in enumerate(stream, 1):
                row = json.loads(raw)
                identity = (row['repo'], row['number'])
                if identity in identities:
                    raise ValueError('Duplicate input PR identity')
                identities.add(identity)
                source_hash.update(raw)
                reason = decision(row)
                base = row.get('base') or {}
                default = (base.get('repo') or {}).get('default_branch')
                defaults[row['repo']].add(default)
                # Partition original lines byte-for-byte, retaining every field and the full body.
                streams[KEEP if reason == 'kept' else REJECT].write(raw)
                evidence = {'repo': row['repo'], 'number': row['number'], 'id': row.get('id'),
                    'html_url': row.get('html_url'), 'input_line': line_number,
                    'input_line_sha256': hashlib.sha256(raw).hexdigest(), 'rule_version': RULE,
                    'decision': reason, 'state': row.get('state'), 'merged_at': row.get('merged_at'),
                    'merged_field_present': 'merged' in row, 'merged': row.get('merged'),
                    'base_ref': base.get('ref'), 'snapshot_default_branch': default,
                    'created_at': row.get('created_at'), 'source_run_id': row.get('source_run_id')}
                streams[LEDGER].write((json.dumps(evidence, ensure_ascii=False) + '\n').encode())
                counts[reason] += 1
                counts['input_prs'] += 1
                repositories[row['repo']][reason] += 1
                years[(row.get('created_at') or 'unknown')[:4]][reason] += 1
        for stream in streams.values():
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
        hashes = {}
        for name in names:
            path, digest = staging / name, hashlib.sha256()
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(chunk)
            hashes[name] = {'sha256': digest.hexdigest(), 'bytes': path.stat().st_size}
        counts['excluded_prs'] = counts['input_prs'] - counts['kept']
        summary = {'rule_version': RULE, 'generated_at': datetime.now(timezone.utc).isoformat(),
            'input': str(source), 'input_sha256': source_hash.hexdigest(),
            'rule': 'Valid merged_at, state=closed, no merged=false contradiction, base.ref=base.repo.default_branch.',
            'branch_reference': 'Repository default_branch in each collected PR snapshot, not historical default at merge time.',
            'time_scope': 'Inherited input created_at range; no additional merged_at date cutoff.',
            'limitations': ['No Git reachability check or merge-method inference.',
                'Indirect integration, cherry-picks, reverts and historical default-branch renames are not resolved.',
                'Image availability and visual necessity are not established by this filter.'],
            'counts': dict(counts), 'repositories': dict(sorted(repositories.items())),
            'created_years': dict(sorted(years.items())),
            'snapshot_default_branches': {r: sorted(v, key=str) for r, v in sorted(defaults.items())},
            'outputs': hashes, 'temporary_directory': str(temporary)}
        (staging / SUMMARY).write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
        output.mkdir(parents=True, exist_ok=True)
        for name in (*names, SUMMARY):
            os.replace(staging / name, output / name)
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', default=str(BASE / '04_pr_body_images_after_attachment_typing/04_prs_with_non_badge_images.jsonl'))
    parser.add_argument('--output', default=str(BASE / '06_merged_default_branch_images'))
    parser.add_argument('--tmp', default=str(ROOT / 'tmp/multimodal-2025/04_merge_default_branch_screening'))
    args = parser.parse_args()
    result = screen(args.input, args.output, args.tmp)
    print(json.dumps({'counts': result['counts'], 'output': args.output}, ensure_ascii=False))
