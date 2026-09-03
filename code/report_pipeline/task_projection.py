"""Materialize the checksum-stable Harbor view of a self-contained case.

The submitted case also contains curator ``meta/`` and append-only ``outputs/``.
Harbor 0.22 hashes every file below the task path, so runs must point at this
content-addressed projection rather than at the case directory itself.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from pathlib import Path

from report_pipeline.paths import TMP_ROOT
from report_pipeline.workflow import _task_inventory


TASK_ENTRIES = ("environment", "instruction.md", "solution", "task.toml", "tests")
CASE_ONLY_ENTRIES = {"meta", "outputs"}


def _validate_source(case: Path) -> None:
    observed = {entry.name for entry in case.iterdir()}
    missing = set(TASK_ENTRIES) - observed
    unexpected = observed - set(TASK_ENTRIES) - CASE_ONLY_ENTRIES
    if missing:
        raise ValueError(f"task_projection_missing_entries:{sorted(missing)}")
    if unexpected:
        raise ValueError(f"task_projection_unexpected_entries:{sorted(unexpected)}")
    for path in (case, *sorted(case.rglob("*"))):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"task_projection_symlink_forbidden:{path}")
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise ValueError(f"task_projection_special_file_forbidden:{path}")


def _copy_entry(source: Path, destination: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, destination, copy_function=shutil.copy2)
    else:
        shutil.copy2(source, destination)


def materialize(case: Path, root: Path | None = None) -> dict:
    """Return an immutable task projection and its content inventory.

    Existing projections are verified before reuse. The digest is calculated on
    exactly the bytes Harbor will see, never on ``meta/`` or ``outputs/``.
    """
    case = case.resolve()
    if not case.is_dir():
        raise ValueError("task_projection_case_missing")
    _validate_source(case)

    staging_parent = (root or (TMP_ROOT / "harbor-task-projections")).resolve()
    staging_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{case.name}-", dir=staging_parent) as temp:
        candidate = Path(temp) / "task"
        candidate.mkdir()
        for name in TASK_ENTRIES:
            _copy_entry(case / name, candidate / name)
        checksum, files = _task_inventory(candidate)
        destination = staging_parent / case.name / checksum
        if destination.exists():
            observed, observed_files = _task_inventory(destination)
            if observed != checksum or observed_files != files:
                raise ValueError("task_projection_existing_content_mismatch")
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(candidate, destination)

    return {
        "source": case,
        "path": destination,
        "sha256": checksum,
        "files": files,
        "entries": list(TASK_ENTRIES),
    }
