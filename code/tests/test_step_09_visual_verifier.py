import copy
import json
import os
from types import SimpleNamespace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from analysis.scripts import step_08_03_pilot_visual_context_vlm as pilot
from analysis.scripts import step_09_03_run_visual_verifiers as runner
from pr_crawler import visual_verifier as verifier


def annotation(pr_id='o/r#1', asset_id='a'):
    return {'schema_version': 'visual-verifier-v1', 'pr_id': pr_id,
        'images': [{'asset_id': asset_id, 'observed': True, 'content_kind': 'web_interface',
            'content_reason': 'Control geometry', 'observation': 'Dot displaced',
            'relevance': 'relevant', 'temporal_role': 'before', 'source_quote': 'BEFORE',
            'faithful_text_representation': 'no', 'ocr_task_sufficient': 'no',
            'text_reason': 'Character transcription loses position'}],
        'task': {'necessity': 'necessary', 'image_transcription_sufficient': 'no',
            'missing_visual_information': 'Which dot is displaced relative to its border',
            'evidence_asset_ids': [asset_id], 'problem_evidence_quotes': ['BEFORE'],
            'reason': 'Geometry supplies the missing constraint'},
        'quality': {'problem_clarity': 'clear', 'evidence_sufficiency': 'sufficient',
            'problem_evidence_separable': 'yes', 'leakage_risk': 'unknown',
            'missing_sources': verifier.MISSING_SOURCES[:], 'reason': 'PR-only provisional triage'}}


class VerifierPolicyTests(unittest.TestCase):
    def setUp(self):
        self.a = annotation()
        self.packet = {'pr_id': 'o/r#1', 'title': 'Case', 'body': 'BEFORE\nAFTER',
                       'missing_sources': verifier.MISSING_SOURCES[:],
                       'images': [{'asset_id': 'a', 'status': 'attached'}]}

    def bucket(self):
        verifier.validate(self.a, self.packet)
        result = verifier.decide(self.a)
        self.assertFalse(result['training_ready'])
        return result['bucket']

    def test_necessary(self):
        self.assertEqual('visual_necessary', self.bucket())

    def test_helpful(self):
        self.a['task']['necessity'] = 'helpful'
        self.assertEqual('visual_helpful', self.bucket())

    def test_ocr_overrides_model_necessary(self):
        self.a['task']['image_transcription_sufficient'] = 'yes'
        self.a['images'][0].update(ocr_task_sufficient='yes', faithful_text_representation='yes')
        self.assertEqual('ocr_auxiliary', self.bucket())

    def test_excluded(self):
        for label in ('redundant', 'unrelated'):
            self.a['task']['necessity'] = label
            self.assertEqual('excluded', self.bucket())

    def test_insufficient_quality(self):
        for key, value in [('problem_clarity', 'unclear'), ('evidence_sufficiency', 'insufficient'),
                           ('problem_evidence_separable', 'unknown')]:
            self.a = annotation()
            self.a['quality'][key] = value
            self.assertEqual('review', self.bucket())

    def test_missing_specific_evidence(self):
        for key, value in [('evidence_asset_ids', []), ('missing_visual_information', ' ')]:
            self.a = annotation()
            self.a['task'][key] = value
            self.assertEqual('review', self.bucket())

    def test_after_only_and_unknown_roles(self):
        for role in ('after', 'mixed', 'unknown'):
            self.a['images'][0]['temporal_role'] = role
            self.a['quality']['leakage_risk'] = 'present'
            self.assertEqual('review', self.bucket())

    def test_before_plus_after(self):
        after = copy.deepcopy(self.a['images'][0])
        after.update(asset_id='b', temporal_role='after', source_quote='AFTER')
        self.a['images'].append(after)
        self.packet['images'].append({'asset_id': 'b', 'status': 'attached'})
        self.a['quality']['leakage_risk'] = 'present'
        self.assertEqual('visual_necessary', self.bucket())

    def test_text_disagreement(self):
        self.a['task']['image_transcription_sufficient'] = 'yes'
        self.assertEqual('review', self.bucket())

    def test_missing_image(self):
        self.packet['images'][0]['status'] = 'unavailable'
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            self.bucket()
        self.a['images'][0].update(observed=False, content_kind=None, relevance='unknown',
            temporal_role='unknown', source_quote=None, faithful_text_representation='unknown',
            ocr_task_sufficient='unknown')
        self.assertEqual('review', self.bucket())

    def test_fabricated_quotes_and_ids(self):
        for mutation in ('quote', 'id', 'missing_source', 'role_quote', 'leakage'):
            self.a = annotation()
            if mutation == 'quote': self.a['task']['problem_evidence_quotes'] = ['invented']
            if mutation == 'id': self.a['task']['evidence_asset_ids'] = ['invented']
            if mutation == 'missing_source': self.a['quality']['missing_sources'] = []
            if mutation == 'role_quote': self.a['images'][0]['source_quote'] = None
            if mutation == 'leakage': self.a['images'][0]['temporal_role'] = 'after'
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.bucket()

    def test_exact_eight_categories(self):
        categories = json.loads(verifier.SCHEMA.read_text())['properties']['images']['items']['properties']['content_kind']['enum']
        self.assertEqual(9, len(categories))
        for category in categories:
            self.a['images'][0]['content_kind'] = category
            self.assertEqual('review' if category is None else 'visual_necessary', self.bucket())

    def test_bound_schema_restricts_long_id_copy_errors(self):
        import jsonschema
        schema = verifier.bind_schema(self.packet)
        jsonschema.validate(self.a, schema)
        self.a['task']['evidence_asset_ids'] = ['typo']
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(self.a, schema)
        self.packet['images'] = []
        schema = verifier.bind_schema(self.packet)
        self.assertEqual(0, schema['properties']['images']['maxItems'])
        self.assertEqual(0, schema['properties']['task']['properties']['evidence_asset_ids']['maxItems'])

    def test_bound_quotes_disallow_paraphrase(self):
        import jsonschema
        self.packet['source_quote_candidates'] = verifier.quote_candidates(self.packet)
        schema = verifier.bind_schema(self.packet)
        jsonschema.validate(self.a, schema)
        self.a['task']['problem_evidence_quotes'] = ['Before fixing this']
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(self.a, schema)
        self.packet['source_quote_candidates'].append('invented')
        with self.assertRaisesRegex(ValueError, 'Quote candidates'):
            verifier.bind_schema(self.packet)

    def test_quote_enum_compatibility_preserves_original_text(self):
        self.packet['body'] = 'BEFORE\nThe "quoted" setting fails.\r\n<img src="example">'
        original = self.packet['body']
        candidates = verifier.quote_candidates(self.packet)
        self.assertEqual(original, self.packet['body'])
        self.assertTrue(all('"' not in q for q in candidates))
        self.assertTrue(all(q in original or q in self.packet['title'] for q in candidates))


class VerifierPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        cache = self.root / 'cache'
        cache.mkdir()
        image = cache / 'original.png'
        Image.new('RGB', (40, 30)).save(image)
        (cache / 'a.json').write_text(json.dumps({'status': 'complete', 'local_path': image.name,
            'sha256': pilot.digest(image), 'bytes': image.stat().st_size}))
        self.rows = [{'repo': 'o/r', 'number': i, 'title': 'Case', 'body': 'BEFORE\r\n```diff\n- unknown\n+ any\n```',
            'html_url': 'https://example.com/pr', 'image_screening': {'assets': [
                {'asset_id': 'a', 'url': 'https://example.com/a.png', 'media_kind': 'image', 'decoration_reason': None}]}}
            for i in range(1, 6)]
        self.lines = [(json.dumps(r, ensure_ascii=False) + '\n').encode() for r in self.rows]
        self.source = self.root / 'source.jsonl'
        self.source.write_bytes(b''.join(self.lines))
        self.cache_patch = patch.object(pilot, 'CACHE', cache)
        self.cache_patch.start()
        self.addCleanup(self.cache_patch.stop)
        self.download = patch.object(pilot, 'bounded_download', side_effect=AssertionError('No network in tests'))
        self.download.start()
        self.addCleanup(self.download.stop)

    def fake(self, **kwargs):
        a = annotation(kwargs['packet']['pr_id'])
        number = int(a['pr_id'].split('#')[1])
        if number == 2: a['task']['necessity'] = 'helpful'
        if number == 3:
            a['task']['image_transcription_sufficient'] = 'yes'
            a['images'][0].update(ocr_task_sufficient='yes', faithful_text_representation='yes')
        if number == 4: a['task']['necessity'] = 'redundant'
        if number == 5: a['quality']['problem_clarity'] = 'unknown'
        raw = kwargs['workdir'] / '09_model_raw.json'
        raw.write_text(json.dumps(a))
        return a, {'backend': 'fixture', 'raw_response': str(raw),
                   'raw_response_sha256': pilot.digest(raw), 'cli_reported_tokens': None}

    def run_fixture(self, **overrides):
        args = dict(source=self.source, wanted=['o/r#' + str(i) for i in range(1, 6)],
            output_root=self.root / 'out', tmp_root=self.root / 'tmp', run_model=True,
            evaluator=self.fake)
        args.update(overrides)
        return runner.run_batch(**args)

    def test_five_exports_raw_preservation_and_reexport(self):
        out, failures = self.run_fixture()
        self.assertEqual(0, failures)
        summary = runner.export_results(out)
        self.assertEqual(dict.fromkeys(verifier.BUCKETS, 1), summary['buckets'])
        for bucket, line in zip(verifier.BUCKETS, self.lines):
            self.assertEqual(line, (out / f'09_{bucket}_prs.jsonl').read_bytes())
        self.assertEqual(5, len((out / '09_decision_ledger.jsonl').read_text().splitlines()))
        self.assertTrue((out / '09_verifier_report.md').is_file())

    def test_preparation_does_not_invoke(self):
        with patch.object(self, 'fake', side_effect=AssertionError('Must not invoke')) as model:
            out, failures = self.run_fixture(run_model=False, evaluator=model)
        model.assert_not_called()
        self.assertEqual(0, failures)
        self.assertFalse((out / '09_summary.json').exists())
        with self.assertRaisesRegex(ValueError, 'finished invoked'):
            runner.export_results(out)

    def test_migrated_api_protocols_end_to_end_and_tamper_detection(self):
        from pr_crawler.api_engines import ApiEvaluator
        for backend in ('k3', 'gemini'):
            answer = annotation('o/r#1')
            response = {'status': 'completed', 'model': 'fixture', 'output': [
                {'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps(answer)}]}]} if backend == 'k3' else {
                'model': 'fixture', 'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(answer)}}]}
            def factory(**kwargs):
                def create(**payload): return SimpleNamespace(model_dump=lambda: response)
                return SimpleNamespace(responses=SimpleNamespace(create=create),
                    chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=lambda: None)
            evaluator = ApiEvaluator(backend, client_factory=factory, sleep=lambda _: None)
            with self.subTest(backend=backend), patch.dict(os.environ, {'ARK_API_KEY': 'fixture-secret', 'AIDP_API_KEY': 'fixture-secret'}):
                out, failed = self.run_fixture(wanted=['o/r#1'], workers=1, evaluator=evaluator)
                self.assertEqual(0, failed)
                self.assertEqual(1, runner.export_results(out)['buckets']['visual_necessary'])
                record = json.loads((out / '09_result_0001.json').read_text())
                path = Path(record['invocation']['provider_response'])
                path.write_text(path.read_text() + ' ')
                with self.assertRaisesRegex(ValueError, 'Provider request/response hash mismatch'):
                    runner.export_results(out)

    def test_model_failure_is_review(self):
        def fail(**kwargs): raise RuntimeError('Backend unavailable')
        out, failures = self.run_fixture(evaluator=fail)
        self.assertEqual(5, failures)
        summary = runner.export_results(out)
        self.assertEqual(5, summary['buckets']['review'])
        self.assertEqual(b''.join(self.lines), (out / '09_review_prs.jsonl').read_bytes())

    def test_tampered_source_rejected(self):
        out, _ = self.run_fixture()
        with (out / '09_source_prs.jsonl').open('ab') as stream: stream.write(b'\n')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            runner.export_results(out)

    def test_tampered_decision_rejected(self):
        out, _ = self.run_fixture()
        path = out / '09_result_0001.json'
        record = json.loads(path.read_text())
        record['decision']['bucket'] = 'excluded'
        path.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, 'Stored decision'):
            runner.export_results(out)

    def test_invalid_selection(self):
        for wanted in ([], ['o/r#1', 'o/r#1'], ['missing#1']):
            with self.assertRaises(ValueError): self.run_fixture(wanted=wanted)

    def test_unterminated_source_preserved_or_rejected_before_call(self):
        self.source.write_bytes(b''.join(self.lines).rstrip(b'\n'))
        selected = runner.select_rows(self.source, ['o/r#1', 'o/r#5'])
        self.assertFalse(selected[-1][1].endswith(b'\n'))
        with self.assertRaisesRegex(ValueError, 'unterminated'):
            runner.select_rows(self.source, ['o/r#5', 'o/r#1'])


if __name__ == '__main__':
    unittest.main()
