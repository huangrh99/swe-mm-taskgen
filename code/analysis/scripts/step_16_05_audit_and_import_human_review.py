"""Audit frozen stage-16 evidence and optionally import a human decision export."""

import argparse
import base64
from collections import Counter
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
from report_pipeline.paths import CODE_ROOT, WORKSPACE_ROOT
from report_pipeline.atomic import write_bytes, write_json

ROOT = WORKSPACE_ROOT
sys.path.insert(0, str(CODE_ROOT))
from pr_crawler import repair_sufficiency as policy


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def directory_digest(path):
    root = Path(path)
    entries = [{'path': item.relative_to(root).as_posix(), 'sha256': digest(item)}
               for item in sorted(root.rglob('*')) if item.is_file()]
    return hashlib.sha256(json.dumps(entries, separators=(',', ':')).encode()).hexdigest()


def _import_paths(run, output):
    return ({
        'original': run / '16_05_human_decisions.original.json',
        'selection': run / '16_05_human_confirmed_visual_candidates.json',
        'audit': output,
    }, run / '.16_05_human_import.transaction.json',
        run / '16_05_human_import.commit.json')


def _validate_import_entries(run, output, entries):
    targets, _, _ = _import_paths(run, output)
    if (not isinstance(entries, list) or len(entries) != len(targets)
            or {item.get('name') for item in entries if isinstance(item, dict)} != set(targets)):
        raise ValueError('Human import transaction inventory is invalid')
    validated = []
    for entry in entries:
        target = targets[entry['name']]
        staging_name = entry.get('staging')
        if (entry.get('target') != target.name or not isinstance(staging_name, str)
                or '/' in staging_name or not staging_name.startswith('.16_05_')
                or not re.fullmatch(r'[0-9a-f]{64}', str(entry.get('sha256', '')))):
            raise ValueError('Human import transaction entry is invalid')
        validated.append((entry, target, run / staging_name))
    return validated


def _recover_human_import(run, output):
    targets, transaction, commit = _import_paths(run, output)
    if commit.exists():
        if commit.is_symlink():
            raise ValueError('Human import commit is unsafe')
        value = json.loads(commit.read_text())
        entries = _validate_import_entries(run, output, value.get('entries'))
        if (value.get('schema_version') != 'visual-human-import-commit-v1'
                or not re.fullmatch(r'[0-9a-f]{64}', str(value.get('transaction_sha256', '')))):
            raise ValueError('Human import commit is invalid')
        for entry, target, _ in entries:
            if target.is_symlink() or not target.is_file() or digest(target) != entry['sha256']:
                raise ValueError('Committed human import artifact changed')
        if transaction.exists():
            if digest(transaction) != value['transaction_sha256']:
                raise ValueError('Committed human import transaction changed')
            transaction.unlink()
        return json.loads(targets['audit'].read_text())
    if not transaction.exists():
        orphan_pattern = re.compile(r'\.16_05_(?:original|selection|audit)\.[0-9a-f]{32}\.staging')
        for artifact in run.iterdir():
            if orphan_pattern.fullmatch(artifact.name):
                if artifact.is_symlink() or artifact.is_file():
                    artifact.unlink()
                else:
                    raise ValueError('Human import orphan staging is not a regular file')
        return None
    if transaction.is_symlink():
        raise ValueError('Human import transaction is unsafe')
    value = json.loads(transaction.read_text())
    if value.get('schema_version') != 'visual-human-import-transaction-v1':
        raise ValueError('Human import transaction is invalid')
    for entry, target, staging in _validate_import_entries(run, output, value.get('entries')):
        for artifact in (target, staging):
            if not artifact.exists():
                continue
            if artifact.is_symlink() or not artifact.is_file() or digest(artifact) != entry['sha256']:
                raise ValueError('Interrupted human import artifact changed')
            artifact.unlink()
    transaction.unlink()
    return None


