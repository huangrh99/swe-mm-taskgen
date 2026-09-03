"""Authoritative roots for the formal submission code.

Modules must not infer the workspace from their own historical stage depth.
"""

from pathlib import Path


CODE_ROOT = Path(__file__).resolve().parents[1]
REPORT_ROOT = CODE_ROOT.parent
# Compatibility name for older modules. It now means this standalone report
# repository, never its parent checkout.
WORKSPACE_ROOT = REPORT_ROOT
CASES_ROOT = REPORT_ROOT / "cases"
REPORT_META_ROOT = REPORT_ROOT / "meta"
RUNTIME_ROOT = REPORT_ROOT / ".runtime"
TMP_ROOT = RUNTIME_ROOT / "tmp"
RUNS_ROOT = RUNTIME_ROOT / "runs"
