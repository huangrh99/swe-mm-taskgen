"""Aggregate frozen archived Stage-16 runs without invoking a model.

The aggregate is a presentation boundary: result records are copied byte-for-byte
into the selected JSONL order, while their packet, curator asset index and bound
schema continue to point at the immutable source runs.
"""

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil

from report_pipeline.paths import WORKSPACE_ROOT
from pr_crawler import repair_sufficiency as policy


ROOT = WORKSPACE_ROOT
DEFAULT_OUTPUT = ROOT / 'crawler-output/multimodal-2025/16_visual_necessity_selection'
ALLOWED_STATUSES = {'prepared', 'complete', 'ineligible', 'failed'}
STATUS_PRIORITY = {'failed': 0, 'prepared': 1, 'ineligible': 2, 'complete': 3}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def identity(repo, number):
    return f'{repo}#{number}'


def selected_cases(path, expected_count=100):
    path = Path(path).resolve()
    rows = []
    with path.open('rb') as stream:
        for line in stream:
            if not line.strip():
                continue
            row = json.loads(line)
            repo, number = row.get('repo'), row.get('number')
            if not isinstance(repo, str) or not isinstance(number, int):
                raise ValueError('Selected JSONL row lacks repo/number identity')
            rows.append((identity(repo, number), repo, number))
    ids = [item[0] for item in rows]
    if expected_count is not None and len(rows) != expected_count:
        raise ValueError(f'Selected JSONL must contain exactly {expected_count} cases')
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate PR in selected JSONL')
    return rows


def frozen_file(run, manifest, filename, key):
    path = run / filename
    if not path.is_file() or manifest.get(key) != digest(path):
        raise ValueError(f'Frozen Stage-16 file changed: {path}')
    return path


def validate_result(run, manifest, index, pr_id, number, case_id):
    result_path = run / f'16_03_result_{index:04d}.json'
    if not result_path.is_file():
        raise ValueError('Missing Stage-16 result: ' + str(result_path))
    result = json.loads(result_path.read_text())
    if (result.get('pr_id') != pr_id or result.get('pr_number') != number
            or result.get('case_id') != case_id or result.get('status') not in ALLOWED_STATUSES):
        raise ValueError('Stage-16 result identity/status mismatch: ' + pr_id)
    packet_path = Path(result.get('packet', '')).resolve()
    curator_path = Path(result.get('curator_assets', '')).resolve()
    schema_path = Path(result.get('bound_schema', '')).resolve()
    for path, key in ((packet_path, 'packet_sha256'),
                      (curator_path, 'curator_assets_sha256'),
                      (schema_path, 'bound_schema_sha256')):
        if not path.is_file() or result.get(key) != digest(path):
            raise ValueError(f'Stage-16 result dependency changed for {pr_id}: {path}')
    packet = json.loads(packet_path.read_text())
    curator = json.loads(curator_path.read_text())
    if (packet.get('case_id') != case_id or packet.get('repository') != pr_id.rsplit('#', 1)[0]
            or packet.get('pr_number') != number or curator.get('case_id') != case_id):
        raise ValueError('Packet or curator identity mismatch: ' + pr_id)
    schema = json.loads(schema_path.read_text())
    if schema != policy.bind_schema(packet, run / '16_02_output_schema.json'):
        raise ValueError('Bound schema differs from frozen packet: ' + pr_id)
    if result['status'] == 'complete':
        policy.validate(result['annotation'], packet, run / '16_02_output_schema.json')
        text_decision = policy.text_decision(result['annotation'])
        if result.get('text_decision') != text_decision:
            raise ValueError('Stored text-only decision changed: ' + pr_id)
        expected = policy.reconcile(
            (result.get('visual_verifier') or {}).get('decision'), text_decision)
        if result.get('reconciliation') != expected:
            raise ValueError('Stored reconciliation changed: ' + pr_id)
    return result_path, result


