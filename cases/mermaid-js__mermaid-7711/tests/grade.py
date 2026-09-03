#!/usr/bin/env python3
import json
from pathlib import Path

CONFIG = Path('/tests/config.json')
RESULT = Path('/logs/verifier/vitest.json')
OUT = Path('/logs/verifier/test_results.json')
FRAMEWORK_OUT = Path('/logs/verifier/framework_results.json')
REWARD = Path('/logs/verifier/reward.txt')


def write(payload, framework, reward):
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n')
    FRAMEWORK_OUT.write_text(json.dumps(framework, indent=2, sort_keys=True) + '\n')
    REWARD.write_text(f'{reward:.1f}\n')


def main():
    config = json.loads(CONFIG.read_text())
    if not RESULT.exists():
        write({'status': 'error', 'reason': 'vitest_json_missing'}, {}, 0.0)
        return
    raw = json.loads(RESULT.read_text())
    observed = {}
    assertions = []
    for suite in raw.get('testResults', []):
        for assertion in suite.get('assertionResults', []):
            test_id = assertion.get('fullName')
            status = assertion.get('status', 'missing')
            if test_id:
                observed[test_id] = status
                assertions.append({'test_id': test_id, 'status': status})

    required = {
        'FAIL_TO_PASS': config['FAIL_TO_PASS'],
        'PASS_TO_PASS': config['PASS_TO_PASS'],
    }
    classified = {}
    all_pass = True
    for category, ids in required.items():
        classified[category] = []
        for test_id in ids:
            status = observed.get(test_id, 'missing')
            classified[category].append({'test_id': test_id, 'status': status})
            all_pass = all_pass and status == 'passed'

    expected_ids = set(config['FAIL_TO_PASS'] + config['PASS_TO_PASS'])
    unexpected = sorted(set(observed) - expected_ids)
    all_pass = all_pass and not unexpected
    reward = 1.0 if all_pass else 0.0
    payload = {
        'schema_version': 'vitest-functional-grade-v1',
        'status': 'passed' if all_pass else 'test_failure',
        'reward': reward,
        'classified_results': classified,
        'unexpected_test_ids': unexpected,
        'counts': {
            'pass': sum(item['status'] == 'passed' for item in assertions),
            'fail': sum(item['status'] == 'failed' for item in assertions),
            'skip': sum(item['status'] in {'pending', 'skipped', 'todo'} for item in assertions),
            'missing': sum(item['status'] == 'missing' for items in classified.values() for item in items),
        },
    }
    framework = {
        'schema_version': 'vitest-json-v1',
        'num_total_tests': raw.get('numTotalTests'),
        'num_passed_tests': raw.get('numPassedTests'),
        'num_failed_tests': raw.get('numFailedTests'),
        'assertions': assertions,
    }
    write(payload, framework, reward)


if __name__ == '__main__':
    main()
