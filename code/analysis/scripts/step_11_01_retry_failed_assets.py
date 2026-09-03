"""Append-only recovery for retryable Stage-11 asset failures."""

import argparse
import hashlib
import json
from pathlib import Path
import sys

from report_pipeline.paths import CODE_ROOT

sys.path.insert(0, str(CODE_ROOT))
from pr_crawler.assets import download, retryable
from pr_crawler.store import now


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def recover(source_run, downloader=download):
    source_run = Path(source_run).resolve()
    output = source_run / '11_01_asset_recovery_manifest.json'
    if output.exists():
        raise FileExistsError(output)
    manifest_path = source_run / '11_manifest.json'
    manifest = json.loads(manifest_path.read_text())
    http_archive = source_run / '11_http_archive'
    entries = []
    for record_path in sorted(source_run.glob('11_record_*.json')):
        expected = manifest.get('files', {}).get(record_path.name)
        if expected and digest(record_path) != expected:
            raise ValueError('Stage-11 record changed: ' + record_path.name)
        record = json.loads(record_path.read_text())
        for asset in record['sections']['assets']['items']:
            if not retryable(asset):
                continue
            result = downloader(asset, http_archive)
            entries.append({'record': record_path.name, 'repo': record['repo'],
                'pr_number': record['number'], 'url': asset['url'],
                'original_status': asset.get('status'), 'original_reason': asset.get('reason'),
                'recovery': result})
    value = {'schema_version': 'stage11-asset-recovery-v1',
        'created_at': now(), 'source_run': str(source_run),
        'source_manifest_sha256': digest(manifest_path),
        'policy': {'retryable_only': True, 'original_records_mutated': False},
        'entries': entries,
        'counts': {'attempted': len(entries),
            'complete': sum(item['recovery'].get('status') == 'complete' for item in entries),
            'failed': sum(item['recovery'].get('status') != 'complete' for item in entries)}}
    output.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-run', type=Path, required=True)
    args = parser.parse_args()
    print(recover(args.source_run))


if __name__ == '__main__':
    main()
