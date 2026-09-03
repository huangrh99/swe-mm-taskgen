#!/bin/bash
set -eu
mkdir -p /logs/verifier
printf '0\n' > /logs/verifier/reward.txt
if ! sha256sum -c /tests/integrity.sha256 > /logs/verifier/00_integrity.log 2>&1; then
  printf '%s\n' '{"schema":"carbon-22019-harbor-result-v1","status":"invalid","reward":0,"failure_ledger":"evidence","reason":"frozen_test_payload_tampered"}' > /logs/verifier/test_results.json
  exit 0
fi
if ! git -C /testbed apply --whitespace=nowarn /tests/test.patch > /logs/verifier/01_test_patch.log 2>&1; then
  printf '%s\n' '{"schema":"carbon-22019-harbor-result-v1","status":"invalid","reward":0,"failure_ledger":"evidence","reason":"test_patch_apply_failed"}' > /logs/verifier/test_results.json
  exit 0
fi
python3 /tests/sweb_grade.py
