"""Symlink-safe atomic publication primitives for formal evidence files."""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets


def assert_no_symlink_chain(path: Path) -> None:
    current = path.absolute()
    while True:
        if current.is_symlink():
            raise ValueError(f"unsafe symlink in publication path: {current}")
        if current.parent == current:
            return
        current = current.parent


def write_bytes(path: Path, payload: bytes) -> None:
    """Write with no-follow dirfds, fsync, then replace in the opened directory."""
    path = path.absolute()
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory = os.open(path.anchor, directory_flags)
    try:
        for component in path.parent.parts[1:]:
            try:
                child = os.open(component, directory_flags, dir_fd=directory)
            except FileNotFoundError:
                os.mkdir(component, dir_fd=directory)
                child = os.open(component, directory_flags, dir_fd=directory)
            os.close(directory)
            directory = child
        temporary_name = f".{path.name}.{secrets.token_hex(16)}.tmp"
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600, dir_fd=directory,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.replace(temporary_name, path.name,
                       src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary_name, dir_fd=directory)
            except FileNotFoundError:
                pass
    finally:
        os.close(directory)


def write_json(path: Path, value: dict) -> None:
    write_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())
