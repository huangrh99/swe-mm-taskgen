"""Save repair-chain candidates from the full PR pool plus optional stage-11 archives."""
import argparse
import json
from pathlib import Path
import sys
from datetime import datetime, timezone
from report_pipeline.paths import CODE_ROOT, WORKSPACE_ROOT

ROOT = WORKSPACE_ROOT
sys.path.insert(0, str(CODE_ROOT))
from analysis.scripts.step_09_03_run_visual_verifiers import digest, write_json
from pr_crawler.repair_relations import build


def run(source, archives, output_root, max_edges=20000):
    def rows():
        with Path(source).open() as stream:
            for line in stream:
                yield json.loads(line)
    records, inputs = [], []
    for directory in archives:
        manifest = json.loads((directory / '11_manifest.json').read_text())
        if manifest['status'] not in ('complete', 'partial'):
            raise ValueError('Archive is unfinished')
        for name, sha in manifest['files'].items():
            path = directory / name
            if digest(path) != sha:
                raise ValueError('Archive hash mismatch')
            records.append(json.loads(path.read_text()))
            immutable = directory / '11_record_versions' / (sha + '.json')
            inputs.append({'path': str((immutable if immutable.exists() else path).resolve()), 'sha256': sha})
    if max_edges <= 0:
        raise ValueError('max_edges must be positive')
    result = build(rows(), records, max_edges=max_edges)
    output = Path(output_root).resolve() / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    output.mkdir(parents=True, exist_ok=False)
    for key in ('nodes', 'edges', 'node_signals', 'reopen_events', 'omissions'):
        (output / f'12_{key}.jsonl').write_text(''.join(json.dumps(v, ensure_ascii=False) + '\n' for v in result[key]))
    write_json(output / '12_summary.json', {'purpose': 'data_archival_and_screening',
        'source': str(Path(source).resolve()), 'source_sha256': digest(source), 'archives': inputs,
        'counts': {k: len(result[k]) for k in ('nodes', 'edges', 'node_signals', 'reopen_events', 'omissions')},
        'status': 'candidate_recall_only', 'max_edges': max_edges, 'limitations': result['limitations']})
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=ROOT / 'crawler-output/multimodal-2025/prs_2025_plus.jsonl')
    parser.add_argument('--archive', type=Path, action='append', default=[])
    parser.add_argument('--output', type=Path, default=ROOT / 'crawler-output/multimodal-2025/12_repair_relations')
    parser.add_argument('--max-edges', type=int, default=20000)
    args = parser.parse_args()
    print(run(args.input, args.archive, args.output, args.max_edges))
