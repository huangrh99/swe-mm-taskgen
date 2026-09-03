#!/bin/bash
set -eu
cd /testbed
git apply --whitespace=nowarn /solution/gold.patch
