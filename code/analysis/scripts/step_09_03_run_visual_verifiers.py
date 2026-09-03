"""Prepare or run 1–20 PR-only single-call verifiers, then export five deterministic buckets."""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time
from report_pipeline.paths import CODE_ROOT, WORKSPACE_ROOT

ROOT = WORKSPACE_ROOT
sys.path.insert(0, str(CODE_ROOT))
from analysis.scripts.step_08_03_pilot_visual_context_vlm import SOURCE, BASE, prepare, digest
from pr_crawler import visual_verifier as verifier
from pr_crawler.verifier_backend import evaluate_once

OUTPUT = BASE / '09_single_call_verifier'
TMP = ROOT / 'tmp/multimodal-2025/09_verifier'


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def identity(row):
    return f"{row['repo']}#{row['number']}"


def select_rows(source, wanted):
    if not 1 <= len(wanted) <= 20 or len(set(wanted)) != len(wanted):
        raise ValueError('Specify 1–20 distinct PRs; full-dataset invocation is not enabled')
    selected = {}
    with Path(source).open('rb') as stream:
        for line in stream:
            row = json.loads(line)
            key = identity(row)
            if key in wanted:
                if key in selected:
                    raise ValueError('Duplicate source PR: ' + key)
                selected[key] = (row, line)
    if set(wanted) != set(selected):
        raise ValueError('Requested PRs absent: ' + str(set(wanted) - set(selected)))
    ordered = [selected[key] for key in wanted]
    if any(not line.endswith(b'\n') for _, line in ordered[:-1]):
        raise ValueError('An unterminated source line must be selected last to preserve JSONL bytes')
    return ordered


def run_one(row, raw_line, directory, output, index, run_model, timeout, evaluator):
    record = {'pr_id': identity(row), 'status': 'failed', 'input_packet': None,
              'source_line_sha256': hashlib.sha256(raw_line).hexdigest()}
    start = time.monotonic()
    try:
        packet, paths = prepare(row, directory)
        for asset in packet['images']:
            if asset['status'] == 'attached':
                old = Path(asset['local_path'])
                target = directory / ('09_' + old.name)
                old.rename(target)
                asset['local_path'] = str(target)
        paths = [Path(a['local_path']) for a in packet['images'] if a['status'] == 'attached']
        packet.update(packet_version='pr-only-verifier-v3', missing_sources=verifier.MISSING_SOURCES,
                      source_quote_candidates=verifier.quote_candidates(packet))
        packet_path = directory / '09_input_packet.json'
        (directory / 'input.packet.json').rename(packet_path)
        write_json(packet_path, packet)
        record.update(input_packet=str(packet_path), packet_sha256=digest(packet_path),
                      image_count=len(paths), status='prepared')
        bound_schema = directory / '09_bound_output_schema.json'
        write_json(bound_schema, verifier.bind_schema(packet, output / '09_output_schema.json'))
        record.update(bound_schema=str(bound_schema), bound_schema_sha256=digest(bound_schema))
        if run_model:
            record['status'] = 'failed'
            annotation, metadata = evaluator(packet=packet, image_paths=paths,
                system_prompt=output / '09_system_prompt.md', schema=bound_schema,
                workdir=directory, timeout=timeout)
            record['invocation'] = metadata
            verifier.validate(annotation, packet, output / '09_output_schema.json')
            record.update(status='complete', annotation=annotation, decision=verifier.decide(annotation))
    except Exception as exc:
        record.update(status='failed', error=f'{type(exc).__name__}: {str(exc)[:1000]}',
                      decision={'bucket': 'review', 'reason_code': 'preparation_invocation_or_validation_failed',
                                'policy_version': verifier.POLICY_VERSION, 'training_ready': False})
    record['elapsed_seconds'] = round(time.monotonic() - start, 3)
    write_json(output / f'09_result_{index:04d}.json', record)
    print(json.dumps({'pr_id': record['pr_id'], 'status': record['status'],
                      'bucket': record.get('decision', {}).get('bucket')}, ensure_ascii=False), flush=True)
    return record


