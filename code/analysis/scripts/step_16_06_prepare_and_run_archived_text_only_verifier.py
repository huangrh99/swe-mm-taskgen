"""Run the stage-16 text-only contract on one or more frozen stage-11 archives."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys
import time
from report_pipeline.paths import CODE_ROOT, WORKSPACE_ROOT

ROOT = WORKSPACE_ROOT
sys.path.insert(0, str(CODE_ROOT))
from analysis.scripts.step_16_03_prepare_and_run_text_only_verifier import (
    digest, eligible_images, issue_problem_sources, load_visual_index, write_json,
)
from pr_crawler import repair_sufficiency as policy

DEFAULT_OUTPUT = ROOT / 'crawler-output/multimodal-2025/16_visual_necessity_selection'
DEFAULT_TMP = ROOT / 'tmp/multimodal-2025/16_visual_necessity_selection'


def normalize_provider_annotation(annotation):
    """Remove only the harmless JSON-Schema declaration some providers echo.

    The original provider bytes remain in the invocation trace.  No semantic
    field is repaired or coerced here; every other extra property continues to
    fail the strict bound schema.
    """
    if not isinstance(annotation, dict):
        return annotation, {'applied': False, 'removed_fields': []}
    normalized = dict(annotation)
    removed = []
    if normalized.get('$schema') == 'https://json-schema.org/draft/2020-12/schema':
        normalized.pop('$schema')
        removed.append('$schema')
    return normalized, {'applied': bool(removed), 'removed_fields': removed}


def semantic_attempt_binding(workdir, semantic_attempt):
    """Bind every retained provider artifact for one semantic attempt."""
    files = {}
    for name in ('10_api_invocation.json', '10_api_request.json',
                 '09_model_raw.json', '16_06_model_raw.json'):
        path = workdir / name
        if path.is_file():
            files[name] = {'path': str(path.resolve()), 'sha256': digest(path)}
    for path in (*sorted(workdir.glob('10_provider_response_*.json')),
                 *sorted(workdir.glob('10_attempt_*.json'))):
        files[path.name] = {'path': str(path.resolve()), 'sha256': digest(path)}
    return {'semantic_attempt': semantic_attempt, 'workdir': str(workdir.resolve()),
            'files': files}


def selected_ids(path):
    if path is None:
        return None, None
    path = Path(path).resolve()
    ids, bindings = [], {}
    with path.open() as stream:
        for line in stream:
            if line.strip():
                row = json.loads(line)
                pr_id = f"{row['repo']}#{row['number']}"
                ids.append(pr_id)
                if row.get('source_archive'):
                    bindings[pr_id] = str(Path(row['source_archive']).resolve())
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate PR in Stage-16 selection allowlist')
    return set(ids), {'path': str(path), 'sha256': digest(path), 'count': len(ids),
                      'source_archive_bindings': bindings}


def archived_cases(runs, allowlist=None, source_archive_bindings=None):
    cases, provenance = [], []
    for run in map(lambda value: Path(value).resolve(), runs):
        manifest_path = run / '11_manifest.json'
        manifest = json.loads(manifest_path.read_text())
        source = run / '11_source_prs.jsonl'
        if digest(source) != manifest['source_sha256']:
            raise ValueError('Stage-11 source snapshot changed: ' + str(run))
        if manifest['status'] not in ('complete', 'partial'):
            raise ValueError('Stage-11 archive is unfinished: ' + str(run))
        for index, pr_id in enumerate(manifest['pr_ids'], 1):
            record_path = run / f'11_record_{index:04d}.json'
            expected = manifest.get('files', {}).get(record_path.name)
            if expected and digest(record_path) != expected:
                raise ValueError('Stage-11 record changed: ' + record_path.name)
            record = json.loads(record_path.read_text())
            if record['instance_id'] != pr_id.replace('/', '__').replace('#', '-'):
                raise ValueError('Stage-11 record identity mismatch: ' + pr_id)
            cases.append((pr_id, record_path, record))
        provenance.append({'path': str(run), 'manifest_path': str(manifest_path),
                           'manifest_sha256': digest(manifest_path)})
    if allowlist is not None:
        source_archive_bindings = source_archive_bindings or {}
        cases = [case for case in cases if case[0] in allowlist and (
            case[0] not in source_archive_bindings
            or str(case[1].resolve()) == source_archive_bindings[case[0]])]
        if not cases:
            raise ValueError('Stage-16 selection allowlist matched no archived PRs')
    identities = [case[0] for case in cases]
    if len(identities) != len(set(identities)):
        raise ValueError('Duplicate selected PR across stage-11 archives')
    return cases, provenance


def build_packet(record_path, archive):
    sources = issue_problem_sources(archive)
    pull = archive['sections']['pull_request']['data']
    recovery_path = record_path.parent / '11_01_asset_recovery_manifest.json'
    packet = {
        'schema_version': 'text-only-repair-packet-v1',
        'case_id': archive['instance_id'],
        'repository': archive['repo'],
        'pr_number': archive['number'],
        'baseline_sha': pull['base']['sha'],
        'problem_sources': sources,
        'baseline_file_index': [],
        'withheld': ['all_image_pixels', 'image_alt_text', 'pull_request_prose', 'comments',
                     'reviews', 'commits', 'diff', 'patch', 'tests', 'reference_code'],
        'evidence_limits': {
            'historical_problem_text_verified': bool(sources) and all(
                source['historical_version_verified'] for source in sources),
            'baseline_file_contents_supplied': False,
            'repository_navigation_executed': False,
        },
        'provenance': {
            'case_manifest': None,
            'case_manifest_sha256': None,
            'source_archive': str(record_path),
            'source_archive_sha256': digest(record_path),
            'asset_recovery': str(recovery_path) if recovery_path.exists() else None,
            'asset_recovery_sha256': digest(recovery_path) if recovery_path.exists() else None,
            'baseline_archive': None,
            'baseline_archive_sha256': None,
        },
    }
    return packet, eligible_images(record_path, archive, sources)


def run_batch(archive_runs, output_root=DEFAULT_OUTPUT, tmp_root=DEFAULT_TMP,
              run_model=False, evaluator=None, visual_index=None, timeout=480,
              selected=None):
    import jsonschema
    if run_model and evaluator is None:
        raise ValueError('Explicit model run requires an evaluator')
    if timeout <= 0:
        raise ValueError('Timeout must be positive')
    jsonschema.Draft202012Validator.check_schema(json.loads(policy.SCHEMA.read_text()))
    allowlist, selection_provenance = selected_ids(selected)
    cases, archive_provenance = archived_cases(
        archive_runs, allowlist,
        (selection_provenance or {}).get('source_archive_bindings'))
    visual, visual_provenance = load_visual_index(visual_index)
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    output = Path(output_root).resolve() / run_id
    working = Path(tmp_root).resolve() / run_id
    output.mkdir(parents=True, exist_ok=False)
    working.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(policy.PROMPT, output / '16_01_system_prompt.md')
    shutil.copyfile(policy.SCHEMA, output / '16_02_output_schema.json')
    shutil.copyfile(Path(__file__), output / '16_06_packet_builder.py')
    shutil.copyfile(Path(policy.__file__), output / '16_06_policy_module.py')
    records = []
    for index, (pr_id, archive_path, archive) in enumerate(cases, 1):
        directory = working / f'16_case_{index:04d}'
        directory.mkdir()
        record = {'case_id': archive['instance_id'], 'repository': archive['repo'],
                  'pr_number': archive['number'], 'pr_id': pr_id, 'status': 'failed'}
        started = time.monotonic()
        try:
            packet, curator_assets = build_packet(archive_path, archive)
            packet_path = output / f'16_06_packet_{index:04d}.json'
            curator_path = output / f'16_06_curator_assets_{index:04d}.json'
            bound_path = output / f'16_06_bound_schema_{index:04d}.json'
            write_json(packet_path, packet)
            write_json(curator_path, {'case_id': packet['case_id'], 'assets': curator_assets})
            write_json(bound_path, policy.bind_schema(packet, output / '16_02_output_schema.json'))
            record.update(status=('prepared' if packet['problem_sources'] else 'ineligible'),
                          ineligible_reason=(None if packet['problem_sources'] else
                                             'no_linked_issue_problem_source'),
                          packet=str(packet_path), packet_sha256=digest(packet_path),
                          curator_assets=str(curator_path), curator_assets_sha256=digest(curator_path),
                          bound_schema=str(bound_path), bound_schema_sha256=digest(bound_path),
                          visual_verifier=visual.get(packet['case_id']))
            if run_model and packet['problem_sources']:
                validation_failures, provider_failures = [], []
                semantic_attempt_records = []
                failure_class = 'provider_or_infrastructure'
                invocation = None
                for semantic_attempt in (1, 2, 3):
                    attempt_dir = directory / f'semantic_{semantic_attempt:02d}'
                    attempt_dir.mkdir()
                    attempt_packet = json.loads(json.dumps(packet))
                    if validation_failures:
                        attempt_packet['previous_output_validation_error'] = validation_failures[-1]
                    try:
                        annotation, invocation = evaluator(
                            packet=attempt_packet, image_paths=[],
                            system_prompt=output / '16_01_system_prompt.md',
                            schema=bound_path, workdir=attempt_dir, timeout=timeout)
                    except Exception as exc:
                        semantic_attempt_records.append(
                            semantic_attempt_binding(attempt_dir, semantic_attempt))
                        provider_failures.append({
                            'attempt': semantic_attempt,
                            'error_type': type(exc).__name__,
                            'status_code': getattr(exc, 'status_code', None),
                        })
                        if semantic_attempt == 3:
                            record.update(
                                failure_class=failure_class,
                                invocation={
                                    'semantic_validation_attempts': semantic_attempt,
                                    'prior_validation_failures': validation_failures,
                                    'prior_provider_failures': provider_failures,
                                    'semantic_attempt_records': semantic_attempt_records,
                                })
                            raise
                        continue
                    legacy_raw = attempt_dir / '09_model_raw.json'
                    if (legacy_raw.is_file()
                            and Path(invocation['raw_response']) == legacy_raw):
                        raw = attempt_dir / '16_06_model_raw.json'
                        legacy_raw.rename(raw)
                        invocation['raw_response'] = str(raw)
                        invocation['raw_response_sha256'] = digest(raw)
                    semantic_attempt_records.append(
                        semantic_attempt_binding(attempt_dir, semantic_attempt))
                    annotation, normalization = normalize_provider_annotation(annotation)
                    try:
                        policy.validate(annotation, packet,
                                        output / '16_02_output_schema.json')
                        break
                    except Exception as exc:
                        failure_class = 'semantic_validation'
                        validation_failures.append(
                            f'{type(exc).__name__}: {str(exc)[:1200]}')
                        if semantic_attempt == 3:
                            record.update(
                                failure_class=failure_class,
                                invocation={
                                    'semantic_validation_attempts': semantic_attempt,
                                    'prior_validation_failures': validation_failures,
                                    'prior_provider_failures': provider_failures,
                                    'semantic_attempt_records': semantic_attempt_records,
                                })
                            raise
                invocation['semantic_validation_attempts'] = semantic_attempt
                invocation['prior_validation_failures'] = validation_failures
                invocation['prior_provider_failures'] = provider_failures
                invocation['semantic_attempt_records'] = semantic_attempt_records
                text = policy.text_decision(annotation)
                record.update(status='complete', annotation=annotation, text_decision=text,
                              provider_annotation_normalization=normalization,
                              reconciliation=policy.reconcile(
                                  (record['visual_verifier'] or {}).get('decision'), text),
                              invocation=invocation)
        except Exception as exc:
            record.update(status='failed', error=f'{type(exc).__name__}: {str(exc)[:1200]}')
        record['elapsed_seconds'] = round(time.monotonic() - started, 3)
        write_json(output / f'16_03_result_{index:04d}.json', record)
        records.append(record)
        print(json.dumps({'pr_id': pr_id, 'status': record['status'],
                          'queue': (record.get('reconciliation') or {}).get('queue')},
                         ensure_ascii=False), flush=True)
    if run_model:
        status = ('complete_with_failures' if any(r['status'] == 'failed' for r in records)
                  else 'complete_with_ineligible' if any(r['status'] == 'ineligible' for r in records)
                  else 'complete')
    else:
        status = ('prepared_with_failures' if any(r['status'] == 'failed' for r in records)
                  else 'prepared_with_ineligible' if any(r['status'] == 'ineligible' for r in records)
                  else 'prepared')
    manifest = {
        'schema_version': 'text-only-verifier-run-v2', 'input_mode': 'stage11_source_archives',
        'run_id': run_id, 'status': status, 'model_invoked': run_model,
        'purpose': 'cross-repository IID candidate triage; human acceptance required',
        'archive_runs': archive_provenance,
        'selection_allowlist': selection_provenance,
        'pr_numbers': [record['pr_number'] for record in records],
        'pr_ids': [record['pr_id'] for record in records],
        'case_ids': [record['case_id'] for record in records],
        'status_counts': {key: sum(r['status'] == key for r in records)
                          for key in ('prepared', 'complete', 'ineligible', 'failed')},
        'visual_index': visual_provenance,
        'prompt_sha256': digest(output / '16_01_system_prompt.md'),
        'schema_sha256': digest(output / '16_02_output_schema.json'),
        'policy_version': policy.POLICY_VERSION,
        'packet_builder_sha256': digest(output / '16_06_packet_builder.py'),
        'policy_module_sha256': digest(output / '16_06_policy_module.py'),
        'temporary_directory': str(working), 'withheld_from_model': True,
        'baseline_file_index_available': False,
        'agent_ablation': 'not_run_optional_after_human_review',
    }
    write_json(output / '16_03_run_manifest.json', manifest)
    return output, sum(record['status'] == 'failed' for record in records)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive-run', type=Path, action='append', required=True)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--tmp', type=Path, default=DEFAULT_TMP)
    parser.add_argument('--visual-index', type=Path)
    parser.add_argument('--selected', type=Path,
                        help='Optional JSONL allowlist; archive cases outside it are not invoked.')
    parser.add_argument('--run', action='store_true')
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
    output, failed = run_batch(args.archive_run, args.output, args.tmp, args.run, evaluator,
                               args.visual_index, args.timeout, args.selected)
    print(json.dumps({'output': str(output), 'failed': failed}, ensure_ascii=False))
    if failed:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