def load_run(path):
    run = Path(path).resolve()
    manifest_path = run / '16_03_run_manifest.json'
    manifest = json.loads(manifest_path.read_text())
    if (manifest.get('input_mode') != 'stage11_source_archives'
            or manifest.get('schema_version') != 'text-only-verifier-run-v2'):
        raise ValueError('Only archived Stage-16 v2 runs can be aggregated: ' + str(run))
    prompt = frozen_file(run, manifest, '16_01_system_prompt.md', 'prompt_sha256')
    schema = frozen_file(run, manifest, '16_02_output_schema.json', 'schema_sha256')
    contract_run, contract_manifest = run, manifest
    if manifest.get('protocol_recovery'):
        contract_run = Path(manifest.get('source_run', '')).resolve()
        source_manifest_path = contract_run / '16_03_run_manifest.json'
        if (not source_manifest_path.is_file()
                or manifest.get('source_run_manifest_sha256') != digest(source_manifest_path)):
            raise ValueError('Recovered Stage-16 source manifest changed: ' + str(contract_run))
        contract_manifest = json.loads(source_manifest_path.read_text())
        for key in ('packet_builder_sha256', 'policy_module_sha256', 'policy_version'):
            if manifest.get(key) != contract_manifest.get(key):
                raise ValueError('Recovered Stage-16 contract differs from source: ' + key)
    builder = frozen_file(
        contract_run, contract_manifest, '16_06_packet_builder.py', 'packet_builder_sha256')
    module = frozen_file(
        contract_run, contract_manifest, '16_06_policy_module.py', 'policy_module_sha256')
    if manifest.get('policy_version') != policy.POLICY_VERSION:
        raise ValueError('Stage-16 policy version mismatch: ' + str(run))
    fields = ('pr_ids', 'pr_numbers', 'case_ids')
    if any(not isinstance(manifest.get(key), list) for key in fields):
        raise ValueError('Stage-16 manifest lacks ordered identities: ' + str(run))
    if len({len(manifest[key]) for key in fields}) != 1:
        raise ValueError('Stage-16 manifest identity lengths differ: ' + str(run))
    records = []
    for index, (pr_id, number, case_id) in enumerate(zip(
            manifest['pr_ids'], manifest['pr_numbers'], manifest['case_ids']), 1):
        if not isinstance(number, int) or not isinstance(case_id, str):
            raise ValueError('Invalid Stage-16 manifest identity')
        result_path, result = validate_result(run, manifest, index, pr_id, number, case_id)
        records.append((pr_id, result_path, result))
    if len({item[0] for item in records}) != len(records):
        raise ValueError('Duplicate PR inside Stage-16 run: ' + str(run))
    return {
        'run': run, 'manifest': manifest, 'manifest_path': manifest_path,
        'prompt': prompt, 'schema': schema, 'builder': builder, 'module': module,
        'records': records,
    }


def _resolve_retry_records(pr_id, candidates, allow_retry_overrides):
    if len(candidates) == 1:
        return candidates[0], None
    if not allow_retry_overrides:
        raise ValueError('Duplicate PR across Stage-16 runs: ' + pr_id)
    bindings = {
        (item[2].get('packet_sha256'), item[2].get('curator_assets_sha256'),
         item[2].get('bound_schema_sha256'), item[2].get('case_id'))
        for item in candidates
    }
    if len(bindings) != 1:
        raise ValueError('Retry candidates differ in frozen input binding: ' + pr_id)
    highest = max(STATUS_PRIORITY[item[2]['status']] for item in candidates)
    winners = [item for item in candidates
               if STATUS_PRIORITY[item[2]['status']] == highest]
    if len(winners) != 1 and highest > STATUS_PRIORITY['failed']:
        raise ValueError('Ambiguous successful retry results: ' + pr_id)
    winner = winners[-1]
    return winner, {
        'policy': 'unique_highest_status_with_identical_frozen_inputs',
        'selected_status': winner[2]['status'],
        'attempts': [
            {'source_run': str(item[0]['run']), 'status': item[2]['status'],
             'source_result': str(item[1]), 'source_result_sha256': digest(item[1])}
            for item in candidates
        ],
    }


