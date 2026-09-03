import json
from pathlib import Path
import tempfile
import unittest

from analysis.scripts import step_08_02_select_cross_repo_candidate_batch as selection


def row(repo, number, body, title='Fix visual defect', images=1):
    return {
        'repo': repo,
        'number': number,
        'title': title,
        'body': body,
        'image_screening': {'assets': [
            {'media_kind': 'image', 'decoration_reason': None}
            for _ in range(images)
        ]},
    }


class CandidateSelectionTests(unittest.TestCase):
    def test_formal_quotas_total_one_hundred(self):
        self.assertEqual(sum(selection.DEFAULT_QUOTAS.values()), 100)

    def test_keeps_previous_cases_and_fills_each_repo_deterministically(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source.jsonl'
            rows = [
                row('a/r', 1, 'plain body ' * 20),
                row('a/r', 2, 'Fixes #12. Before screenshot. After screenshot. ' * 4),
                row('a/r', 3, 'Fixes #13. Expected layout. Actual overlap. ' * 4),
                row('b/r', 4, 'Fixes #14. Before render. After render. ' * 4),
                row('b/r', 5, 'plain body ' * 20),
            ]
            source.write_text(''.join(json.dumps(value) + '\n' for value in rows))
            first = selection.select(source, {'a/r': 2, 'b/r': 1}, ['a/r#1'], 'seed')
            second = selection.select(source, {'a/r': 2, 'b/r': 1}, ['a/r#1'], 'seed')
            ids = [selection.identity(value) for value, _ in first]
            self.assertEqual(ids, [selection.identity(value) for value, _ in second])
            self.assertEqual(ids[0], 'a/r#1')
            self.assertIn('a/r#2', ids)
            self.assertIn('b/r#4', ids)

    def test_rejects_source_repository_without_quota(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source.jsonl'
            source.write_text(json.dumps(row('unknown/r', 1, 'body ' * 30)) + '\n')
            with self.assertRaisesRegex(ValueError, 'without quotas'):
                selection.select(source, {'a/r': 1}, [], 'seed')

    def test_more_than_ten_associated_issues_is_temporarily_excluded(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source.jsonl'
            complex_body = ' '.join(f'Fixes #{number}.' for number in range(1, 12))
            rows = [row('a/r', 20, complex_body), row('a/r', 21, 'Fixes #1. Visual layout bug. ' * 8)]
            source.write_text(''.join(json.dumps(value) + '\n' for value in rows))
            chosen = selection.select(source, {'a/r': 1}, ['a/r#20'], 'seed')
            self.assertEqual([selection.identity(value) for value, _ in chosen], ['a/r#21'])
            self.assertEqual(selection.signals(rows[0])['direct_issue_reference_count'], 11)
            self.assertTrue(selection.signals(rows[0])['temporarily_excluded_over_complex'])

    def test_only_statically_confirmed_issue_references_count_toward_complexity(self):
        candidate = row(
            'a/r', 20,
            ('PR #2; see other/repo#3; '
             'https://github.com/a/r/pull/4; CHANGELOG.md#8203; '
             'https://github.com/a/r/issues/5; Fixes #6; '
             'resolves other/repo#7; Fixes #6.'),
        )
        self.assertEqual(
            selection.associated_issues(candidate),
            {('a/r', 5), ('a/r', 6), ('other/repo', 7)},
        )
        self.assertEqual(selection.signals(candidate)['direct_issue_reference_count'], 3)


if __name__ == '__main__':
    unittest.main()
