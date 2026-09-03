#!/usr/bin/env python3
"""Isolated public entrypoint for the formal report snapshot."""

from __future__ import annotations

import sys
from pathlib import Path


REPORT_ROOT = Path(__file__).resolve().parent
CODE_ROOT = REPORT_ROOT / "code"


def _resolved(value: str) -> Path:
    return Path(value or ".").resolve()


# The parent checkout may contain historical packages with the same names.
# Import only this repository's code and remove both the repository cwd and its
# parent from the inherited path. Site-packages and stdlib remain intact.
sys.path[:] = [str(CODE_ROOT)] + [
    item
    for item in sys.path
    if _resolved(item) not in {REPORT_ROOT.parent, REPORT_ROOT, CODE_ROOT}
]

from report_pipeline.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