def export_results(directory):
    """Offline revalidation; output raw source lines, never replace input payload fields."""
    directory = Path(directory)
    manifest = json.loads((directory / '09_run_manifest.json').read_text())
    if manifest['status'] not in ('complete', 'complete_with_failures'):
        raise ValueError('Only a finished invoked run may be exported')
    if manifest['policy_version'] != verifier.POLICY_VERSION:
        raise ValueError('Policy version mismatch; do not relabel an older run silently')
    for filename, key in [('09_system_prompt.md', 'prompt_sha256'),
                          ('09_output_schema.json', 'schema_sha256'),
                          ('09_source_prs.jsonl', 'selected_source_sha256')]:
        if digest(directory / filename) != manifest[key]:
            raise ValueError('Run snapshot hash mismatch: ' + filename)
    lines = (directory / '09_source_prs.jsonl').read_bytes().splitlines(keepends=True)
    if [identity(json.loads(line)) for line in lines] != manifest['pr_ids']:
        raise ValueError('Source selection identity/order mismatch')
    buckets = {key: [] for key in verifier.BUCKETS}
    ledger, records = [], []
    for i, line in enumerate(lines, 1):
        record = json.loads((directory / f'09_result_{i:04d}.json').read_text())
        if record['pr_id'] != manifest['pr_ids'][i - 1] or record['source_line_sha256'] != hashlib.sha256(line).hexdigest():
            raise ValueError('Result/source identity or hash mismatch')
        if record['status'] == 'complete':
            packet_path = Path(record['input_packet'])
            if digest(packet_path) != record['packet_sha256']:
                raise ValueError('Packet hash mismatch')
            packet, row = json.loads(packet_path.read_text()), json.loads(line)
            if manifest.get('schema_binding_version') or record.get('bound_schema'):
                bound_path = Path(record['bound_schema'])
                if digest(bound_path) != record['bound_schema_sha256'] or json.loads(bound_path.read_text()) != verifier.bind_schema(packet, directory / '09_output_schema.json'):
                    raise ValueError('Bound schema hash or packet binding mismatch')
            if packet['title'] != row['title'] or packet['body'] != (row.get('body') or ''):
                raise ValueError('Packet text differs from original source')
            expected = [a['asset_id'] for a in row['image_screening']['assets']
                        if a['media_kind'] == 'image' and not a['decoration_reason']]
            if expected != [a['asset_id'] for a in packet['images']]:
                raise ValueError('Packet image coverage differs from original source')
            for asset in packet['images']:
                if asset['status'] == 'attached' and digest(asset['local_path']) != asset['sha256']:
                    raise ValueError('Image hash mismatch')
            metadata = record['invocation']
            if metadata.get('provider_response'):
                from pr_crawler.api_engines import extract_annotation, PROFILES
                full = Path(metadata['provider_response'])
                if digest(full) != metadata['provider_response_sha256'] or digest(metadata['request']) != metadata['request_sha256']:
                    raise ValueError('Provider request/response hash mismatch')
                if extract_annotation(json.loads(full.read_text()), PROFILES[metadata['backend']]['protocol']) != record['annotation']:
                    raise ValueError('Annotation differs from provider response')
            if digest(metadata['raw_response']) != metadata['raw_response_sha256']:
                raise ValueError('Raw model response hash mismatch')
            if json.loads(Path(metadata['raw_response']).read_text()) != record['annotation']:
                raise ValueError('Annotation differs from raw model response')
            verifier.validate(record['annotation'], packet, directory / '09_output_schema.json')
            decision = verifier.decide(record['annotation'])
        elif record['status'] == 'failed':
            decision = {'bucket': 'review', 'reason_code': 'preparation_invocation_or_validation_failed',
                        'policy_version': verifier.POLICY_VERSION, 'training_ready': False}
        else:
            raise ValueError('Unexpected unfinished result')
        if decision != record['decision']:
            raise ValueError('Stored decision differs from deterministic policy')
        buckets[decision['bucket']].append(line)
        ledger.append({'pr_id': record['pr_id'], 'source_line_sha256': record['source_line_sha256'],
                       'result_file': f'09_result_{i:04d}.json', **decision})
        records.append(record)
    # Validate all evidence before publishing any derived exports.
    for bucket, values in buckets.items():
        target = directory / f'09_{bucket}_prs.jsonl'
        temporary = target.with_suffix('.jsonl.tmp')
        temporary.write_bytes(b''.join(values))
        temporary.replace(target)
    target = directory / '09_decision_ledger.jsonl'
    target.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in ledger))
    summary = {'policy_version': verifier.POLICY_VERSION, 'input_prs': len(lines),
               'status_counts': dict(Counter(r['status'] for r in records)),
               'buckets': {k: len(v) for k, v in buckets.items()},
               'cli_reported_tokens_sum': sum(r.get('invocation', {}).get('cli_reported_tokens') or 0 for r in records),
               'training_ready': False, 'scope': 'PR-only model triage, not executable benchmark validation',
               'outputs': {f'09_{key}_prs.jsonl': digest(directory / f'09_{key}_prs.jsonl') for key in buckets}}
    write_json(directory / '09_summary.json', summary)
    report = ['# 09 · 单次 Verifier 筛选结果', '',
              '主集合只保留视觉必要且不能仅靠文字转写完成的候选。所有结果仍需 Issue/patch/历史与执行核验，不是可直接训练的数据。', '',
              '| PR | 状态 | 分流 | 原因 |', '| --- | --- | --- | --- |']
    for record in records:
        d = record['decision']
        report.append(f"| {record['pr_id']} | {record['status']} | {d['bucket']} | {d['reason_code']} |")
    for record in records:
        report += ['', '## ' + record['pr_id'], '']
        if record['status'] == 'complete':
            a = record['annotation']
            report += [a['task']['reason'], '', '材料质量：' + a['quality']['reason'], '',
                       '```json', json.dumps(a, ensure_ascii=False, indent=2), '```']
        else:
            report += [record['error']]
    (directory / '09_verifier_report.md').write_text('\n'.join(report) + '\n')
    return summary


