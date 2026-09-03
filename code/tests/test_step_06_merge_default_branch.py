import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from analysis.scripts.step_06_filter_merged_default_branch import decision, screen, KEEP, REJECT, LEDGER


class MergeDefaultBranchScreeningTests(unittest.TestCase):
    def row(self, number=1):
        return {'repo': 'o/r', 'number': number, 'state': 'closed',
                'merged_at': '2025-05-01T00:00:00Z', 'created_at': '2025-01-01T00:00:00Z',
                'base': {'ref': 'trunk', 'repo': {'default_branch': 'trunk'}},
                'body': '原文\r\n```diff\r\n- unknown\r\n+ any\r\n```'}

    def test_default_branch_names_not_hardcoded(self):
        for branch in ('master', 'main', 'trunk', 'develop', 'v2'):
            row = self.row()
            row['base'] = {'ref': branch, 'repo': {'default_branch': branch}}
            self.assertEqual('kept', decision(row))

    def test_closed_and_commit_sha_do_not_prove_merge(self):
        row = self.row()
        row.update(merged_at=None, closed_at='2025-05-01T00:00:00Z', merge_commit_sha='abc')
        self.assertEqual('closed_without_merge', decision(row))
        row['state'] = 'open'
        self.assertEqual('still_open', decision(row))

    def test_non_default_and_missing_branch(self):
        row = self.row()
        row['base']['ref'] = 'feature/next'
        self.assertEqual('merged_to_non_default_branch', decision(row))
        row['base']['repo'] = None
        self.assertEqual('unknown_branch_metadata', decision(row))

    def test_missing_invalid_or_conflicting_merge_evidence(self):
        row = self.row()
        del row['merged_at']
        self.assertEqual('unknown_merge_metadata', decision(row))
        for timestamp in ('bad date', '2025-01-01', 123):
            row['merged_at'] = timestamp
            self.assertEqual('unknown_merge_metadata', decision(row))
        row = self.row()
        row['merged'] = False
        self.assertEqual('inconsistent_merge_metadata', decision(row))
        row.update(merged=True, merged_at=None)
        self.assertEqual('inconsistent_merge_metadata', decision(row))

    def test_exact_partition_ledger_hashes_and_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = [self.row(n) for n in range(4)]
            rows[1]['merged_at'] = None
            rows[2].update(state='open', merged_at=None)
            rows[3]['base']['ref'] = 'feature/x'
            original = [json.dumps(r, ensure_ascii=False).encode() + b'\n' for r in rows]
            source = root / 'input.jsonl'
            source.write_bytes(b''.join(original))
            result = screen(source, root / 'out', root / 'tmp')
            self.assertEqual(4, result['counts']['input_prs'])
            self.assertEqual(1, result['counts']['kept'])
            self.assertEqual(3, result['counts']['excluded_prs'])
            self.assertEqual(original[0], (root / 'out' / KEEP).read_bytes())
            self.assertEqual(b''.join(original[1:]), (root / 'out' / REJECT).read_bytes())
            ledger = [json.loads(line) for line in (root / 'out' / LEDGER).read_text().splitlines()]
            self.assertEqual([hashlib.sha256(raw).hexdigest() for raw in original],
                             [r['input_line_sha256'] for r in ledger])
            self.assertEqual([], list((root / 'tmp').iterdir()))
            for name, info in result['outputs'].items():
                self.assertEqual(info['sha256'], hashlib.sha256((root / 'out' / name).read_bytes()).hexdigest())

    def test_duplicate_input_never_publishes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'input.jsonl'
            source.write_text((json.dumps(self.row()) + '\n') * 2)
            with self.assertRaisesRegex(ValueError, 'Duplicate'):
                screen(source, root / 'out', root / 'tmp')
            self.assertFalse((root / 'out').exists())
            self.assertEqual([], list((root / 'tmp').iterdir()))


if __name__ == '__main__':
    unittest.main()
