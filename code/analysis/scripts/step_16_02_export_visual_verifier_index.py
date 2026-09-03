"""Validate one or more stage-09 runs and index their decisions for stage 16."""

import argparse
import hashlib
import json
from pathlib import Path
import sys
from report_pipeline.paths import CODE_ROOT, WORKSPACE_ROOT

ROOT = WORKSPACE_ROOT
sys.path.insert(0, str(CODE_ROOT))
from pr_crawler import visual_verifier


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def case_id(pr_id):
    repo, number = pr_id.rsplit('#', 1)
    return repo.replace('/', '__') + '-' + number


def build_index(runs):
    cases, run_records = {}, []
    for run in map(lambda p: Path(p).resolve(), runs):
        manifest_path = run / '09_run_manifest.json'
        manifest = json.loads(manifest_path.read_text())
        if manifest['status'] not in ('complete', 'complete_with_failures'):
            raise ValueError('Stage-09 run is not complete: ' + str(run))
        for filename, key in [('09_system_prompt.md', 'prompt_sha256'),
                              ('09_output_schema.json', 'schema_sha256'),
                              ('09_source_prs.jsonl', 'selected_source_sha256')]:
            if digest(run / filename) != manifest[key]:
                raise ValueError('Stage-09 frozen file changed: ' + filename)
        for index, pr_id in enumerate(manifest['pr_ids'], 1):
            result_path = run / f'09_result_{index:04d}.json'
            result = json.loads(result_path.read_text())
            if result['pr_id'] != pr_id or result['status'] != 'complete':
                continue
            packet = json.loads(Path(result['input_packet']).read_text())
            if digest(result['input_packet']) != result['packet_sha256']:
                raise ValueError('Stage-09 input packet changed')
            visual_verifier.validate(result['annotation'], packet, run / '09_output_schema.json')
            decision = visual_verifier.decide(result['annotation'])
            if decision != result['decision']:
                raise ValueError('Stage-09 stored decision changed')
            cid = case_id(pr_id)
            if cid in cases:
                raise ValueError('Duplicate visual verifier case across runs: ' + cid)
            cases[cid] = {'pr_id': pr_id, 'decision': decision, 'annotation': result['annotation'],
                          'result_path': str(result_path), 'result_sha256': digest(result_path),
                          'run_manifest_path': str(manifest_path),
                          'run_manifest_sha256': digest(manifest_path)}
        run_records.append({'path': str(run), 'manifest_sha256': digest(manifest_path)})
    return {'schema_version': 'visual-verifier-index-v1', 'runs': run_records,
            'case_count': len(cases), 'cases': cases}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, action='append', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    value = build_index(args.run)
    args.output.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    print(args.output)


if __name__ == '__main__':
    main()
