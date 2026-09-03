#!/bin/bash
set -eu
mkdir -p /logs/verifier
printf '0\n' > /logs/verifier/reward.txt
sha256sum -c /tests/integrity.sha256 >/dev/null || {
  printf '%s\n' '{"status":"invalid","reward":0,"reason":"frozen_test_material_tampered"}' > /logs/verifier/test_results.json
  exit 0
}
python3 /tests/verify.py
