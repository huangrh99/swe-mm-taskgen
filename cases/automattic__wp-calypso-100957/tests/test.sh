#!/bin/bash
set -eu
mkdir -p /logs/verifier
printf "0\n" > /logs/verifier/reward.txt
sha256sum -c /tests/integrity.sha256 >/dev/null || {
  printf '%s\n' '{"status":"invalid","reward":0,"reason":"frozen_bootstrap_tampered"}' > /logs/verifier/test_results.json
  exit 0
}
install -D -m 0444 /tests/acceptance.test.tsx /testbed/packages/global-styles/src/components/global-styles-variations/__tests__/preview.test.tsx
python3 /tests/sweb_grade.py
