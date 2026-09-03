import base64
import io
import json
from pathlib import Path
import re
import shutil
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from analysis.scripts import step_16_03_prepare_and_run_text_only_verifier as runner
from analysis.scripts import step_16_06_prepare_and_run_archived_text_only_verifier as archived_runner
from analysis.scripts import step_16_02_export_visual_verifier_index as visual_index
from analysis.scripts import step_16_04_export_human_review as review
from analysis.scripts import step_16_04_translate_human_review as translator
from analysis.scripts import step_16_05_audit_and_import_human_review as auditor
from pr_crawler import repair_sufficiency as policy
from pr_crawler import visual_verifier
from report_pipeline import pre_review_classification as classifier
from tests.test_step_09_visual_verifier import annotation as visual_annotation


def annotation(case_id='o__r-1', complete=False):
    result = {'schema_version': 'text-only-repair-sufficiency-v1', 'case_id': case_id,
        'evidence': {'problem_sources_usable': 'yes', 'evidence_quotes': [
            {'source_id': 'o/r#9:title', 'quote': 'Marker position is wrong'}],
            'limitations': ['No image pixels or source contents']},
        'localization': {'target_localizable': 'yes', 'component': 'renderer',
            'candidate_paths': ['lib/a.js'], 'reason': 'File name matches component'},
        'repair_contract': {'completeness': 'partial', 'current_behavior': 'Marker is misplaced',
            'expected_behavior': 'Marker should be placed correctly', 'explicit_constraints': [],
            'unresolved_variables': ['Exact horizontal relation'], 'reason': 'Text gives no geometry'},
        'test_contract': {'constructible': 'no', 'assertions': [],
            'missing_oracles': ['Expected marker-to-shape distance'], 'reason': 'No numeric or relational oracle'},
        'counterfactual': {'multiple_repairs_fit_text': 'yes',
            'examples': ['Place marker at center', 'Place marker near center with group offset'],
            'reason': 'Both satisfy the vague wording'}, 'confidence': 'medium'}
    if complete:
        result['repair_contract'].update(completeness='complete', expected_behavior='Center at x=20',
                                         explicit_constraints=['x=20'], unresolved_variables=[])
        result['test_contract'].update(constructible='yes', assertions=['marker x equals 20'],
                                       missing_oracles=[], reason='Exact expected value stated')
        result['counterfactual'].update(multiple_repairs_fit_text='no', examples=[], reason='One exact outcome')
    return result