def _audit_unlocked(run, human_decisions=None, output=None):
    run = Path(run).resolve()
    output = Path(output).resolve() if output else run / '16_05_audit.json'
    if human_decisions and output != run / '16_05_audit.json':
        raise ValueError('Human import audit output must be the run-bound 16_05_audit.json')
    manifest_path = run / '16_03_run_manifest.json'
    manifest = json.loads(manifest_path.read_text())
    if manifest['prompt_sha256'] != digest(run / '16_01_system_prompt.md') or manifest['schema_sha256'] != digest(run / '16_02_output_schema.json'):
        raise ValueError('Frozen prompt or schema changed')
    if manifest['policy_version'] != policy.POLICY_VERSION:
        raise ValueError('Policy version mismatch')
    if manifest['packet_builder_sha256'] != digest(run / '16_03_packet_builder.py') or manifest['policy_module_sha256'] != digest(run / '16_03_policy_module.py'):
        raise ValueError('Frozen packet builder or policy module changed')
    if manifest['pilot_manifest_sha256'] != digest(manifest['pilot_manifest']):
        raise ValueError('Pilot manifest changed')
    case_ids, packet_hashes = [], {}
    for index, number in enumerate(manifest['pr_numbers'], 1):
        result_path = run / f'16_03_result_{index:04d}.json'
        record = json.loads(result_path.read_text())
        if record['pr_number'] != number or record['status'] not in ('prepared', 'complete', 'ineligible'):
            raise ValueError('Missing or failed case cannot pass evidence audit')
        packet_path, curator_path, schema_path = map(Path, (record['packet'], record['curator_assets'], record['bound_schema']))
        if digest(packet_path) != record['packet_sha256'] or digest(curator_path) != record['curator_assets_sha256'] or digest(schema_path) != record['bound_schema_sha256']:
            raise ValueError('Case packet, curator assets or schema changed')
        packet, curator = json.loads(packet_path.read_text()), json.loads(curator_path.read_text())
        if packet['case_id'] != record['case_id'] or curator['case_id'] != record['case_id']:
            raise ValueError('Case identity mismatch')
        if json.loads(schema_path.read_text()) != policy.bind_schema(packet, run / '16_02_output_schema.json'):
            raise ValueError('Bound schema differs from packet')
        serialized = packet_path.read_text()
        if any(asset.get('url') and asset['url'] in serialized or asset.get('local_path') and asset['local_path'] in serialized
               for asset in curator['assets']):
            raise ValueError('Curator-only visual location leaked into model packet')
        for source in packet['problem_sources']:
            if '![' in source['text'] or '<img' in source['text'].lower() or 'user-attachments/assets/' in source['text']:
                raise ValueError('Image syntax or URL remained in text-only source')
            if hashlib.sha256(source['text'].encode()).hexdigest() != source['text_sha256']:
                raise ValueError('Masked source text hash mismatch')
        for key in ('case_manifest', 'source_archive', 'baseline_archive'):
            if digest(packet['provenance'][key]) != packet['provenance'][key + '_sha256']:
                raise ValueError('Upstream provenance changed: ' + key)
        for asset in curator['assets']:
            if asset['status'] == 'available' and digest(asset['local_path']) != asset['sha256']:
                raise ValueError('Curator visual asset changed')
        if record['status'] == 'complete':
            policy.validate(record['annotation'], packet, run / '16_02_output_schema.json')
            decision = policy.text_decision(record['annotation'])
            if decision != record['text_decision']:
                raise ValueError('Stored text-only decision changed')
            expected = policy.reconcile((record.get('visual_verifier') or {}).get('decision'), decision)
            if expected != record['reconciliation']:
                raise ValueError('Stored reconciliation changed')
            invocation = record['invocation']
            if digest(invocation['raw_response']) != invocation['raw_response_sha256']:
                raise ValueError('Raw model response changed')
        case_ids.append(record['case_id'])
        packet_hashes[record['case_id']] = record['packet_sha256']
    if case_ids != manifest['case_ids'] or len(case_ids) != len(set(case_ids)):
        raise ValueError('Manifest case identity/order mismatch')
    review_manifest_path = run / '16_04_review_manifest.json'
    review_manifest = json.loads(review_manifest_path.read_text())
    transaction_path = run / '.16_04_review_bundle.transaction.json'
    commit_path = run / '16_04_review_bundle.commit.json'
    if transaction_path.exists() or transaction_path.is_symlink():
        raise ValueError('Review bundle publication is incomplete')
    if commit_path.is_symlink() or not commit_path.is_file():
        raise ValueError('Review bundle commit is missing or unsafe')
    commit = json.loads(commit_path.read_text())
    entries = commit.get('entries')
    targets = {
        'html': run / review_manifest['html'],
        'assets': run / review_manifest['review_assets'],
        'builder': run / review_manifest['builder'],
        'seed': run / review_manifest['seed'],
        'manifest': review_manifest_path,
    }
    if (commit.get('schema_version') != 'visual-review-bundle-commit-v1'
            or not isinstance(entries, list) or len(entries) != len(targets)
            or {item.get('name') for item in entries if isinstance(item, dict)} != set(targets)):
        raise ValueError('Review bundle commit inventory is invalid')
    for entry in entries:
        target = targets[entry['name']]
        if entry.get('kind') == 'directory':
            if target.is_symlink() or not target.is_dir() or directory_digest(target) != entry.get('sha256'):
                raise ValueError('Review bundle asset changed after commit')
        elif entry.get('kind') == 'file':
            if target.is_symlink() or not target.is_file() or digest(target) != entry.get('sha256'):
                raise ValueError('Review bundle file changed after commit: ' + target.name)
        else:
            raise ValueError('Review bundle commit entry kind is invalid')
    bundle_sha256 = hashlib.sha256(
        json.dumps(entries, separators=(',', ':')).encode()).hexdigest()
    if commit.get('bundle_sha256') != bundle_sha256:
        raise ValueError('Review bundle commit hash is invalid')
    if review_manifest['run_manifest_sha256'] != digest(manifest_path):
        raise ValueError('Review page bound to another run manifest')
    classification_path_value = review_manifest.get('pre_review_classification')
    classification_sha256 = review_manifest.get('pre_review_classification_sha256')
    classification_ready = review_manifest.get('pre_review_classification_ready') is True
    if review_manifest.get('pre_review_classification_complete') is not classification_ready:
        raise ValueError('Review classification readiness is inconsistent')
    expected_review_status = ('ready_for_human_review' if classification_ready
                              else 'materials_only_pre_review_classification_incomplete')
    if review_manifest.get('status') != expected_review_status:
        raise ValueError('Review readiness status is inconsistent')
    if classification_path_value:
        classification_path = Path(classification_path_value).resolve()
        if (not classification_path.is_file()
                or digest(classification_path) != classification_sha256):
            raise ValueError('Pre-review classification changed')
    elif classification_sha256 is not None or classification_ready:
        raise ValueError('Review classification binding is incomplete')
    for filename, key in [(review_manifest['html'], 'html_sha256'), (review_manifest['seed'], 'seed_sha256')]:
        if digest(run / filename) != review_manifest[key]:
            raise ValueError('Review artifact changed: ' + filename)
    if digest(run / review_manifest['builder']) != review_manifest['builder_sha256']:
        raise ValueError('Frozen review builder changed')
    page = (run / review_manifest['html']).read_text()
    match = re.search(r"atob\('([^']+)'\)", page)
    if not match:
        raise ValueError('Review page dataset missing')
    embedded = json.loads(base64.b64decode(match.group(1)))
    if [row['case_id'] for row in embedded['rows']] != case_ids or embedded['manifest_sha256'] != digest(manifest_path):
        raise ValueError('Review page case binding mismatch')
    if (embedded.get('classification_path') != classification_path_value
            or embedded.get('classification_sha256') != classification_sha256
            or (embedded.get('classification_ready') is True) != classification_ready):
        raise ValueError('Review page classification binding mismatch')
    seed = json.loads((run / review_manifest['seed']).read_text())
    if (seed.get('pre_review_classification') != classification_path_value
            or seed.get('pre_review_classification_sha256') != classification_sha256
            or (seed.get('pre_review_classification_ready') is True) != classification_ready):
        raise ValueError('Review seed classification binding mismatch')
    scripts = re.findall(r'<script>(.*?)</script>', page, re.DOTALL)
    node = shutil.which('node')
    if node:
        for script in scripts:
            subprocess.run([node, '--check'], input=script.encode(), capture_output=True, check=True, timeout=10)
    result = {'status': 'passed', 'run_manifest_sha256': digest(manifest_path), 'cases': len(case_ids),
              'model_invoked': manifest['model_invoked'], 'review_page_sha256': review_manifest['html_sha256'],
              'pre_review_classification': classification_path_value,
              'pre_review_classification_sha256': classification_sha256,
              'pre_review_classification_ready': classification_ready,
              'review_script_syntax_checked': bool(node),
              'review_bundle_commit_sha256': digest(commit_path),
              'review_bundle_sha256': bundle_sha256,
              'auditor_sha256': digest(Path(__file__)),
              'human_decisions_imported': False, 'agent_ablation': 'not_run'}
    if human_decisions:
        if not classification_ready:
            raise ValueError('Review is not ready for human review import')
        human_decisions = Path(human_decisions).resolve()
        value = json.loads(human_decisions.read_text())
        if value.get('schema_version') != 'visual-necessity-human-export-v1' or value.get('source_manifest_sha256') != digest(manifest_path):
            raise ValueError('Human export is not bound to this run')
        if (value.get('pre_review_classification') != classification_path_value
                or value.get('pre_review_classification_sha256') != classification_sha256
                or value.get('pre_review_classification_ready') is not True):
            raise ValueError('Human export classification binding mismatch')
        rows = value.get('rows', [])
        if [row.get('case_id') for row in rows] != case_ids:
            raise ValueError('Human export case identity/order mismatch')
        completed = []
        for row in rows:
            if row.get('packet_sha256') != packet_hashes[row['case_id']]:
                raise ValueError('Human decision bound to changed packet')
            if row.get('decision') is not None:
                policy.validate_human_record(row)
                completed.append(row)
        targets, transaction, commit = _import_paths(run, output)
        recovered = _recover_human_import(run, output)
        if recovered is not None:
            if digest(human_decisions) != digest(targets['original']):
                raise ValueError('Committed human export differs from retry input')
            return recovered
        if any(path.exists() or path.is_symlink() for path in targets.values()):
            raise FileExistsError('Human import destination exists without a commit')
        original = targets['original']
        labels = Counter(row['decision'] for row in completed)
        selected = [row for row in completed if row['decision'] == 'human_confirmed_visual_candidate']
        source_human_sha256 = digest(human_decisions)
        selection = {'schema_version': 'iid-visual-selection-v1', 'source_human_sha256': source_human_sha256,
                     'pre_review_classification': classification_path_value,
                     'pre_review_classification_sha256': classification_sha256,
                     'pre_review_classification_ready': classification_ready,
                     'selected_count': len(selected), 'minimum_target': 5,
                     'target_met': len(selected) >= 5, 'cases': selected,
                     'empirical_visual_dependence': 'not_tested_unless_case_ablation_record_says_otherwise'}
        selection_path = targets['selection']
        result.update(human_decisions_imported=True, human_export_sha256=source_human_sha256,
                      label_counts=dict(labels), reviewed=len(completed), pending=len(rows) - len(completed),
                      selected=len(selected), target_met=len(selected) >= 5,
                      selection=selection_path.name)
        token = secrets.token_hex(16)
        staging = {
            name: run / f'.16_05_{name}.{token}.staging' for name in targets
        }
        write_bytes(staging['original'], human_decisions.read_bytes())
        write_json(staging['selection'], selection)
        result['selection_sha256'] = digest(staging['selection'])
        write_json(staging['audit'], result)
        entries = [
            {'name': name, 'target': targets[name].name, 'staging': staging[name].name,
             'sha256': digest(staging[name])}
            for name in ('original', 'selection', 'audit')
        ]
        write_json(transaction, {
            'schema_version': 'visual-human-import-transaction-v1',
            'entries': entries,
        })
        for name in ('original', 'selection', 'audit'):
            staging[name].replace(targets[name])
        write_json(commit, {
            'schema_version': 'visual-human-import-commit-v1',
            'transaction_sha256': digest(transaction),
            'entries': entries,
        })
        transaction.unlink()
    return result


def audit(run, human_decisions=None, output=None):
    if not human_decisions:
        return _audit_unlocked(run, human_decisions, output)
    run = Path(run).resolve()
    if run.is_symlink() or not run.is_dir():
        raise ValueError('Human import run directory is unsafe')
    directory = os.open(
        run, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0))
    descriptor = os.open(
        '.16_05_human_import.lock',
        os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600,
        dir_fd=directory,
    )
    os.close(directory)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Human import is already in progress') from None
        return _audit_unlocked(run, human_decisions, output)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--human-decisions', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    output = args.output.resolve() if args.output else args.run.resolve() / '16_05_audit.json'
    value = audit(args.run, args.human_decisions, output)
    if not args.human_decisions and output.exists():
        raise FileExistsError(output)
    if not args.human_decisions:
        write_json(output, value)
    print(output)


if __name__ == '__main__':
    main()
