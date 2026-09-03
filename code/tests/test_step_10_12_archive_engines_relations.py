import base64
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from pr_crawler.api_engines import PROFILES, ApiEvaluator, data_url, extract_annotation, load_key, request_body, retry_after
from pr_crawler.api import API
from pr_crawler.store import Store
from pr_crawler.source_archive import enrich_with_history, partition, merge_anchors
from pr_crawler.repair_relations import build
from test_pr_crawler import FakeGitHub, pr


class ApiEngineTests(unittest.TestCase):
    def test_retry_after_seconds_and_date(self):
        error = SimpleNamespace(response=SimpleNamespace(headers={'retry-after': '11'}))
        self.assertEqual(11, retry_after(error))
        error.response.headers['retry-after'] = 'Thu, 01 Jan 1970 00:01:00 GMT'
        with patch('pr_crawler.api_engines.time.time', return_value=50):
            self.assertEqual(10, retry_after(error))

    def test_retry_preserves_server_delay_and_sanitizes_failure(self):
        class RateLimit(Exception):
            status_code = 429
            response = SimpleNamespace(headers={'retry-after': '9'})
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {'ARK_API_KEY': 'fixture-secret'}):
            root = Path(d)
            prompt, schema = root / 'prompt', root / 'schema'
            prompt.write_text('Classify'); schema.write_text('{}')
            delays, calls = [], []
            def call(**payload):
                calls.append(payload)
                if len(calls) == 1:
                    raise RateLimit('credential fixture-secret must not be logged')
                return SimpleNamespace(model_dump=lambda: {'status': 'completed', 'output': [
                    {'type': 'message', 'content': [{'type': 'output_text', 'text': '{}'}]}]})
            factory = lambda **kw: SimpleNamespace(responses=SimpleNamespace(create=call), close=lambda: None)
            clock = [100.0]
            def sleep(seconds):
                delays.append(seconds)
                clock[0] += seconds
            evaluator = ApiEvaluator('k3', attempts=2, client_factory=factory, sleep=sleep)
            with patch('pr_crawler.api_engines.time.monotonic', side_effect=lambda: clock[0]):
                _, metadata = evaluator(packet={}, image_paths=[], system_prompt=prompt, schema=schema, workdir=root, timeout=10)
            self.assertEqual(2, metadata['attempts'])
            self.assertIn(9, delays)
            for path in root.iterdir():
                self.assertNotIn('fixture-secret', path.read_text())

    def test_literal_key_file_never_executes(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "pass --key-file explicitly"):
                load_key(PROFILES['k3'])
            path = Path(d) / 'key.sh'
            path.write_text('export ARK_API_KEY="fixture-secret"\n')
            self.assertEqual('fixture-secret', load_key(PROFILES['k3'], path))
            path.write_text('export ARK_API_KEY="$(echo unsafe)"\n')
            with self.assertRaises(ValueError): load_key(PROFILES['k3'], path)

    def test_original_bytes_and_both_protocols(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'original.png'
            Image.new('RGB', (20, 10)).save(path)
            url = data_url(path)
            self.assertTrue(url.startswith('data:image/png;base64,'))
            self.assertEqual(path.read_bytes(), base64.b64decode(url.split(',')[1]))
            ark = request_body(PROFILES['k3'], {'text': 'whole body'}, [path], 'system', 123)
            aidp = request_body(PROFILES['gemini'], {'text': 'whole body'}, [path], 'system', 123)
            self.assertEqual('input_image', ark['input'][0]['content'][1]['type'])
            self.assertEqual('image_url', aidp['messages'][1]['content'][1]['type'])
            self.assertEqual('system', aidp['messages'][0]['content'])

    def test_zero_max_tokens_omits_client_side_output_limit(self):
        ark = request_body(PROFILES['k3'], {'text': 'whole body'}, [], 'system', 0)
        aidp = request_body(PROFILES['gemini'], {'text': 'whole body'}, [], 'system', 0)
        self.assertNotIn('max_output_tokens', ark)
        self.assertNotIn('max_tokens', aidp)
        self.assertEqual(0, ApiEvaluator('k3', max_tokens=0).max_tokens)

    def test_parse_completed_only(self):
        answer = {'pr_id': 'o/r#1'}
        chat = {'choices': [{'finish_reason': 'stop', 'message': {'content': '<think>ignored</think>```json\n' + json.dumps(answer) + '\n```'}}]}
        self.assertEqual(answer, extract_annotation(chat, 'chat'))
        chat['choices'][0]['finish_reason'] = 'length'
        with self.assertRaises(ValueError): extract_annotation(chat, 'chat')
        with self.assertRaises(ValueError): extract_annotation({'status': 'incomplete'}, 'responses')

    def test_full_response_saved_and_no_sdk_retry_or_key_logs(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {'ARK_API_KEY': 'fixture-secret'}):
            root = Path(d)
            prompt, schema = root / 'prompt', root / 'schema'
            prompt.write_text('Classify'); schema.write_text('{}')
            response = {'status': 'completed', 'id': 'response-1', 'model': 'actual', 'usage': {'input_tokens': 4},
                        'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': '{"ok":true}'}]}]}
            calls = []
            def factory(**kwargs):
                self.assertEqual(0, kwargs['max_retries'])
                def call(**payload):
                    calls.append(payload)
                    return SimpleNamespace(model_dump=lambda: response)
                return SimpleNamespace(responses=SimpleNamespace(create=call), close=lambda: None)
            evaluator = ApiEvaluator('k3', client_factory=factory, sleep=lambda _: None)
            result, meta = evaluator(packet={}, image_paths=[], system_prompt=prompt, schema=schema, workdir=root, timeout=10)
            self.assertEqual({'ok': True}, result)
            self.assertEqual(1, len(calls))
            self.assertEqual(response, json.loads(Path(meta['provider_response']).read_text()))
            for path in root.iterdir():
                self.assertNotIn('fixture-secret', path.read_text())