def aggregate(runs, selected, output_root=DEFAULT_OUTPUT, expected_count=100,
              allow_retry_overrides=False):
    selected = Path(selected).resolve()
    ordered = selected_cases(selected, expected_count)
    sources = [load_run(run) for run in runs]
    if not sources:
        raise ValueError('At least one Stage-16 run is required')
    frozen_contract = {(digest(source['prompt']), digest(source['schema']),
                        source['manifest']['policy_version']) for source in sources}
    if len(frozen_contract) != 1:
        raise ValueError('Stage-16 runs use different prompt/schema/policy contracts')
    by_id = {}
    for source in sources:
        for pr_id, result_path, result in source['records']:
            by_id.setdefault(pr_id, []).append((source, result_path, result))
    wanted = [item[0] for item in ordered]
    missing = [pr_id for pr_id in wanted if pr_id not in by_id]
    extras = sorted(set(by_id) - set(wanted))
    if missing:
        raise ValueError('Selected PRs missing from Stage-16 runs: ' + ', '.join(missing))
    if extras:
        raise ValueError('Stage-16 runs contain unselected PRs: ' + ', '.join(extras))
    resolved = {
        pr_id: _resolve_retry_records(pr_id, by_id[pr_id], allow_retry_overrides)
        for pr_id in wanted
    }

    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    output_root = Path(output_root).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    output = output_root / run_id
    output.mkdir(exist_ok=False)
    shutil.copyfile(sources[0]['prompt'], output / '16_01_system_prompt.md')
    shutil.copyfile(sources[0]['schema'], output / '16_02_output_schema.json')
    shutil.copyfile(Path(__file__), output / '16_07_aggregate_builder.py')
    statuses = Counter()
    case_ids, pr_numbers, source_cases = [], [], []
    for index, (pr_id, _, number) in enumerate(ordered, 1):
        (source, result_path, result), retry_resolution = resolved[pr_id]
        target = output / f'16_03_result_{index:04d}.json'
        shutil.copyfile(result_path, target)
        statuses[result['status']] += 1
        case_ids.append(result['case_id'])
        pr_numbers.append(number)
        source_cases.append({'pr_id': pr_id, 'source_run': str(source['run']),
                             'source_result': str(result_path),
                             'source_result_sha256': digest(result_path),
                             'aggregate_result': target.name,
                             'aggregate_result_sha256': digest(target),
                             'retry_resolution': retry_resolution})
    manifest = {
        'schema_version': 'text-only-verifier-aggregate-v1',
        'input_mode': 'aggregate_stage16_archived_runs',
        'run_id': run_id, 'status': 'complete',
        'model_invoked': False,
        'purpose': 'ordered cumulative human-review input; no model invocation',
        'selected_source': str(selected), 'selected_source_sha256': digest(selected),
        'source_runs': [{'path': str(source['run']),
                         'manifest_sha256': digest(source['manifest_path'])}
                        for source in sources],
        'retry_resolution_enabled': allow_retry_overrides,
        'pr_ids': wanted, 'pr_numbers': pr_numbers, 'case_ids': case_ids,
        'status_counts': {key: statuses.get(key, 0) for key in sorted(ALLOWED_STATUSES)},
        'prompt_sha256': digest(output / '16_01_system_prompt.md'),
        'schema_sha256': digest(output / '16_02_output_schema.json'),
        'policy_version': policy.POLICY_VERSION,
        'aggregate_builder_sha256': digest(output / '16_07_aggregate_builder.py'),
        'source_cases': source_cases,
        'model_results_reused': True,
    }
    manifest_path = output / '16_03_run_manifest.json'
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, action='append', required=True,
                        help='Frozen archived Stage-16 run; repeat in any order.')
    parser.add_argument('--selected', type=Path, required=True,
                        help='08_02 selected JSONL whose order is authoritative.')
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        '--resolve-retries', action='store_true',
        help='Resolve duplicate PR attempts only when frozen inputs match and one status wins uniquely.')
    parser.add_argument('--expected-count', type=int, default=100)
    args = parser.parse_args()
    print(aggregate(args.run, args.selected, args.output, args.expected_count,
                    args.resolve_retries))


if __name__ == '__main__':
    main()
