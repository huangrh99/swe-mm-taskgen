"""Archive the not-yet-archived cases from a Stage-08 candidate selection."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from report_pipeline.paths import WORKSPACE_ROOT
from analysis.scripts.step_11_archive_pr_sources import run as archive_run
from analysis.scripts.step_11_03_audit_source_archives import run as audit_archives


ROOT = WORKSPACE_ROOT
DEFAULT_OUTPUT = ROOT / 'crawler-output/multimodal-2025/11_source_archive'
DEFAULT_ORCHESTRATION = ROOT / 'crawler-output/multimodal-2025/11_02_candidate_waves'


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def selection(selection_run):
    run = Path(selection_run).resolve()
    candidates = [run / name for name in ('08_03_manifest.json', '08_02_manifest.json')
                  if (run / name).is_file()]
    if len(candidates) != 1:
        raise ValueError('Selection run must contain exactly one supported manifest')
    manifest_path = candidates[0]
    manifest = json.loads(manifest_path.read_text())
    source = run / manifest['selected_file']
    ledger = run / manifest['selection_ledger']
    if digest(source) != manifest['selected_sha256'] or digest(ledger) != manifest['selection_ledger_sha256']:
        raise ValueError('Stage-08 selection changed')
    with ledger.open() as stream:
        ids = [json.loads(line)['pr_id'] for line in stream if line.strip()]
    if len(ids) != manifest['selected_count'] or len(ids) != len(set(ids)):
        raise ValueError('Stage-08 identity count mismatch')
    return source, ids, {'path': str(run), 'manifest_sha256': digest(manifest_path)}


def archived_ids(runs):
    values = set()
    provenance = []
    for run in map(lambda value: Path(value).resolve(), runs):
        manifest_path = run / '11_manifest.json'
        manifest = json.loads(manifest_path.read_text())
        if manifest['status'] not in ('complete', 'partial'):
            raise ValueError('Previous Stage-11 archive is unfinished: ' + str(run))
        values.update(manifest['pr_ids'])
        provenance.append({'path': str(run), 'manifest_sha256': digest(manifest_path)})
    return values, provenance


def run(selection_run, previous_runs, output_root, orchestration_root,
        fetch=False, download_media=False, batch_size=20, workers=1,
        asset_workers=1):
    if not 1 <= batch_size <= 20:
        raise ValueError('Stage-11 wave size must be 1-20')
    if not 1 <= workers <= 8:
        raise ValueError('Stage-11 workers must be 1-8')
    if not 1 <= asset_workers <= 8:
        raise ValueError('Stage-11 asset workers must be 1-8')
    source, selected_ids, selection_provenance = selection(selection_run)
    done, previous_provenance = archived_ids(previous_runs)
    pending = [pr_id for pr_id in selected_ids if pr_id not in done]
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    orchestration = Path(orchestration_root).resolve() / run_id
    orchestration.mkdir(parents=True, exist_ok=False)
    chunks = [pending[start:start + batch_size]
              for start in range(0, len(pending), batch_size)]

    def archive_wave(item):
        index, ids = item
        archive = archive_run(source, ids, output_root, fetch=fetch,
                              download_media=download_media,
                              asset_workers=asset_workers)
        archive_manifest = json.loads((archive / '11_manifest.json').read_text())
        return {'index': index, 'pr_ids': ids, 'run': str(archive),
                'run_manifest_sha256': digest(archive / '11_manifest.json'),
                'status': archive_manifest['status']}

    indexed_chunks = list(enumerate(chunks, 1))
    if workers == 1 or len(indexed_chunks) < 2:
        waves = [archive_wave(item) for item in indexed_chunks]
    else:
        with ThreadPoolExecutor(max_workers=min(workers, len(indexed_chunks))) as pool:
            waves = sorted(pool.map(archive_wave, indexed_chunks),
                           key=lambda wave: wave['index'])
    quality_path = orchestration / '11_03_archive_quality.json'
    quality = audit_archives([*map(str, previous_runs),
                              *[wave['run'] for wave in waves]], quality_path)
    value = {
        'schema_version': 'selected-candidate-stage11-waves-v1',
        'status': ('prepared' if not fetch else
                   'complete_with_partial' if any(wave['status'] == 'partial' for wave in waves)
                   else 'complete'),
        'fetched': fetch,
        'download_media': download_media,
        'selection': selection_provenance,
        'previous_runs': previous_provenance,
        'selected_count': len(selected_ids),
        'already_archived_count': len(set(selected_ids) & done),
        'attempted_count': len(pending),
        'wave_count': len(waves),
        'batch_size': batch_size,
        'workers': workers,
        'asset_workers': asset_workers,
        'automatic_decision': quality['automatic_decision'],
        'quality_audit': {'path': str(quality_path), 'sha256': digest(quality_path)},
        'waves': waves,
    }
    manifest_path = orchestration / '11_02_manifest.json'
    manifest_path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    return manifest_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--selection-run', type=Path, required=True)
    parser.add_argument('--previous-run', type=Path, action='append', default=[])
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--orchestration-output', type=Path, default=DEFAULT_ORCHESTRATION)
    parser.add_argument('--batch-size', type=int, default=20)
    parser.add_argument('--workers', type=int, default=1,
                        help='Independent Stage-11 waves to fetch concurrently (1-8).')
    parser.add_argument('--asset-workers', type=int, default=1, choices=range(1, 9),
                        help='Credential-free image/video downloads per PR (1-8).')
    parser.add_argument('--fetch', action='store_true')
    parser.add_argument('--download-media', action='store_true')
    args = parser.parse_args()
    if args.download_media and not args.fetch:
        parser.error('--download-media requires --fetch')
    print(run(args.selection_run, args.previous_run, args.output,
              args.orchestration_output, args.fetch, args.download_media,
              args.batch_size, args.workers, args.asset_workers))


if __name__ == '__main__':
    main()