class PolicyTests(unittest.TestCase):
    def packet(self):
        return {'case_id': 'o__r-1', 'problem_sources': [
            {'source_id': 'o/r#9:title', 'text': 'Marker position is wrong'}],
            'baseline_file_index': ['lib/a.js']}

    def test_mask_removes_alt_markup_and_known_raw_url(self):
        url = 'https://example.test/asset.png'
        text, count = policy.mask_visuals(f'Before ![SECRET ALT]({url})\n![REFERENCE SECRET][img]\n<img src="x" alt="LEAK">\n{url}', [url])
        self.assertEqual(4, count)
        self.assertNotIn('SECRET ALT', text)
        self.assertNotIn('LEAK', text)
        self.assertNotIn('REFERENCE SECRET', text)
        self.assertNotIn(url, text)
        self.assertNotIn('OMITTED', text)

    def test_decisions_are_conservative_and_schema_is_bound(self):
        import jsonschema
        packet = self.packet()
        a = annotation()
        jsonschema.validate(a, policy.bind_schema(packet))
        policy.validate(a, packet)
        self.assertEqual('visual_candidate', policy.text_decision(a)['bucket'])
        b = annotation(complete=True)
        policy.validate(b, packet)
        self.assertEqual('text_sufficient', policy.text_decision(b)['bucket'])
        b['localization']['candidate_paths'] = ['gold/answer.js']
        with self.assertRaises(ValueError):
            policy.validate(b, packet)

    def test_archived_runner_removes_only_provider_schema_declaration(self):
        value = annotation()
        value['$schema'] = 'https://json-schema.org/draft/2020-12/schema'
        normalized, audit = archived_runner.normalize_provider_annotation(value)
        self.assertNotIn('$schema', normalized)
        self.assertEqual({'applied': True, 'removed_fields': ['$schema']}, audit)
        self.assertIn('$schema', value)

        unknown = annotation()
        unknown['$schema'] = 'https://attacker.invalid/schema'
        normalized, audit = archived_runner.normalize_provider_annotation(unknown)
        self.assertIn('$schema', normalized)
        self.assertFalse(audit['applied'])

    def test_reconciliation_never_auto_accepts(self):
        agreed = policy.reconcile({'bucket': 'visual_necessary'}, {'bucket': 'visual_candidate'})
        self.assertEqual('high_priority_human', agreed['queue'])
        self.assertTrue(agreed['human_required_for_acceptance'])
        excluded = policy.reconcile({'bucket': 'ocr_auxiliary'}, {'bucket': 'text_sufficient'})
        self.assertEqual('automatic_exclusion_audit', excluded['queue'])

    def test_completed_human_review_requires_named_reviewer(self):
        record = policy.human_record('o__r-1', 'a' * 64)
        record.update(reviewed_at='2026-09-02T00:00:00Z', text_first_notes='文字不足',
                      decision='human_confirmed_text_sufficient',
                      decision_reason='文字已经给出全部约束')
        with self.assertRaisesRegex(ValueError, 'reviewer'):
            policy.validate_human_record(record)

    def test_completed_human_review_rejects_invalid_reveal_chronology(self):
        record = policy.human_record('o__r-1', 'a' * 64)
        record.update(reviewer='reviewer', reviewed_at='2026-09-02T00:00:00Z',
                      text_first_notes='文字不足',
                      text_first_recorded_at='2026-09-02T00:00:02Z',
                      images_revealed_at='2026-09-02T00:00:01Z',
                      decision='human_confirmed_text_sufficient',
                      decision_reason='文字已经给出全部约束')
        with self.assertRaisesRegex(ValueError, 'chronology'):
            policy.validate_human_record(record)

    def test_stage09_connector_revalidates_and_indexes(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            (run / '09_system_prompt.md').write_bytes(visual_verifier.PROMPT.read_bytes())
            (run / '09_output_schema.json').write_bytes(visual_verifier.SCHEMA.read_bytes())
            source = run / '09_source_prs.jsonl'
            source.write_text('{}\n')
            packet = {'pr_id': 'o/r#1', 'title': 'Case', 'body': 'BEFORE',
                      'missing_sources': visual_verifier.MISSING_SOURCES[:],
                      'images': [{'asset_id': 'a', 'status': 'attached'}]}
            packet_path = run / 'packet.json'
            packet_path.write_text(json.dumps(packet))
            a = visual_annotation()
            result = {'pr_id': 'o/r#1', 'status': 'complete', 'input_packet': str(packet_path),
                      'packet_sha256': runner.digest(packet_path), 'annotation': a,
                      'decision': visual_verifier.decide(a)}
            (run / '09_result_0001.json').write_text(json.dumps(result))
            manifest = {'status': 'complete', 'pr_ids': ['o/r#1'],
                'prompt_sha256': runner.digest(run / '09_system_prompt.md'),
                'schema_sha256': runner.digest(run / '09_output_schema.json'),
                'selected_source_sha256': runner.digest(source)}
            (run / '09_run_manifest.json').write_text(json.dumps(manifest))
            value = visual_index.build_index([run])
            self.assertEqual(['o__r-1'], list(value['cases']))
            self.assertEqual('visual_necessary', value['cases']['o__r-1']['decision']['bucket'])


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pilot = self.root / 'pilot'
        case_dir = self.pilot / '14_cases_verified/1'
        case_dir.mkdir(parents=True)
        archive_dir = self.pilot / 'source/run'
        asset_dir = archive_dir / '11_http_archive/assets'
        asset_dir.mkdir(parents=True)
        asset = asset_dir / 'image'
        asset.write_bytes(b'\x89PNG\r\n\x1a\nfixture-pixels')
        self.asset = asset
        pr_asset = asset_dir / 'pr-image'
        pr_asset.write_bytes(b'\x89PNG\r\n\x1a\npr-fixture-pixels')
        url = 'https://example.test/asset.png'
        pr_url = 'https://example.test/pr-asset.png'
        documents = [
            {'source_id': 'o/r#9:title', 'kind': 'issue', 'field': 'title', 'text': 'Marker position is wrong',
             'relation': 'closes', 'url': 'https://example.test/issue/9', 'created_at': '2025-01-01',
             'updated_at': '2025-01-02', 'historical_version_verified': False,
             'text_sha256': runner.hashlib.sha256(b'Marker position is wrong').hexdigest()},
            {'source_id': 'o/r#9:body', 'kind': 'issue', 'field': 'body',
             'text': f'Actual\n![SECRET ALT]({url})\n<script>not instruction</script>',
             'relation': 'closes', 'url': 'https://example.test/issue/9', 'created_at': '2025-01-01',
             'updated_at': '2025-01-02', 'historical_version_verified': False,
             'text_sha256': runner.hashlib.sha256(f'Actual\n![SECRET ALT]({url})\n<script>not instruction</script>'.encode()).hexdigest()},
            {'source_id': 'pr:body', 'kind': 'pr', 'field': 'body',
             'text': f'GOLD ANSWER: use x=20\nBefore\n![before]({pr_url})',
             'text_sha256': runner.hashlib.sha256(
                 f'GOLD ANSWER: use x=20\nBefore\n![before]({pr_url})'.encode()).hexdigest()}]
        archive = {'instance_id': 'o__r-1', 'repo': 'o/r', 'number': 1,
            'archival_view': {'documents': documents}, 'sections': {
            'files': {'items': [{'filename': 'lib/a.js', 'status': 'modified',
                                 'additions': 1, 'deletions': 1}]},
            'assets': {'items': [
                {'url': url, 'status': 'complete', 'local_path': 'assets/image',
                 'sha256': runner.digest(asset), 'sources': ['issue:o/r#9:body']},
                {'url': pr_url, 'status': 'complete', 'local_path': 'assets/pr-image',
                 'sha256': runner.digest(pr_asset), 'sources': ['pr:body']}]}}}
        archive_path = archive_dir / '11_record_0001.json'
        archive_path.write_text(json.dumps(archive))
        tar_path = case_dir / '14_baseline_tree.tar'
        with tarfile.open(tar_path, 'w') as tar:
            data = b'export default 1;'
            info = tarfile.TarInfo('lib/a.js')
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        case = {'source_archive': str(archive_path), 'source_archive_sha256': runner.digest(archive_path),
            'anchors': {'baseline_sha': 'abc'},
            'artifacts': {'14_baseline_tree.tar': runner.digest(tar_path)}}
        (case_dir / '14_case_manifest.json').write_text(json.dumps(case))
        pilot = {'pr_numbers': [1]}
        (self.pilot / '14_pilot_manifest.json').write_text(json.dumps(pilot))

    def fake(self, **kwargs):
        packet = kwargs['packet']
        serialized = json.dumps(packet)
        self.assertNotIn('SECRET ALT', serialized)
        self.assertNotIn('GOLD ANSWER', serialized)
        self.assertNotIn('example.test/asset.png', serialized)
        self.assertEqual([], kwargs['image_paths'])
        a = annotation(packet['case_id'])
        raw = kwargs['workdir'] / '16_03_model_raw.json'
        raw.write_text(json.dumps(a))
        return a, {'backend': 'fixture', 'raw_response': str(raw),
                   'raw_response_sha256': runner.digest(raw)}

    def test_archived_runner_retries_schema_invalid_provider_output(self):
        archive_run = self.pilot / 'source/run'
        archive_path = archive_run / '11_record_0001.json'
        archive = json.loads(archive_path.read_text())
        archive['sections']['pull_request'] = {'data': {'base': {'sha': 'abc'}}}
        archive_path.write_text(json.dumps(archive))
        source = archive_run / '11_source_prs.jsonl'
        source.write_text('{}\n')
        (archive_run / '11_manifest.json').write_text(json.dumps({
            'status': 'complete',
            'source_sha256': runner.digest(source),
            'pr_ids': ['o/r#1'],
            'files': {'11_record_0001.json': runner.digest(archive_path)},
        }))
        calls = []

        def flaky_schema(**kwargs):
            calls.append(kwargs['packet'])
            if len(calls) == 1:
                value = {'case_id': kwargs['packet']['case_id']}
            else:
                value = annotation(kwargs['packet']['case_id'])
                value['localization'].update(
                    target_localizable='unknown', candidate_paths=[])
            raw = kwargs['workdir'] / '09_model_raw.json'
            raw.write_text(json.dumps(value))
            return value, {'backend': 'fixture', 'raw_response': str(raw),
                           'raw_response_sha256': runner.digest(raw)}

        output, failures = archived_runner.run_batch(
            [archive_run], self.root / 'archived-out', self.root / 'archived-tmp',
            True, flaky_schema)
        self.assertEqual(0, failures)
        result = json.loads((output / '16_03_result_0001.json').read_text())
        self.assertEqual('complete', result['status'])
        self.assertEqual(2, result['invocation']['semantic_validation_attempts'])
        self.assertEqual(1, len(result['invocation']['prior_validation_failures']))
        self.assertEqual(2, len(result['invocation']['semantic_attempt_records']))
        self.assertIn('previous_output_validation_error', calls[1])

    def run_pipeline(self, invoke=True):
        return runner.run_batch(self.pilot, self.root / 'out', self.root / 'tmp',
                                [1], invoke, self.fake if invoke else None)

    def classification(self, out, *, capability='ineligible'):
        source_manifest = json.loads((out / '16_03_run_manifest.json').read_text())
        source_result_path = out / '16_03_result_0001.json'
        source_result = json.loads(source_result_path.read_text())
        archive_path = Path(json.loads(Path(source_result['packet']).read_text())[
            'provenance']['source_archive'])
        archive = json.loads(archive_path.read_text())
        curator_path = Path(source_result['curator_assets'])
        curator = json.loads(curator_path.read_text())
        for index, asset in enumerate(curator['assets'], 1):
            asset['display_index'] = index
        curator_path.write_text(json.dumps(curator))
        source_result['curator_assets_sha256'] = runner.digest(curator_path)
        source_result_path.write_text(json.dumps(source_result))
        available = [item for item in curator['assets'] if item.get('status') == 'available']
        packet = {
            'task_id': 'o__r-1',
            'problem_statement': review.load_rows(out)[1][0]['human_seed']['problem_statement'],
            'assets': [{'asset_id': item['asset_id'], 'attachment_index': position,
                        'source_ids': item.get('source_ids', [])}
                       for position, item in enumerate(available, 1)],
        }
        packet_path = self.root / f'classification-packet-{capability}.json'
        packet_path.write_text(json.dumps(packet))
        shutil.copyfile(classifier.PROMPT,
                        self.root / '16_03_05_visual_capability.system.md')
        shutil.copyfile(classifier.SCHEMA,
                        self.root / '16_03_06_visual_capability.schema.json')
        scale = classifier.classify_change_scale(archive['sections']['files']['items'])
        value = {
            'schema_version': 'pre-human-review-classification-run-v1',
            'source_run': str(out.resolve()),
            'source_manifest_sha256': runner.digest(out / '16_03_run_manifest.json'),
            'source_run_id': source_manifest['run_id'],
            'classification_runner_sha256': runner.digest(
                Path(classifier.__file__).resolve()),
            'prompt_sha256': runner.digest(classifier.PROMPT),
            'schema_sha256': runner.digest(classifier.SCHEMA),
            'model_contracts': [],
            'human_review_ready': scale['label'] != '无法分类' and capability == 'ineligible',
            'records': [{
                'case_id': 'o__r-1',
                'source_result_sha256': runner.digest(source_result_path),
                'source_packet_sha256': source_result['packet_sha256'],
                'source_archive_sha256': runner.digest(archive_path),
                'change_scale': scale,
                'packet': str(packet_path), 'packet_sha256': runner.digest(packet_path),
                'visual_capability': {'status': capability, 'annotation': None,
                                      'invocation': None},
            }],
        }
        path = self.root / f'classification-{scale["label"]}-{capability}.json'
        path.write_text(json.dumps(value))
        return path

    def test_prepare_only_never_invokes_and_keeps_curator_assets_separate(self):
        out, failed = self.run_pipeline(False)
        self.assertEqual(0, failed)
        manifest = json.loads((out / '16_03_run_manifest.json').read_text())
        self.assertFalse(manifest['model_invoked'])
        packet = (out / '16_03_packet_0001.json').read_text()
        self.assertNotIn('SECRET ALT', packet)
        self.assertNotIn('asset.png', packet)
        self.assertIn('asset.png', (out / '16_03_curator_assets_0001.json').read_text())

    def test_missing_issue_is_retained_as_ineligible_without_pr_fallback(self):
        archive_path = self.pilot / 'source/run/11_record_0001.json'
        archive = json.loads(archive_path.read_text())
        archive['archival_view']['documents'] = [archive['archival_view']['documents'][-1]]
        archive_path.write_text(json.dumps(archive))
        case_path = self.pilot / '14_cases_verified/1/14_case_manifest.json'
        case = json.loads(case_path.read_text())
        case['source_archive_sha256'] = runner.digest(archive_path)
        case_path.write_text(json.dumps(case))
        out, failed = self.run_pipeline(False)
        self.assertEqual(0, failed)
        record = json.loads((out / '16_03_result_0001.json').read_text())
        self.assertEqual('ineligible', record['status'])
        self.assertEqual([], json.loads((out / '16_03_packet_0001.json').read_text())['problem_sources'])
        _, rows = review.load_rows(out)
        self.assertEqual('human_problem_statement_required', rows[0]['reconciliation']['queue'])
        self.assertEqual('needs_human_problem_statement', rows[0]['problem_statement_status'])
        self.assertIn('needs_human_problem_statement', policy.HUMAN_LABELS)

    def test_issue_asset_without_download_path_is_retained_as_unavailable(self):
        archive_path = self.pilot / 'source/run/11_record_0001.json'
        archive = json.loads(archive_path.read_text())
        del archive['sections']['assets']['items'][0]['local_path']
        archive['sections']['assets']['items'][0]['status'] = 'failed'
        archive_path.write_text(json.dumps(archive))
        case_path = self.pilot / '14_cases_verified/1/14_case_manifest.json'
        case = json.loads(case_path.read_text())
        case['source_archive_sha256'] = runner.digest(archive_path)
        case_path.write_text(json.dumps(case))
        packet, assets = runner.build_packet(self.pilot / '14_cases_verified/1')
        self.assertTrue(packet['problem_sources'])
        self.assertEqual('unavailable', assets[0]['status'])
        self.assertIsNone(assets[0]['local_path'])

    def test_review_rejects_symlinked_archive_media(self):
        out, failed = self.run_pipeline(True)
        self.assertEqual(0, failed)
        outside = self.root / 'outside.png'
        outside.write_bytes(self.asset.read_bytes())
        self.asset.unlink()
        self.asset.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            review.render(out, out / '16_04_human_review.html')

    def test_review_rejects_absolute_or_traversing_archive_media_paths(self):
        out, failed = self.run_pipeline(True)
        self.assertEqual(0, failed)
        archive_path = self.pilot / 'source/run/11_record_0001.json'
        original = json.loads(archive_path.read_text())
        for unsafe in ('absolute', 'traversal'):
            with self.subTest(unsafe=unsafe):
                archive = json.loads(json.dumps(original))
                archive['sections']['assets']['items'][0]['local_path'] = (
                    str(self.asset.resolve()) if unsafe == 'absolute' else '../outside.png')
                archive_path.write_text(json.dumps(archive))
                review_dir = self.root / f'unsafe-{unsafe}'
                review_dir.mkdir()
                with self.assertRaisesRegex(ValueError, 'unsafe archived media path'):
                    review.render(out, review_dir / '16_04_human_review.html')

    def test_review_rejects_archive_media_hash_drift(self):
        out, failed = self.run_pipeline(True)
        self.assertEqual(0, failed)
        self.asset.write_bytes(b'\x89PNG\r\n\x1a\nchanged-pixels')
        with self.assertRaisesRegex(ValueError, 'hash changed'):
            review.render(out, out / '16_04_human_review.html')

    def test_review_rejects_symlinked_publication_target(self):
        out, failed = self.run_pipeline(True)
        self.assertEqual(0, failed)
        outside = self.root / 'outside-review-assets'
        outside.mkdir()
        (out / '16_04_review_assets').symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            review.render(out, out / '16_04_human_review.html')

    def test_review_ignores_precreated_legacy_staging_symlink(self):
        out, failed = self.run_pipeline(True)
        self.assertEqual(0, failed)
        victim = self.root / 'victim.txt'
        victim.write_text('unchanged\n')
        legacy = out / '.16_04_human_review.html.staging'
        legacy.symlink_to(victim)

        review.render(out, out / '16_04_human_review.html')

        self.assertEqual('unchanged\n', victim.read_text())
        self.assertTrue(legacy.is_symlink())
        self.assertTrue((out / '16_04_human_review.html').is_file())

    def test_review_copy_failure_never_publishes_partial_assets(self):
        out, failed = self.run_pipeline(True)
        self.assertEqual(0, failed)

        def corrupt_copy(_source, target):
            Path(target).write_bytes(b'corrupt')

        with patch.object(review.shutil, 'copy2', side_effect=corrupt_copy):
            with self.assertRaisesRegex(ValueError, 'changed while copying'):
                review.render(out, out / '16_04_human_review.html')
        self.assertFalse((out / '16_04_review_assets').exists())
        self.assertEqual([], list(out.glob('.16_04_review_assets-*')))

    def test_review_bundle_recovers_interrupted_commit_by_hash(self):
        out, failed = self.run_pipeline(True)
        self.assertEqual(0, failed)
        classification = self.classification(out)
        output = out / '16_04_human_review.html'
        original_atomic_json = review._atomic_json

        def interrupt_commit(path, value):
            if value.get('schema_version') == 'visual-review-bundle-commit-v1':
                raise KeyboardInterrupt('simulated review publication interruption')
            return original_atomic_json(path, value)

        with patch.object(review, '_atomic_json', side_effect=interrupt_commit):
            with self.assertRaisesRegex(KeyboardInterrupt, 'simulated review publication'):
                review.render(out, output, classification)

        transaction = out / '.16_04_review_bundle.transaction.json'
        commit = out / '16_04_review_bundle.commit.json'
        self.assertTrue(output.is_file())
        self.assertTrue((out / '16_04_review_assets').is_dir())
        self.assertTrue((out / '16_04_human_review_seed.json').is_file())
        self.assertTrue((out / '16_04_review_manifest.json').is_file())
        self.assertTrue(transaction.is_file())
        self.assertFalse(commit.exists())

        result = review.render(out, output, classification)
        self.assertEqual(out / '16_04_review_manifest.json', result)
        self.assertTrue(output.is_file())
        self.assertTrue(commit.is_file())
        self.assertFalse(transaction.exists())

    def test_review_audit_rejects_post_commit_asset_tampering(self):
        out, failed = self.run_pipeline(True)
        self.assertEqual(0, failed)
        classification = self.classification(out)
        review.render(out, out / '16_04_human_review.html', classification)
        asset = next((out / '16_04_review_assets').iterdir())
        asset.write_bytes(asset.read_bytes() + b'tamper')
        with self.assertRaisesRegex(ValueError, 'asset changed after commit'):
            auditor.audit(out)

    def test_full_fixture_review_audit_and_human_import(self):
        out, failed = self.run_pipeline(True)
        self.assertEqual(0, failed)
        classification = self.classification(out)
        page = out / '16_04_human_review.html'
        review.render(out, page, classification)
        page_text = page.read_text()
        self.assertIn('必须先记录并持久化无图判断', page_text)
        self.assertIn('id="reveal"', page_text)
        self.assertIn('images_revealed_at', page_text)
        self.assertIn("const postReveal=revealed?", page_text)
        self.assertIn("revealed?'disabled'", page_text)
        self.assertIn('data-k="reviewer"', page_text)
        self.assertIn('审核人', page_text)
        self.assertNotIn('max-height:420px', page_text)
        self.assertIn('data-k="${statementKey}"', page_text)
        self.assertIn('class="verifier-full"', page_text)
        self.assertNotIn('class="verifier-full" open', page_text)
        self.assertIn('id="prev"', page_text)
        self.assertIn('id="next"', page_text)
        self.assertIn('id="counter"', page_text)
        self.assertIn('id="language"', page_text)
        self.assertIn('problem_statement_zh', page_text)
        self.assertIn('PR 修复证据（仅人工可见）', page_text)
        self.assertIn('PR 证据图片', page_text)
        _, rows = review.load_rows(out)
        self.assertEqual(1, len(rows[0]['assets']))
        self.assertEqual(1, len(rows[0]['pr_assets']))
        self.assertIn('PR 证据图片 1', rows[0]['pr_body'])
        self.assertIn('视觉材料 ${esc(a.display_index)}', page_text)
        self.assertIn('function renderCurrent()', page_text)
        self.assertNotIn("DATA.rows.map(showCase).join('')", page_text)
        embedded = json.loads(base64.b64decode(re.search(r"atob\('([^']+)'\)", page_text).group(1)))
        self.assertEqual(str(classification.resolve()), embedded['classification_path'])
        self.assertEqual(runner.digest(classification), embedded['classification_sha256'])
        self.assertTrue(embedded['classification_ready'])
        self.assertEqual('https://github.com/o/r/pull/1', embedded['rows'][0]['pr_url'])
        self.assertIn('Marker position is wrong', embedded['rows'][0]['human_seed']['problem_statement'])
        self.assertIn('视觉材料 1', embedded['rows'][0]['human_seed']['problem_statement'])
        self.assertNotIn('example.test/asset.png', embedded['rows'][0]['human_seed']['problem_statement'])
        review_manifest = json.loads((out / '16_04_review_manifest.json').read_text())
        self.assertEqual('ready_for_human_review', review_manifest['status'])
        self.assertEqual(str(classification.resolve()),
                         review_manifest['pre_review_classification'])
        self.assertEqual(runner.digest(classification),
                         review_manifest['pre_review_classification_sha256'])
        self.assertTrue(review_manifest['pre_review_classification_ready'])
        self.assertEqual({'complete': 1}, review_manifest['counts']['result_status'])
        self.assertEqual(1, review_manifest['task_review_asset_count'])
        self.assertEqual(1, review_manifest['pr_curator_asset_count'])
        self.assertEqual(2, review_manifest['review_asset_count'])
        self.assertTrue((out / '16_04_review_assets' / f'{runner.digest(self.asset)}.png').is_file())
        self.assertEqual(1, sum(review_manifest['counts']['queue'].values()))
        self.assertEqual('o__r-1', review_manifest['case_index'][0]['case_id'])
        self.assertEqual(64, len(review_manifest['case_index'][0]['result_sha256']))
        audit = auditor.audit(out)
        self.assertEqual('passed', audit['status'])
        seed = json.loads((out / '16_04_human_review_seed.json').read_text())
        self.assertEqual(str(classification.resolve()), seed['pre_review_classification'])
        self.assertEqual(runner.digest(classification),
                         seed['pre_review_classification_sha256'])
        self.assertTrue(seed['pre_review_classification_ready'])
        row = seed['rows'][0]
        self.assertIn('Marker position is wrong', row['problem_statement'])
        row['problem_statement'] = 'Edited benchmark problem statement'
        row.update(reviewer='stage16-reviewer', reviewed_at='2026-09-01T00:00:00Z',
                   text_first_notes='文字缺少精确几何关系',
                   text_first_recorded_at='2026-08-31T23:59:00Z',
                   images_revealed_at='2026-08-31T23:59:01Z',
                   visual_delta='图片显示 marker 相对边框的精确位置',
                   patch_and_test_alignment='需要人工查看测试后确认',
                   decision='human_confirmed_visual_candidate', decision_reason='视觉补足文字缺口')
        export = self.root / 'human.json'
        export.write_text(json.dumps(seed))
        commit_path = out / '16_05_human_import.commit.json'
        original_write_json = auditor.write_json

        def interrupt_commit(path, value):
            if Path(path) == commit_path:
                raise KeyboardInterrupt('injected before human import commit')
            return original_write_json(path, value)

        with patch.object(auditor, 'write_json', side_effect=interrupt_commit):
            with self.assertRaises(KeyboardInterrupt):
                auditor.audit(out, export)
        self.assertTrue((out / '.16_05_human_import.transaction.json').is_file())
        imported = auditor.audit(out, export)
        self.assertEqual(1, imported['selected'])
        self.assertFalse(imported['target_met'])
        self.assertEqual(str(classification.resolve()),
                         imported['pre_review_classification'])
        self.assertEqual(runner.digest(classification),
                         imported['pre_review_classification_sha256'])
        self.assertTrue(imported['pre_review_classification_ready'])
        selection = json.loads(
            (out / '16_05_human_confirmed_visual_candidates.json').read_text())
        self.assertEqual(runner.digest(classification),
                         selection['pre_review_classification_sha256'])
        self.assertTrue(selection['pre_review_classification_ready'])
        self.assertTrue((out / '16_05_human_decisions.original.json').is_file())
        self.assertTrue(commit_path.is_file())
        self.assertFalse((out / '.16_05_human_import.transaction.json').exists())

    def test_review_is_materials_only_when_scale_or_visual_classification_is_unresolved(self):
        out, failed = self.run_pipeline(True)
        self.assertEqual(0, failed)
        classification = self.classification(out, capability='requires_video_review')
        review_dir = self.root / 'review-video'
        review_dir.mkdir()
        review.render(out, review_dir / '16_04_human_review.html', classification)
        manifest = json.loads((review_dir / '16_04_review_manifest.json').read_text())
        self.assertEqual(
            'materials_only_pre_review_classification_incomplete', manifest['status'])
        self.assertFalse(manifest['pre_review_classification_complete'])

    def test_review_is_materials_only_when_change_scale_is_unresolved(self):
        archive_path = self.pilot / 'source/run/11_record_0001.json'
        archive = json.loads(archive_path.read_text())
        archive['sections']['files']['items'] = [
            {'filename': 'tests/a.test.js', 'status': 'modified',
             'additions': 1, 'deletions': 0}]
        archive_path.write_text(json.dumps(archive))
        case_path = self.pilot / '14_cases_verified/1/14_case_manifest.json'
        case = json.loads(case_path.read_text())
        case['source_archive_sha256'] = runner.digest(archive_path)
        case_path.write_text(json.dumps(case))
        out, failed = self.run_pipeline(True)
        self.assertEqual(0, failed)
        classification = self.classification(out)
        review.render(out, out / '16_04_human_review.html', classification)
        manifest = json.loads((out / '16_04_review_manifest.json').read_text())
        self.assertEqual(
            'materials_only_pre_review_classification_incomplete', manifest['status'])
        self.assertFalse(manifest['pre_review_classification_complete'])

    def test_default_classification_is_recorded_with_the_same_path_and_hash(self):
        out, failed = self.run_pipeline(True)
        self.assertEqual(0, failed)
        classification = self.classification(out)
        default = out / '16_03_08_pre_review_classifications.json'
        default.write_bytes(classification.read_bytes())
        shutil.copyfile(classifier.PROMPT,
                        out / '16_03_05_visual_capability.system.md')
        shutil.copyfile(classifier.SCHEMA,
                        out / '16_03_06_visual_capability.schema.json')
        review.render(out, out / '16_04_human_review.html')
        manifest = json.loads((out / '16_04_review_manifest.json').read_text())
        self.assertEqual('ready_for_human_review', manifest['status'])
        self.assertEqual(str(default.resolve()), manifest['pre_review_classification'])
        self.assertEqual(runner.digest(default),
                         manifest['pre_review_classification_sha256'])

    def test_review_audit_rejects_changed_classification(self):
        out, failed = self.run_pipeline(True)
        self.assertEqual(0, failed)
        classification = self.classification(out)
        review.render(out, out / '16_04_human_review.html', classification)
        classification.write_text(classification.read_text() + '\n')
        with self.assertRaisesRegex(ValueError, 'classification changed'):
            auditor.audit(out)

    def test_human_import_rejects_materials_only_review(self):
        out, failed = self.run_pipeline(True)
        self.assertEqual(0, failed)
        review.render(out, out / '16_04_human_review.html')
        manifest = json.loads((out / '16_04_review_manifest.json').read_text())
        self.assertEqual(
            'materials_only_pre_review_classification_incomplete', manifest['status'])
        self.assertIsNone(manifest['pre_review_classification'])
        self.assertFalse(manifest['pre_review_classification_ready'])
        human = self.root / 'human-materials-only.json'
        human.write_bytes((out / '16_04_human_review_seed.json').read_bytes())
        with self.assertRaisesRegex(ValueError, 'not ready for human review'):
            auditor.audit(out, human)

    def test_human_import_rejects_changed_classification_binding(self):
        out, failed = self.run_pipeline(True)
        self.assertEqual(0, failed)
        classification = self.classification(out)
        review.render(out, out / '16_04_human_review.html', classification)
        value = json.loads((out / '16_04_human_review_seed.json').read_text())
        value['pre_review_classification_sha256'] = '0' * 64
        human = self.root / 'human-wrong-classification.json'
        human.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, 'classification binding mismatch'):
            auditor.audit(out, human)

    def test_human_import_rejects_missing_text_first_reveal_evidence(self):
        out, failed = self.run_pipeline(True)
        self.assertEqual(0, failed)
        classification = self.classification(out)
        review.render(out, out / '16_04_human_review.html', classification)
        value = json.loads((out / '16_04_human_review_seed.json').read_text())
        row = value['rows'][0]
        row.update(reviewer='reviewer', reviewed_at='2026-09-01T00:00:00Z',
                   text_first_notes='文字不足', decision='human_confirmed_text_sufficient',
                   decision_reason='文字已经足够')
        human = self.root / 'human-missing-reveal.json'
        human.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, 'text-first timestamps'):
            auditor.audit(out, human)

    def test_translation_validation_preserves_visual_markers(self):
        source = {'case_id': 'o__r-1', 'pr_title': 'Fix marker',
                  'problem_statement': 'Before\n\n> **视觉材料 1**：见下方对应图片。'}
        valid = {'case_id': 'o__r-1', 'pr_title_zh': '修复标记',
                 'problem_statement_zh': '修复前\n\n> **视觉材料 1**：见下方对应图片。'}
        translator.validate(source, valid)
        translator.validate({**source, 'problem_statement': source['problem_statement'] + '\nhttps://example.test/a'},
                            {**valid, 'problem_statement_zh': valid['problem_statement_zh'] + '\nhttps://example.test/a。'})
        translator.validate({**source, 'problem_statement': source['problem_statement'] + '\nhttps://example.test/a'},
                            {**valid, 'problem_statement_zh': valid['problem_statement_zh'] + '\nhttps://example.test/a，该页面'})
        invalid = {**valid, 'problem_statement_zh': '没有图片引用'}
        with self.assertRaises(ValueError):
            translator.validate(source, invalid)

    def test_translation_resume_rejects_unbound_batch_artifacts(self):
        out, failed = self.run_pipeline(True)
        self.assertEqual(0, failed)
        _, rows = review.load_rows(out)
        source = {'case_id': rows[0]['case_id'], 'pr_title': rows[0]['pr_title'],
                  'problem_statement': rows[0]['human_seed']['problem_statement']}
        directory = out / '16_04_translation_calls/16_04_call_01'
        directory.mkdir(parents=True)
        translated = {'case_id': source['case_id'], 'pr_title_zh': '修复标记',
                      'problem_statement_zh': source['problem_statement']}
        (directory / '09_model_raw.json').write_text(json.dumps(
            {'translations': [translated]}))
        (directory / '10_api_invocation.json').write_text(json.dumps({'backend': 'gemini'}))
        with self.assertRaisesRegex(ValueError, 'resume contract'):
            translator.run(out, self.root / 'unused-key', resume=True)

    def test_translation_resume_reuses_only_identical_frozen_contract(self):
        out, failed = self.run_pipeline(True)
        self.assertEqual(0, failed)
        calls = []

        class FakeEvaluator:
            def __init__(self, backend, model=None, **kwargs):
                self.backend = backend
                self.profile = {'protocol': 'chat', 'endpoint': 'fixture',
                                'model': model or 'fixture-default'}
                self.attempts = kwargs['attempts']
                self.min_interval = kwargs['min_interval']
                self.max_tokens = kwargs['max_tokens']

            def __call__(self, *, packet, workdir, **kwargs):
                calls.append(packet)
                result = {'translations': [{
                    'case_id': item['case_id'], 'pr_title_zh': item['pr_title'],
                    'problem_statement_zh': item['problem_statement'],
                } for item in packet['items']]}
                (workdir / '09_model_raw.json').write_text(json.dumps(result))
                invocation = {'backend': self.backend, 'profile': self.profile}
                (workdir / '10_api_invocation.json').write_text(json.dumps(invocation))
                return result, invocation

        with patch.object(translator, 'ApiEvaluator', FakeEvaluator):
            output = translator.run(out, self.root / 'unused-key', model='fixture')
            self.assertEqual(1, len(calls))
            output.unlink()
            resumed = translator.run(
                out, self.root / 'unused-key', model='fixture', resume=True)
            self.assertEqual(1, len(calls))
            resumed_invocation = json.loads(resumed.read_text())['invocations'][0]
            self.assertTrue(resumed_invocation['reused'])
            self.assertEqual('fixture', resumed_invocation['profile']['model'])
            output.unlink()
            batch_contract = out / '16_04_translation_calls/16_04_call_01/00_resume_contract.json'
            frozen_batch = json.loads(batch_contract.read_text())
            batch_contract.write_text(json.dumps({**frozen_batch, 'packet_sha256': '0' * 64}))
            with self.assertRaisesRegex(ValueError, 'batch resume contract'):
                translator.run(out, self.root / 'unused-key', model='fixture', resume=True)
            batch_contract.write_text(json.dumps(frozen_batch, ensure_ascii=False, indent=2) + '\n')
            with self.assertRaisesRegex(ValueError, 'resume contract changed'):
                translator.run(out, self.root / 'unused-key', model='other', resume=True)

    def test_translation_resume_retries_incomplete_batch_without_reusing_it(self):
        out, failed = self.run_pipeline(True)
        self.assertEqual(0, failed)

        class EvaluatorBase:
            def __init__(self, backend, model=None, **kwargs):
                self.backend = backend
                self.profile = {'protocol': 'chat', 'endpoint': 'fixture',
                                'model': model or 'fixture'}
                self.attempts = kwargs['attempts']
                self.min_interval = kwargs['min_interval']
                self.max_tokens = kwargs['max_tokens']

        class FailedEvaluator(EvaluatorBase):
            def __call__(self, *, workdir, **kwargs):
                (workdir / '10_api_invocation.json').write_text(json.dumps(
                    {'backend': self.backend, 'profile': self.profile}))
                raise ValueError('fixture failure')

        class SuccessfulEvaluator(EvaluatorBase):
            def __call__(self, *, packet, workdir, **kwargs):
                result = {'translations': [{
                    'case_id': item['case_id'], 'pr_title_zh': item['pr_title'],
                    'problem_statement_zh': item['problem_statement'],
                } for item in packet['items']]}
                (workdir / '09_model_raw.json').write_text(json.dumps(result))
                invocation = {'backend': self.backend, 'profile': self.profile}
                (workdir / '10_api_invocation.json').write_text(json.dumps(invocation))
                return result, invocation

        with patch.object(translator, 'ApiEvaluator', FailedEvaluator):
            with self.assertRaisesRegex(ValueError, 'fixture failure'):
                translator.run(out, self.root / 'unused-key', model='fixture')
        failed_dir = out / '16_04_translation_calls/16_04_call_01'
        self.assertFalse((failed_dir / '12_resume_receipt.json').exists())
        with patch.object(translator, 'ApiEvaluator', SuccessfulEvaluator):
            translated = translator.run(
                out, self.root / 'unused-key', model='fixture', resume=True)
        retry = out / '16_04_translation_calls/16_04_call_01_retry_01'
        self.assertTrue((retry / '12_resume_receipt.json').is_file())
        self.assertEqual('o__r-1', json.loads(translated.read_text())['items'][0]['case_id'])


if __name__ == '__main__':
    unittest.main()
