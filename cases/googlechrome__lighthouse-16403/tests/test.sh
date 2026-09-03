#!/bin/bash
set -eu
mkdir -p /logs/verifier
printf '0\n' > /logs/verifier/reward.txt
if ! sha256sum -c /tests/integrity.sha256 > /logs/verifier/00_integrity.log 2>&1; then
  printf '%s\n' '{"schema":"lighthouse-16403-harbor-result-v1","status":"invalid","reward":0,"failure_ledger":"evidence","reason":"frozen_test_payload_tampered"}' > /logs/verifier/test_results.json
  exit 0
fi
python3 /tests/sweb_grade.py