class SourceArchiveTests(unittest.TestCase):
    def test_missing_merge_sha_uses_timeline_without_overwriting_raw(self):
        original = {'merged_at': '2025-01-01T00:00:00Z'}
        events = [{'event': 'merged', 'commit_id': 'abc', 'url': 'event-url'}]
        self.assertEqual('abc', merge_anchors(original, events)['resolved_sha'])
        self.assertNotIn('merge_commit_sha', original)
        original['merge_commit_sha'] = 'different'
        self.assertEqual('ambiguous', merge_anchors(original, events)['status'])

    def test_history_and_partitions_preserve_raw_sources(self):
        fake = FakeGitHub(1)
        def send(method, endpoint, payload, accept, token):
            if '/timeline?' in endpoint:
                return 200, {}, json.dumps([{'event': 'closed', 'created_at': '2020-02-01T00:00:00Z'},
                    {'event': 'reopened', 'created_at': '2020-02-02T00:00:00Z'}]).encode()
            if endpoint.endswith('/commits/merge'):
                return 200, {}, b'{"sha":"merge","parents":[{"sha":"base"}]}'
            return fake(method, endpoint, payload, accept, token)
        with tempfile.TemporaryDirectory() as d:
            store = Store(d)
            try:
                run = store.new_run({})
                record = enrich_with_history(API(store, run, send=send, sleep=lambda _: None), 'o/r', 1)
                self.assertEqual('complete', record['sections']['timeline']['status'])
                self.assertEqual(2, len(record['sections']['timeline']['items']))
                view = record['archival_view']
                self.assertEqual(['base'], view['git_anchors']['merge_parents'])
                self.assertFalse(view['problem_packet']['safe_for_problem_input'])
                self.assertIn('o/r#2:body', view['problem_packet']['candidate_source_ids'])
                self.assertNotIn('pr:body', view['problem_packet']['candidate_source_ids'])
                self.assertEqual(pr()['body'], next(x['text'] for x in view['documents'] if x['source_id'] == 'pr:body'))
                self.assertIsNone(view['f2p'])
                self.assertTrue(view['media'][0]['occurrences'])
            finally:
                store.close()

    def test_unverified_text_reference_is_curator_only_and_does_not_require_timeline(self):
        fake = FakeGitHub(1)
        timeline_calls = []

        def send(method, endpoint, payload, accept, token):
            if endpoint == '/graphql' and 'closingIssuesReferences' in payload['query']:
                connection = {'nodes': [], 'totalCount': 0,
                              'pageInfo': {'hasNextPage': False, 'endCursor': None}}
                body = {'data': {'repository': {'pullRequest': {'connection': connection}}}}
                return 200, {}, json.dumps(body).encode()
            if endpoint == '/repos/o/r/pulls/1' and 'json' in accept:
                value = pr()
                value['body'] = 'See the project policy at https://github.com/o/r/issues/2'
                return 200, {}, json.dumps(value).encode()
            if '/timeline?' in endpoint:
                timeline_calls.append(endpoint)
                if '/issues/2/' in endpoint:
                    return 500, {}, b'{"message":"should not fetch unverified reference timeline"}'
                return 200, {}, b'[]'
            if endpoint.endswith('/commits/merge'):
                return 200, {}, b'{"sha":"merge","parents":[{"sha":"base"}]}'
            return fake(method, endpoint, payload, accept, token)

        with tempfile.TemporaryDirectory() as d:
            store = Store(d)
            try:
                run = store.new_run({})
                record = enrich_with_history(
                    API(store, run, send=send, sleep=lambda _: None), 'o/r', 1)
                linked = record['sections']['linked_issues']['items'][0]
                self.assertEqual('complete', record['status'])
                self.assertEqual('not_required', linked['timeline']['status'])
                self.assertEqual('unverified_text_reference', linked['timeline']['reason'])
                self.assertEqual(1, len(timeline_calls))
                self.assertNotIn(
                    'o/r#2:body',
                    record['archival_view']['problem_packet']['candidate_source_ids'])
                self.assertIn(
                    'o/r#2:body',
                    record['archival_view']['curator_packet']['source_ids'])
            finally:
                store.close()


