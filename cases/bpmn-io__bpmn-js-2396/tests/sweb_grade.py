"""Strict frozen-test verifier. Runs inside Harbor, never applies a repair patch."""
import hashlib
import json
import os
from pathlib import Path
import resource
import shutil
import subprocess
import time

APP = Path('/testbed')
TESTS = Path('/tests')
LOGS = Path('/logs/verifier')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check(condition, message):
    if not condition:
        raise ValueError(message)


def inventory(root, excluded=()):
    result = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        relative = Path(directory).relative_to(root)
        if relative == Path('.'):
            dirs[:] = [d for d in dirs if d not in excluded]
        for name in dirs + files:
            path = Path(directory) / name
            if path.is_symlink():
                result[path.relative_to(root).as_posix()] = 'symlink:' + os.readlink(path)
            elif path.is_file():
                result[path.relative_to(root).as_posix()] = digest(path)
    return result


def judge(report, expected, code):
    check(report.get('complete') is True and not report.get('browser_errors'), 'incomplete_or_browser_error')
    rows = report.get('tests', [])
    actual_ids = [t['test_id'] for t in rows]
    check(bool(rows) and len(set(actual_ids)) == len(actual_ids), 'empty_or_duplicate_tests')
    check(set(actual_ids) == set(expected), 'missing_or_unexpected_test_ids')
    check(all(t['status'] in ('pass', 'fail', 'skip') for t in rows), 'unknown_test_status')
    counts = {s: sum(t['status'] == s for t in rows) for s in ('pass', 'fail', 'skip')}
    summary = report['framework_summary']
    check(not summary['error'] and not summary['disconnected'], 'framework_resource_error')
    check([summary['success'], summary['failed'], summary['skipped']] ==
          [counts['pass'], counts['fail'], counts['skip']], 'framework_count_mismatch')
    expected_code = int(counts['fail'] > 0)
    check(code == report['runner_exit_code'] == summary['exitCode'] == expected_code, 'exit_code_mismatch')
    check(counts['skip'] == 0, 'skipped_required_tests')
    return int(counts['fail'] == 0), counts


def main():
    LOGS.mkdir(parents=True, exist_ok=True)
    (LOGS / 'reward.txt').write_text('0\n')
    result = {'schema': 'harbor-frozen-test-result-v1', 'status': 'invalid', 'reward': 0,
              'production_changes_preserved': None, 'tests': [], 'started_at_unix': time.time()}
    before = None
    try:
        config = json.loads((TESTS / 'config.json').read_text())
        check(config['FAIL_TO_PASS'] and config['PASS_TO_PASS'], 'empty_required_transition_group')
        expected = config['FAIL_TO_PASS'] + config['PASS_TO_PASS']
        check(len(expected) == len(set(expected)), 'overlapping_or_duplicate_required_tests')
        before = inventory(APP, excluded=('test', 'node_modules', '.git'))
        payload = TESTS / 'payload/test'
        reference = inventory(payload)
        check(all(not value.startswith('symlink:') for value in reference.values()), 'unsupported_test_symlink')
        harness = Path('/harness')
        harness.mkdir(exist_ok=False)
        frozen_tests = harness / 'test'
        shutil.copytree(payload, frozen_tests)
        check(inventory(frozen_tests) == reference, 'frozen_test_copy_mismatch')
        os.symlink(APP / 'node_modules', harness / 'node_modules', target_is_directory=True)
        os.symlink(APP / 'lib', harness / 'lib', target_is_directory=True)
        os.symlink(APP / 'assets', harness / 'assets', target_is_directory=True)
        for name in ('test_manifest.json', 'sweb_runner.cjs'):
            shutil.copyfile(TESTS / name, harness / name)
        output = Path('/results/framework_results.json')
        check(not output.exists() and not output.is_symlink() and not output.parent.is_symlink(), 'stale_framework_output')
        def limits():
            resource.setrlimit(resource.RLIMIT_FSIZE, (32 * 1024 * 1024, 32 * 1024 * 1024))
        with (LOGS / '15_test_stdout.log').open('wb') as log:
            proc = subprocess.run(['node', '/harness/sweb_runner.cjs'], cwd=APP,
                stdout=log, stderr=subprocess.STDOUT, timeout=600, preexec_fn=limits)
        check(output.is_file() and not output.is_symlink() and output.stat().st_size < 32 * 1024 * 1024,
              'missing_or_invalid_framework_report')
        report = json.loads(output.read_text())
        shutil.copyfile(output, LOGS / 'framework_results.json')
        result['tests'] = report.get('tests', [])
        result['reward'], result['counts'] = judge(report, expected, proc.returncode)
        result['status'] = 'passed' if result['reward'] else 'test_failure'
        result['f2p'], result['p2p'] = config['FAIL_TO_PASS'], config['PASS_TO_PASS']
        result['config_sha256'] = digest(TESTS / 'config.json')
        # Ensure test execution did not overwrite verifier-private assertions/resources.
        check(inventory(frozen_tests) == reference, 'frozen_tests_changed_during_execution')
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        result.update(status='invalid', reward=0, reason=f'{type(exc).__name__}: {exc}')
    finally:
        if before is not None:
            after = inventory(APP, excluded=('test', 'node_modules', '.git'))
            result['production_changes_preserved'] = before == after
            result['production_snapshot_before_sha256'] = hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest()
            result['production_snapshot_after_sha256'] = hashlib.sha256(json.dumps(after, sort_keys=True).encode()).hexdigest()
            if before != after:
                result.update(status='invalid', reward=0, reason='production_changed_during_verification')
        result['finished_at_unix'] = time.time()
        (LOGS / 'test_results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        (LOGS / 'reward.txt').write_text(str(result['reward']) + '\n')


if __name__ == '__main__':
    main()