def run_batch(source, wanted, output_root=OUTPUT, tmp_root=TMP, run_model=False,
              workers=2, timeout=480, evaluator=evaluate_once):
    import jsonschema
    from pr_crawler.api_engines import ApiEvaluator
    api_backend = isinstance(evaluator, ApiEvaluator)
    source, output_root, tmp_root = (Path(p).resolve() for p in (source, output_root, tmp_root))
    if workers not in (1, 2) or timeout <= 0:
        raise ValueError('Use 1–2 workers and a positive timeout')
    if api_backend and workers != 1:
        raise ValueError('API backends require one worker')
    jsonschema.Draft202012Validator.check_schema(json.loads(verifier.SCHEMA.read_text()))
    selected = select_rows(source, wanted)
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    output, working = Path(output_root) / run_id, Path(tmp_root) / run_id
    output.mkdir(parents=True, exist_ok=False)
    working.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(verifier.PROMPT, output / '09_system_prompt.md')
    shutil.copyfile(verifier.SCHEMA, output / '09_output_schema.json')
    (output / '09_source_prs.jsonl').write_bytes(b''.join(line for _, line in selected))
    manifest = {'run_id': run_id, 'status': 'running' if run_model else 'preparing',
                'pr_ids': wanted, 'input': str(Path(source).resolve()), 'input_sha256': digest(source),
                'selected_source_sha256': digest(output / '09_source_prs.jsonl'),
                'prompt_sha256': digest(output / '09_system_prompt.md'),
                'schema_sha256': digest(output / '09_output_schema.json'),
                'policy_version': verifier.POLICY_VERSION, 'invoked': run_model,
                'schema_binding_version': 'packet-ids-and-quotes-v3',
                'requested_backend': evaluator.backend if api_backend else 'codex' if evaluator is evaluate_once else 'custom',
                'purpose': 'data_archival_and_screening',
                'requested_model': evaluator.profile['model'] if api_backend else 'gpt-5.6-luna' if evaluator is evaluate_once else None,
                'requested_effort': 'provider_auto' if api_backend else 'max' if evaluator is evaluate_once else None,
                'workers': workers, 'timeout_seconds': timeout, 'temporary_directory': str(working),
                'selection': 'Explicit small pilot, not representative or accuracy evaluation'}
    write_json(output / '09_run_manifest.json', manifest)
    def process(item):
        i, (row, line) = item
        return run_one(row, line, working / f'09_pr_{i:04d}', output, i, run_model, timeout, evaluator)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        records = list(pool.map(process, enumerate(selected, 1)))
    failures = sum(r['status'] == 'failed' for r in records)
    manifest.update(status=('complete_with_failures' if failures else 'complete') if run_model else 'prepared',
                    failed=failures, completed=sum(r['status'] == 'complete' for r in records))
    write_json(output / '09_run_manifest.json', manifest)
    if run_model:
        export_results(output)
    return output, failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=SOURCE)
    parser.add_argument('--pr', action='append', required=True)
    parser.add_argument('--output', type=Path, default=OUTPUT)
    parser.add_argument('--tmp', type=Path, default=TMP)
    parser.add_argument('--run', action='store_true', help='Invoke the model; otherwise only prepare packets')
    parser.add_argument('--workers', type=int, choices=(1, 2))
    parser.add_argument('--backend', choices=('codex', 'k3', 'gemini'), default='codex')
    parser.add_argument('--model', help='API model override; Codex retains the historical Luna/max configuration')
    parser.add_argument('--key-file', type=Path)
    parser.add_argument('--attempts', type=int, choices=(1, 2, 3), default=1)
    parser.add_argument('--timeout', type=int, default=480)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error('timeout must be positive')
    evaluator = evaluate_once
    workers = args.workers or (2 if args.backend == 'codex' else 1)
    if args.backend != 'codex':
        from pr_crawler.api_engines import ApiEvaluator
        if workers != 1:
            parser.error('The API pilot uses one worker for a single explicit rate/retry budget')
        evaluator = ApiEvaluator(args.backend, args.model, args.key_file, args.attempts)
    elif args.model or args.key_file or args.attempts != 1:
        parser.error('Model/key-file/attempts are API-backend options')
    output, failed = run_batch(args.input, args.pr, args.output, args.tmp, args.run, workers, args.timeout, evaluator)
    print(json.dumps({'output': str(output), 'failed': failed}), flush=True)
    if failed:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
