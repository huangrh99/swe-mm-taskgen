"""Archive explicitly selected PRs, linked sources, timelines, and source partitions."""
import argparse
import json
from pathlib import Path
import sys
from datetime import datetime, timezone
from report_pipeline.paths import CODE_ROOT, WORKSPACE_ROOT

ROOT = WORKSPACE_ROOT
sys.path.insert(0, str(CODE_ROOT))
from analysis.scripts.step_09_03_run_visual_verifiers import select_rows, digest, write_json, SOURCE
from pr_crawler.api import API, credential
from pr_crawler.store import Store
from pr_crawler.source_archive import enrich_with_history


def run(source, wanted, output_root, fetch=False, resume=None, download_media=False,
        asset_workers=1):
    if resume:
        directory = Path(resume).resolve()
        manifest = json.loads((directory / '11_manifest.json').read_text())
        if digest(directory / '11_source_prs.jsonl') != manifest['source_sha256']:
            raise ValueError('Source snapshot hash mismatch')
        selected = select_rows(directory / '11_source_prs.jsonl', manifest['pr_ids'])
        download_media = manifest['download_media']
    else:
        selected = select_rows(source, wanted)
        directory = Path(output_root).resolve() / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        directory.mkdir(parents=True, exist_ok=False)
        (directory / '11_source_prs.jsonl').write_bytes(b''.join(line for _, line in selected))
        manifest = {'purpose': 'data_archival_and_screening', 'status': 'prepared', 'pr_ids': wanted,
                    'input': str(Path(source).resolve()), 'source_sha256': digest(directory / '11_source_prs.jsonl'),
                    'download_media': download_media, 'archive_schema': 'source-history-v1'}
        write_json(directory / '11_manifest.json', manifest)
    if not fetch:
        return directory
    store = Store(directory / '11_http_archive')
    try:
        run_id = manifest.get('archive_run_id') or store.new_run({'purpose': manifest['purpose'], 'pr_ids': manifest['pr_ids']})
        manifest.update(archive_run_id=run_id, status='running')
        write_json(directory / '11_manifest.json', manifest)
        api, records = API(store, run_id, credential()), []
        for i, (row, _) in enumerate(selected, 1):
            record = store.get(run_id, f'11_record/{row["repo"]}/{row["number"]}')
            if not record or record['status'] != 'complete':
                record = enrich_with_history(api, row['repo'], row['number'], download_media,
                                             asset_workers)
            write_json(directory / f'11_record_{i:04d}.json', record)
            versions = directory / '11_record_versions'
            versions.mkdir(exist_ok=True)
            sha = digest(directory / f'11_record_{i:04d}.json')
            if not (versions / (sha + '.json')).exists():
                write_json(versions / (sha + '.json'), record)
            records.append(record)
            print(json.dumps({'pr': f'{row["repo"]}#{row["number"]}', 'status': record['status']}), flush=True)
        manifest.update(status='complete' if all(r['status'] == 'complete' for r in records) else 'partial',
                        records=len(records), completed=sum(r['status'] == 'complete' for r in records),
                        files={f'11_record_{i:04d}.json': digest(directory / f'11_record_{i:04d}.json') for i in range(1, len(records)+1)})
        store.finish(run_id, manifest['status'])
        write_json(directory / '11_manifest.json', manifest)
        return directory
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=SOURCE)
    parser.add_argument('--pr', action='append', default=[])
    parser.add_argument('--output', type=Path, default=ROOT / 'crawler-output/multimodal-2025/11_source_archive')
    parser.add_argument('--fetch', action='store_true')
    parser.add_argument('--download-media', action='store_true')
    parser.add_argument('--asset-workers', type=int, choices=range(1, 9), default=1)
    parser.add_argument('--resume', type=Path)
    args = parser.parse_args()
    print(run(args.input, args.pr, args.output, args.fetch, args.resume,
              args.download_media, args.asset_workers))


if __name__ == '__main__':
    main()
