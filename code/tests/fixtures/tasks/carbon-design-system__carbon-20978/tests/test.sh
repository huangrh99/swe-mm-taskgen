#!/bin/bash
set -eu
mkdir -p /logs/verifier
if ! /usr/bin/python3 -I /tests/integrity.py '{"/tests/verify.py":"17c2ba753aecb8a463c88167d13ba9b1cc3fe486566c5d859c1b5a0900aaf1d7","/tests/test_manifest.json":"de4f60a9722b7236d4d1beaf89222a7deb1c84de7333daecb43ff00a19a0d561","/tests/frozen_inventory.json":"e36eccd631a40262a5edd6dd7dad0d718c2555abdee4f32886a2bef63d60ad54","/tests/functional_runner.py":"87a547498f07c0229d728082bd2639e31a7472daaf485b63a0c0170ac17705d6"}'; then exit 0; fi
/usr/bin/python3 -I /tests/verify.py
