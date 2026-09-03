"""Prepare immutable text-only packets; invoke Gemini only with explicit --run."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import sys
import tarfile
import time
from report_pipeline.paths import CODE_ROOT, WORKSPACE_ROOT

ROOT = WORKSPACE_ROOT
sys.path.insert(0, str(CODE_ROOT))
from pr_crawler import repair_sufficiency as policy
from pr_crawler.assets import apply_recovery

DEFAULT_PILOT = ROOT / 'crawler-output/multimodal-2025/14_pr_test_pilot/20260901_bpmn_10'
DEFAULT_OUTPUT = ROOT / 'crawler-output/multimodal-2025/16_visual_necessity_selection'
DEFAULT_TMP = ROOT / 'tmp/multimodal-2025/16_visual_necessity_selection'


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def baseline_index(case_dir, manifest):
    tar_path = case_dir / '14_baseline_tree.tar'
    if digest(tar_path) != manifest['artifacts']['14_baseline_tree.tar']:
        raise ValueError('Baseline archive hash mismatch')
    paths = []
    with tarfile.open(tar_path, 'r') as archive:
        for member in archive.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or '..' in path.parts:
                raise ValueError('Unsafe baseline archive path')
            if member.isfile():
                paths.append(str(path))
    if not paths or len(paths) != len(set(paths)):
        raise ValueError('Baseline file index is empty or duplicated')
    return sorted(paths), tar_path


def issue_problem_sources(archive):
    urls = [asset.get('url') for asset in archive['sections']['assets']['items'] if asset.get('url')]
    sources = []
    for document in archive['archival_view']['documents']:
        if document['kind'] != 'issue' or document.get('field') not in ('title', 'body'):
            continue
        masked, _ = policy.mask_visuals(document['text'], urls)
        sources.append({'source_id': document['source_id'], 'kind': document['kind'],
            'field': document['field'], 'relation': document.get('relation'), 'url': document.get('url'),
            'created_at': document.get('created_at'), 'updated_at': document.get('updated_at'),
            'historical_version_verified': bool(document.get('historical_version_verified')),
            'original_text_sha256': document['text_sha256'],
            'text': masked, 'text_sha256': hashlib.sha256(masked.encode()).hexdigest()})
    return sources


def eligible_images(archive_path, archive, problem_sources):
    source_ids = {source['source_id'] for source in problem_sources}
    source_documents = {doc['source_id']: doc['text'] for doc in archive['archival_view']['documents']
                        if doc['source_id'] in source_ids}
    items = []
    for raw_asset in archive['sections']['assets']['items']:
        asset = apply_recovery(archive_path, raw_asset)
        url = asset.get('url')
        matches = [source_id for source_id, text in source_documents.items() if url and url in text]
        if not matches:
            continue
        local_path = asset.get('local_path')
        path = archive_path.parent / '11_http_archive' / local_path if local_path else None
        if (asset.get('status') != 'complete' or path is None or not path.is_file()
                or digest(path) != asset['sha256']):
            items.append({'asset_id': asset.get('sha256') or url, 'url': url, 'status': 'unavailable',
                          'source_ids': matches, 'local_path': None, 'sha256': asset.get('sha256')})
        else:
            items.append({'asset_id': asset['sha256'], 'url': url, 'status': 'available',
                          'source_ids': matches, 'local_path': str(path.resolve()), 'sha256': asset['sha256']})
    return items


def build_packet(case_dir):
    case_dir = Path(case_dir).resolve()
    case = json.loads((case_dir / '14_case_manifest.json').read_text())
    archive_path = Path(case['source_archive'])
    if digest(archive_path) != case['source_archive_sha256']:
        raise ValueError('Source archive hash mismatch')
    archive = json.loads(archive_path.read_text())
    paths, tar_path = baseline_index(case_dir, case)
    sources = issue_problem_sources(archive)
    images = eligible_images(archive_path, archive, sources)
    recovery_path = archive_path.parent / '11_01_asset_recovery_manifest.json'
    packet = {'schema_version': 'text-only-repair-packet-v1',
        'case_id': archive['instance_id'], 'repository': archive['repo'], 'pr_number': archive['number'],
        'baseline_sha': case['anchors']['baseline_sha'], 'problem_sources': sources,
        'baseline_file_index': paths,
        'withheld': ['all_image_pixels', 'image_alt_text', 'pull_request_prose', 'comments',
                     'reviews', 'commits', 'diff', 'patch', 'tests', 'reference_code'],
        'evidence_limits': {'historical_problem_text_verified': all(s['historical_version_verified'] for s in sources),
            'baseline_file_contents_supplied': False, 'repository_navigation_executed': False},
        'provenance': {'case_manifest': str((case_dir / '14_case_manifest.json').resolve()),
            'case_manifest_sha256': digest(case_dir / '14_case_manifest.json'),
            'source_archive': str(archive_path.resolve()), 'source_archive_sha256': digest(archive_path),
            'asset_recovery': str(recovery_path.resolve()) if recovery_path.exists() else None,
            'asset_recovery_sha256': digest(recovery_path) if recovery_path.exists() else None,
            'baseline_archive': str(tar_path.resolve()), 'baseline_archive_sha256': digest(tar_path)}}
    return packet, images


def load_visual_index(path):
    if not path:
        return {}, None
    path = Path(path).resolve()
    value = json.loads(path.read_text())
    if value.get('schema_version') != 'visual-verifier-index-v1' or not isinstance(value.get('cases'), dict):
        raise ValueError('Unexpected visual verifier index')
    return value['cases'], {'path': str(path), 'sha256': digest(path)}


def run_batch(pilot=DEFAULT_PILOT, output_root=DEFAULT_OUTPUT, tmp_root=DEFAULT_TMP,
              numbers=None, run_model=False, evaluator=None, visual_index=None, timeout=480):
    import jsonschema
    pilot, output_root, tmp_root = map(lambda p: Path(p).resolve(), (pilot, output_root, tmp_root))
    config = json.loads((pilot / '14_pilot_manifest.json').read_text())
    selected = numbers or config['pr_numbers']
    if not selected or len(selected) != len(set(selected)) or not set(selected) <= set(config['pr_numbers']):
        raise ValueError('Select distinct PR numbers from the frozen pilot')
    if run_model and evaluator is None:
        raise ValueError('Explicit model run requires an evaluator')
    if timeout <= 0:
        raise ValueError('Timeout must be positive')
    jsonschema.Draft202012Validator.check_schema(json.loads(policy.SCHEMA.read_text()))
    visual, visual_provenance = load_visual_index(visual_index)
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    output, working = output_root / run_id, tmp_root / run_id
    output.mkdir(parents=True, exist_ok=False)
    working.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(policy.PROMPT, output / '16_01_system_prompt.md')
    shutil.copyfile(policy.SCHEMA, output / '16_02_output_schema.json')
    shutil.copyfile(Path(__file__), output / '16_03_packet_builder.py')
    shutil.copyfile(Path(policy.__file__), output / '16_03_policy_module.py')
    records = []
    for index, number in enumerate(selected, 1):
        directory = working / f'16_case_{index:04d}'
        directory.mkdir()
        record = {'case_id': None, 'pr_number': number, 'status': 'failed'}
        started = time.monotonic()
        try:
            packet, curator_assets = build_packet(pilot / '14_cases_verified' / str(number))
            record['case_id'] = packet['case_id']
            packet_path = output / f'16_03_packet_{index:04d}.json'
            write_json(packet_path, packet)
            curator_path = output / f'16_03_curator_assets_{index:04d}.json'
            write_json(curator_path, {'case_id': packet['case_id'], 'assets': curator_assets})
            bound = output / f'16_03_bound_schema_{index:04d}.json'
            write_json(bound, policy.bind_schema(packet, output / '16_02_output_schema.json'))
            record.update(status=('prepared' if packet['problem_sources'] else 'ineligible'),
                          ineligible_reason=(None if packet['problem_sources'] else 'no_linked_issue_problem_source'),
                          packet=str(packet_path), packet_sha256=digest(packet_path),
                          curator_assets=str(curator_path), curator_assets_sha256=digest(curator_path),
                          bound_schema=str(bound), bound_schema_sha256=digest(bound),
                          visual_verifier=visual.get(packet['case_id']))
            if run_model and packet['problem_sources']:
                annotation, invocation = evaluator(packet=packet,
                    image_paths=[], system_prompt=output / '16_01_system_prompt.md', schema=bound,
                    workdir=directory, timeout=timeout)
                legacy_raw = directory / '09_model_raw.json'
                if legacy_raw.is_file() and Path(invocation['raw_response']) == legacy_raw:
                    raw = directory / '16_03_model_raw.json'
                    legacy_raw.rename(raw)
                    invocation['raw_response'] = str(raw)
                    invocation['raw_response_sha256'] = digest(raw)
                policy.validate(annotation, packet, output / '16_02_output_schema.json')
                text = policy.text_decision(annotation)
                record.update(status='complete', annotation=annotation, text_decision=text,
                              reconciliation=policy.reconcile((record['visual_verifier'] or {}).get('decision'), text),
                              invocation=invocation)
        except Exception as exc:
            record.update(status='failed', error=f'{type(exc).__name__}: {str(exc)[:1200]}')
        record['elapsed_seconds'] = round(time.monotonic() - started, 3)
        write_json(output / f'16_03_result_{index:04d}.json', record)
        records.append(record)
    manifest = {'schema_version': 'text-only-verifier-run-v1', 'run_id': run_id,
        'status': ('complete_with_failures' if any(r['status'] == 'failed' for r in records) else 'complete_with_ineligible'
                   if any(r['status'] == 'ineligible' for r in records) else 'complete') if run_model else
                  ('prepared_with_failures' if any(r['status'] == 'failed' for r in records) else 'prepared_with_ineligible'
                   if any(r['status'] == 'ineligible' for r in records) else 'prepared'),
        'model_invoked': run_model, 'purpose': 'IID candidate triage; human acceptance required',
        'pilot_manifest': str((pilot / '14_pilot_manifest.json').resolve()),
        'pilot_manifest_sha256': digest(pilot / '14_pilot_manifest.json'), 'pr_numbers': selected,
        'case_ids': [r['case_id'] for r in records],
        'status_counts': {key: sum(r['status'] == key for r in records)
                          for key in ('prepared', 'complete', 'ineligible', 'failed')},
        'visual_index': visual_provenance,
        'prompt_sha256': digest(output / '16_01_system_prompt.md'),
        'schema_sha256': digest(output / '16_02_output_schema.json'),
        'policy_version': policy.POLICY_VERSION,
        'packet_builder_sha256': digest(output / '16_03_packet_builder.py'),
        'policy_module_sha256': digest(output / '16_03_policy_module.py'),
        'temporary_directory': str(working), 'withheld_from_model': True,
        'agent_ablation': 'not_run_optional_after_human_review'}
    write_json(output / '16_03_run_manifest.json', manifest)
    return output, sum(r['status'] == 'failed' for r in records)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pilot', type=Path, default=DEFAULT_PILOT)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--tmp', type=Path, default=DEFAULT_TMP)
    parser.add_argument('--pr', type=int, action='append')
    parser.add_argument('--visual-index', type=Path)
    parser.add_argument('--run', action='store_true', help='Call Gemini; default only freezes packets')
    parser.add_argument('--model')
    parser.add_argument('--key-file', type=Path)
    parser.add_argument('--attempts', type=int, choices=(1, 2, 3), default=1)
    parser.add_argument('--timeout', type=int, default=480)
    args = parser.parse_args()
    evaluator = None
    if args.run:
        from pr_crawler.api_engines import ApiEvaluator
        evaluator = ApiEvaluator('gemini', args.model, args.key_file, args.attempts)
    elif args.model or args.key_file or args.attempts != 1:
        parser.error('Model/key/attempt options require --run')
    output, failed = run_batch(args.pilot, args.output, args.tmp, args.pr, args.run,
                               evaluator, args.visual_index, args.timeout)
    print(json.dumps({'output': str(output), 'failed': failed}, ensure_ascii=False))
    if failed:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
