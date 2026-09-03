import json
from pathlib import Path
import shutil
import tempfile
import unittest

from analysis.scripts import step_16_07_aggregate_runs as aggregate
from analysis.scripts import step_16_04_export_human_review as review
from analysis.scripts import step_16_06_prepare_and_run_archived_text_only_verifier as archived_runner
from pr_crawler import repair_sufficiency as policy


class AggregateRunsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_stage16_selection_allowlist_is_hash_bound_and_deduplicated(self):
        selected = self.root / 'allowlist.jsonl'
        selected.write_text(json.dumps({'repo': 'o/r', 'number': 1}) + '\n')
        values, provenance = archived_runner.selected_ids(selected)
        self.assertEqual(values, {'o/r#1'})
        self.assertEqual(provenance['sha256'], aggregate.digest(selected))
        self.assertEqual(provenance['source_archive_bindings'], {})
        selected.write_text(selected.read_text() + json.dumps({'repo': 'o/r', 'number': 1}) + '\n')
        with self.assertRaisesRegex(ValueError, 'Duplicate PR'):
            archived_runner.selected_ids(selected)

    def make_run(self, name, cases):
        run = self.root / name
        run.mkdir()
        shutil.copyfile(policy.PROMPT, run / '16_01_system_prompt.md')
        shutil.copyfile(policy.SCHEMA, run / '16_02_output_schema.json')
        builder = run / '16_06_packet_builder.py'
        module = run / '16_06_policy_module.py'
        builder.write_text('# frozen packet builder\n')
        module.write_text('# frozen policy module\n')
        pr_ids, numbers, case_ids = [], [], []
        for index, (repo, number) in enumerate(cases, 1):
            pr_id = f'{repo}#{number}'
            case_id = repo.replace('/', '__') + '-' + str(number)
            archive = run / f'archive_{index:04d}.json'
            archive.write_text(json.dumps({
                'instance_id': case_id, 'repo': repo, 'number': number,
                'archival_view': {'documents': [
                    {'source_id': 'pr:title', 'kind': 'pr', 'field': 'title',
                     'text': f'PR {number}'},
                    {'source_id': 'pr:body', 'kind': 'pr', 'field': 'body', 'text': ''},
                ]},
                'sections': {'assets': {'items': []}},
            }))
            packet = {'schema_version': 'text-only-repair-packet-v1',
                      'case_id': case_id, 'repository': repo, 'pr_number': number,
                      'baseline_sha': 'abc', 'problem_sources': [],
                      'baseline_file_index': [], 'withheld': [], 'evidence_limits': {},
                      'provenance': {'source_archive': str(archive)}}
            packet_path = run / f'16_06_packet_{index:04d}.json'
            curator_path = run / f'16_06_curator_assets_{index:04d}.json'
            schema_path = run / f'16_06_bound_schema_{index:04d}.json'
            packet_path.write_text(json.dumps(packet))
            curator_path.write_text(json.dumps({'case_id': case_id, 'assets': []}))
            schema_path.write_text(json.dumps(policy.bind_schema(
                packet, run / '16_02_output_schema.json')))
            result = {
                'case_id': case_id, 'repository': repo, 'pr_number': number,
                'pr_id': pr_id, 'status': 'ineligible',
                'ineligible_reason': 'no_linked_issue_problem_source',
                'packet': str(packet_path), 'packet_sha256': aggregate.digest(packet_path),
                'curator_assets': str(curator_path),
                'curator_assets_sha256': aggregate.digest(curator_path),
                'bound_schema': str(schema_path),
                'bound_schema_sha256': aggregate.digest(schema_path),
                'visual_verifier': None,
            }
            (run / f'16_03_result_{index:04d}.json').write_text(json.dumps(result))
            pr_ids.append(pr_id)
            numbers.append(number)
            case_ids.append(case_id)
        manifest = {
            'schema_version': 'text-only-verifier-run-v2',
            'input_mode': 'stage11_source_archives', 'run_id': name,
            'status': 'complete_with_ineligible', 'model_invoked': False,
            'pr_ids': pr_ids, 'pr_numbers': numbers, 'case_ids': case_ids,
            'prompt_sha256': aggregate.digest(run / '16_01_system_prompt.md'),
            'schema_sha256': aggregate.digest(run / '16_02_output_schema.json'),
            'policy_version': policy.POLICY_VERSION,
            'packet_builder_sha256': aggregate.digest(builder),
            'policy_module_sha256': aggregate.digest(module),
        }
        (run / '16_03_run_manifest.json').write_text(json.dumps(manifest))
        return run

    def selection(self, cases):
        path = self.root / '08_02_selected_100_prs.jsonl'
        path.write_text(''.join(json.dumps({'repo': repo, 'number': number}) + '\n'
                                for repo, number in cases))
        return path

    def test_aggregates_in_selected_order_and_feeds_existing_review_exporter(self):
        cases = [('owner/repo', number) for number in range(1, 101)]
        first = self.make_run('first', cases[50:])
        second = self.make_run('second', cases[:50])
        selected = self.selection(cases)
        output = aggregate.aggregate([first, second], selected, self.root / 'output')
        manifest = json.loads((output / '16_03_run_manifest.json').read_text())
        self.assertEqual([f'owner/repo#{number}' for number in range(1, 101)],
                         manifest['pr_ids'])
        self.assertEqual(100, len(manifest['source_cases']))
        self.assertTrue(manifest['model_results_reused'])
        original = second / '16_03_result_0001.json'
        self.assertEqual(original.read_bytes(),
                         (output / '16_03_result_0001.json').read_bytes())
        loaded_manifest, rows = review.load_rows(output)
        self.assertEqual(manifest['run_id'], loaded_manifest['run_id'])
        self.assertEqual(100, len(rows))
        self.assertEqual('owner__repo-1', rows[0]['case_id'])
        self.assertEqual('owner__repo-100', rows[-1]['case_id'])

    def test_rejects_duplicate_case_across_runs(self):
        cases = [('owner/repo', number) for number in range(1, 101)]
        first = self.make_run('first', cases[:51])
        second = self.make_run('second', cases[50:])
        with self.assertRaisesRegex(ValueError, 'Duplicate PR across'):
            aggregate.aggregate([first, second], self.selection(cases),
                                self.root / 'output')

    def test_resolves_failed_retry_only_when_frozen_inputs_match(self):
        cases = [('owner/repo', number) for number in range(1, 101)]
        first = self.make_run('first', cases)
        retry = self.root / 'retry'
        retry.mkdir()
        for filename in ('16_01_system_prompt.md', '16_02_output_schema.json',
                         '16_06_packet_builder.py', '16_06_policy_module.py'):
            shutil.copyfile(first / filename, retry / filename)
        original = json.loads((first / '16_03_result_0001.json').read_text())
        failed = dict(original)
        failed['status'] = 'failed'
        failed['error'] = 'provider timeout'
        (first / '16_03_result_0001.json').write_text(json.dumps(failed))
        prepared = dict(original)
        prepared['status'] = 'prepared'
        (retry / '16_03_result_0001.json').write_text(json.dumps(prepared))
        first_manifest = json.loads((first / '16_03_run_manifest.json').read_text())
        retry_manifest = dict(first_manifest)
        retry_manifest.update(run_id='retry', pr_ids=['owner/repo#1'],
                              pr_numbers=[1], case_ids=['owner__repo-1'])
        (retry / '16_03_run_manifest.json').write_text(json.dumps(retry_manifest))
        output = aggregate.aggregate([first, retry], self.selection(cases),
                                     self.root / 'output', allow_retry_overrides=True)
        manifest = json.loads((output / '16_03_run_manifest.json').read_text())
        self.assertTrue(manifest['retry_resolution_enabled'])
        resolution = manifest['source_cases'][0]['retry_resolution']
        self.assertEqual('prepared', resolution['selected_status'])
        self.assertEqual(['failed', 'prepared'],
                         [item['status'] for item in resolution['attempts']])

    def test_retry_resolution_rejects_changed_frozen_input(self):
        cases = [('owner/repo', number) for number in range(1, 101)]
        first = self.make_run('first', cases)
        second = self.make_run('second', [('owner/repo', 1)])
        with self.assertRaisesRegex(ValueError, 'differ in frozen input binding'):
            aggregate.aggregate([first, second], self.selection(cases),
                                self.root / 'output', allow_retry_overrides=True)

    def test_rejects_missing_case_and_tampered_dependency(self):
        cases = [('owner/repo', number) for number in range(1, 101)]
        first = self.make_run('first', cases[:50])
        second = self.make_run('second', cases[50:99])
        with self.assertRaisesRegex(ValueError, 'missing from Stage-16'):
            aggregate.aggregate([first, second], self.selection(cases),
                                self.root / 'missing-output')
        second = self.make_run('tampered', cases[50:])
        (second / '16_06_curator_assets_0001.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'dependency changed'):
            aggregate.aggregate([first, second], self.selection(cases),
                                self.root / 'tampered-output')

    def test_protocol_recovery_uses_hash_bound_source_contract(self):
        source = self.make_run('source', [('owner/repo', 1)])
        recovery = self.root / 'recovery'
        shutil.copytree(source, recovery)
        (recovery / '16_06_packet_builder.py').unlink()
        (recovery / '16_06_policy_module.py').unlink()
        manifest_path = recovery / '16_03_run_manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest.update({
            'protocol_recovery': 'test-recovery-v1',
            'source_run': str(source),
            'source_run_manifest_sha256': aggregate.digest(
                source / '16_03_run_manifest.json'),
        })
        manifest_path.write_text(json.dumps(manifest))
        loaded = aggregate.load_run(recovery)
        self.assertEqual('owner/repo#1', loaded['records'][0][0])
        (source / '16_03_run_manifest.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'source manifest changed'):
            aggregate.load_run(recovery)


if __name__ == '__main__':
    unittest.main()
