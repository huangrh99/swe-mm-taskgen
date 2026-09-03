#!/bin/bash
set -eu
mkdir -p /logs/verifier /results
printf "0\n" > /logs/verifier/reward.txt
sha256sum -c /tests/integrity.sha256 >/dev/null || {
  printf '%s\n' '{"schema_version":"xyflow-playwright-verifier-v2","status":"invalid","reward":0,"failure_ledger":"evidence","retryable":false,"reason":"frozen_test_material_tampered"}' > /logs/verifier/test_results.json
  exit 0
}
git -C /testbed apply --whitespace=nowarn /tests/test.patch || {
  printf '%s\n' '{"schema_version":"xyflow-playwright-verifier-v2","status":"invalid","reward":0,"failure_ledger":"evidence","retryable":false,"reason":"test_patch_apply_failed"}' > /logs/verifier/test_results.json
  exit 0
}
python3 /tests/sweb_grade.py