def row(n, body, title='Fix unique thing', merged=None):
    return {'repo': 'o/r', 'number': n, 'title': title, 'body': body, 'state': 'closed',
            'merged_at': merged or f'2025-01-{n:02d}T00:00:00Z', 'html_url': f'https://github.com/o/r/pull/{n}'}


class RepairRecallTests(unittest.TestCase):
    def test_relation_schema_requires_evidence_and_forbids_runtime_claims(self):
        import jsonschema
        schema = json.loads((Path(__file__).resolve().parents[1] / 'analysis/prompts/12_02_repair_relation_verifier.schema.json').read_text())
        jsonschema.Draft202012Validator.check_schema(schema)
        review = dict(schema_version='repair-relation-review-v1', edge_id='fixture', relation_type='unknown',
            confidence='low', chronology='unknown', evidence=[], counterevidence=[], missing_evidence=['diff'],
            discovery_time=None, oracle_risk=['needs_review'], reason='Insufficient evidence',
            action='retain_both_pending_runtime_validation', runtime_validation='not_executed')
        jsonschema.validate(review, schema)
        review['relation_type'] = 'incomplete_fix'
        with self.assertRaises(jsonschema.ValidationError): jsonschema.validate(review, schema)
        review['relation_type'] = 'unknown'; review['runtime_validation'] = 'passed'
        with self.assertRaises(jsonschema.ValidationError): jsonschema.validate(review, schema)

    def test_linked_text_not_misattributed_and_anchor_preserved(self):
        record = {'repo': 'o/r', 'sections': {'pull_request': {'data': row(1, '')}},
                  'archival_view': {'git_anchors': {'merge_sha': 'timeline-merge'}, 'documents': [
                      {'kind': 'referenced_pr', 'source_id': 'o/r#2:body', 'text': 'Still broken in #3'},
                      {'kind': 'issue', 'source_id': 'o/r#99:body', 'text': 'Missed in #3'}]}}
        result = build([row(1, '', title='One'), row(2, '', title='Two'), row(3, '', title='Three')], [record])
        self.assertEqual([], result['edges'])
        self.assertEqual([], result['node_signals'])
        self.assertEqual('timeline-merge', result['nodes'][0]['merge_commit_sha'])

    def test_pr_timeline_is_separate_signal(self):
        record = {'repo': 'o/r', 'sections': {'pull_request': {'data': row(1, '')}, 'timeline': {'items': [
            {'id': 100, 'source': {'issue': {'html_url': 'https://github.com/o/r/pull/2'}}}]}},
            'archival_view': {'documents': []}}
        result = build([row(1, '', title='One'), row(2, '', title='Two')], [record])
        self.assertEqual('pr_timeline_cross_reference', result['edges'][0]['signals'][0]['kind'])

    def test_explicit_reference_without_shared_files(self):
        result = build([row(1, ''), row(2, 'I missed this in #1', title='Another component')])
        self.assertEqual(1, len(result['edges']))
        edge = result['edges'][0]
        self.assertEqual(('o/r#1', 'o/r#2'), (edge['a'], edge['b']))
        self.assertEqual('unknown', edge['relation_type'])
        self.assertEqual('retain_both_pending_review', edge['action'])

    def test_same_issue_is_only_recall(self):
        result = build([row(1, 'Fixes #99', title='One'), row(2, 'Closes #99', title='Two')])
        self.assertEqual('shared_issue_reference', result['edges'][0]['signals'][0]['kind'])
        self.assertEqual('unknown', result['edges'][0]['relation_type'])

    def test_template_negation_and_quote_not_positive(self):
        result = build([row(1, '<!-- still broken #2 -->\nnot a regression\n> still broken #2\n- [ ] minimize regression'), row(2, '', title='Other')])
        self.assertEqual([], result['edges'])
        self.assertEqual({'negated_context', 'quoted_context', 'template_like'}, {s['qualification'] for s in result['node_signals']})

    def test_external_reference_and_no_fixed_time_window(self):
        result = build([row(1, 'Follow-up https://github.com/o/r/pull/999', merged='2020-01-01T00:00:00Z'),
                        row(2, 'Missed in #1', title='Other', merged='2026-01-01T00:00:00Z')])
        self.assertEqual(2, len(result['edges']))
        self.assertTrue(any(n['external'] for n in result['nodes']))

    def test_edge_cap_is_reported(self):
        result = build([row(i, 'Fixes #99', title=str(i)) for i in (1, 2, 3)], max_edges=1)
        self.assertEqual(1, len(result['edges']))
        self.assertTrue(result['omissions'])


if __name__ == '__main__':
    unittest.main()
