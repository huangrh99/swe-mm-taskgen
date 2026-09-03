#!/bin/bash
set -eu
mkdir -p /logs/verifier
printf '0\n' > /logs/verifier/reward.txt
sha256sum -c /tests/integrity.sha256 >/dev/null || {
  printf '%s\n' '{"status":"invalid","reward":0,"reason":"frozen_bootstrap_tampered"}' > /logs/verifier/test_results.json
  exit 0
}
git -C /testbed apply --whitespace=nowarn /tests/test.patch || {
  printf '%s\n' '{"status":"invalid","reward":0,"reason":"test_patch_apply_failed"}' > /logs/verifier/test_results.json
  exit 0
}
cd /testbed
set +e
pnpm exec vitest run \
  packages/mermaid/src/rendering-util/layout-algorithms/dagre/benchmark-self-loop.spec.js \
  packages/mermaid/src/rendering-util/layout-algorithms/dagre/mermaid-graphlib.spec.js \
  --reporter=json \
  --outputFile=/logs/verifier/vitest.json \
  > /logs/verifier/vitest.stdout.log 2> /logs/verifier/vitest.stderr.log
printf '%s\n' "$?" > /logs/verifier/vitest.exit_code
set -e
python3 /tests/grade.py
