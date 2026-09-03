"""Run Stage 09 over the not-yet-verified cases in one Stage-08 selection."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from report_pipeline.paths import WORKSPACE_ROOT
from analysis.scripts.step_09_03_run_visual_verifiers import run_batch


ROOT = WORKSPACE_ROOT
DEFAULT_OUTPUT = ROOT / 'crawler-output/multimodal-2025/image-screening/09_single_call_verifier'
DEFAULT_TMP = ROOT / 'tmp/multimodal-2025/09_verifier'
DEFAULT_ORCHESTRATION = ROOT / 'crawler-output/multimodal-2025/image-screening/09_06_candidate_waves'


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verified_ids(runs):
    values = set()
    provenance = []
    for run in map(lambda value: Path(value).resolve(), runs):
        manifest_path = run / '09_run_manifest.json'
        manifest = json.loads(manifest_path.read_text())
        if manifest['status'] not in ('complete', 'complete_with_failures'):
            raise ValueError('Previous Stage-09 run is unfinished: ' + str(run))
        for index, pr_id in enumerate(manifest['pr_ids'], 1):
            result = json.loads((run / f'09_result_{index:04d}.json').read_text())
            if result['pr_id'] == pr_id and result['status'] == 'complete':
                values.add(pr_id)
        provenance.append({'path': str(run), 'manifest_sha256': digest(manifest_path)})
    return values, provenance


def selection(selection_run):
    run = Path(selection_run).resolve()
    manifest_path = run / '08_02_manifest.json'
    manifest = json.loads(manifest_path.read_text())
    selected = run / manifest['selected_file']
    ledger = run / manifest['selection_ledger']
    if digest(selected) != manifest['selected_sha256'] or digest(ledger) != manifest['selection_ledger_sha256']:
        raise ValueError('Stage-08 selection changed')
    ids = [json.loads(line)['pr_id'] for line in ledger.open() if line.strip()]
    if len(ids) != manifest['selected_count'] or len(ids) != len(set(ids)):
        raise ValueError('Stage-08 identity count mismatch')
    return selected, ids, {'path': str(run), 'manifest_sha256': digest(manifest_path)}


def run(selection_run, previous_runs, output_root, tmp_root, orchestration_root,
        run_model, evaluator, workers=1, timeout=480, batch_size=20):
    if not 1 <= batch_size <= 20:
        raise ValueError('Stage-09 wave size must be 1-20')
    source, selected_ids, selection_provenance = selection(selection_run)
    done, previous_provenance = verified_ids(previous_runs)
    pending = [pr_id for pr_id in selected_ids if pr_id not in done]
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    orchestration = Path(orchestration_root).resolve() / run_id
    orchestration.mkdir(parents=True, exist_ok=False)
    waves = []
    for start in range(0, len(pending), batch_size):
        ids = pending[start:start + batch_size]
        wave_run, failures = run_batch(source, ids, output_root, tmp_root, run_model,
                                       workers, timeout, evaluator)
        waves.append({'index': len(waves) + 1, 'pr_ids': ids, 'run': str(wave_run),
                      'run_manifest_sha256': digest(wave_run / '09_run_manifest.json'),
                      'failures': failures})
    value = {
        'schema_version': 'selected-candidate-stage09-waves-v1',
        'status': 'complete_with_failures' if any(wave['failures'] for wave in waves) else 'complete',
        'model_invoked': run_model,
        'selection': selection_provenance,
        'previous_runs': previous_provenance,
        'selected_count': len(selected_ids),
        'already_complete_count': len(set(selected_ids) & done),
        'attempted_count': len(pending),
        'wave_count': len(waves),
        'batch_size': batch_size,
        'waves': waves,
    }
    manifest_path = orchestration / '09_06_manifest.json'
    manifest_path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    return manifest_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--selection-run', type=Path, required=True)
    parser.add_argument('--previous-run', type=Path, action='append', default=[])
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--tmp', type=Path, default=DEFAULT_TMP)
    parser.add_argument('--orchestration-output', type=Path, default=DEFAULT_ORCHESTRATION)
    parser.add_argument('--batch-size', type=int, default=20)
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--backend', choices=('gemini', 'k3'), default='gemini')
    parser.add_argument('--model')
    parser.add_argument('--key-file', type=Path)
    parser.add_argument('--attempts', type=int, choices=(1, 2, 3), default=1)
    parser.add_argument('--timeout', type=int, default=480)
    args = parser.parse_args()
    evaluator = None
    if args.run:
        from pr_crawler.api_engines import ApiEvaluator
        evaluator = ApiEvaluator(args.backend, args.model, args.key_file, args.attempts)
    elif args.model or args.key_file or args.attempts != 1:
        parser.error('Model/key/attempt options require --run')
    print(run(args.selection_run, args.previous_run, args.output, args.tmp,
              args.orchestration_output, args.run, evaluator, timeout=args.timeout,
              batch_size=args.batch_size))


if __name__ == '__main__':
    main()
