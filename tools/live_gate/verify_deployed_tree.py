"""Strict lstat-based comparison of reviewed and deployed integration trees."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import subprocess
import sys


class TreeError(RuntimeError):
    pass


def fail(code: str) -> None:
    raise TreeError(code)


def expected_files(repo: Path) -> set[str]:
    try:
        output = subprocess.check_output(
            ["git", "-C", str(repo), "ls-files", "custom_components/openrbus"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        fail("git_index_unavailable")
    prefix = "custom_components/openrbus/"
    files = {line.removeprefix(prefix) for line in output.splitlines() if line.startswith(prefix)}
    if not files:
        fail("expected_tree_empty")
    return files


def expected_dirs(files: set[str]) -> set[str]:
    result = {""}
    for file_name in files:
        parent = Path(file_name).parent
        while str(parent) != ".":
            result.add(str(parent))
            parent = parent.parent
    return result


def scan(root: Path) -> tuple[set[str], set[str]]:
    try:
        root_mode = os.lstat(root).st_mode
    except OSError:
        fail("tree_root_missing")
    if not stat.S_ISDIR(root_mode):
        fail("tree_root_not_directory")
    dirs = {""}
    files: set[str] = set()
    todo = [Path("")]
    while todo:
        relative = todo.pop()
        directory = root / relative
        try:
            entries = list(os.scandir(directory))
        except OSError:
            fail("tree_scan_failed")
        for entry in entries:
            entry_relative = str(relative / entry.name)
            try:
                mode = os.lstat(entry.path).st_mode
            except OSError:
                fail("tree_lstat_failed")
            if stat.S_ISDIR(mode):
                dirs.add(entry_relative)
                todo.append(relative / entry.name)
            elif stat.S_ISREG(mode):
                files.add(entry_relative)
            else:
                fail("tree_special_or_symlink")
    return dirs, files


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify(repo: Path, source: Path, deployed: Path) -> None:
    expected = expected_files(repo)
    expected_directory_set = expected_dirs(expected)
    source_dirs, source_files = scan(source)
    deployed_dirs, deployed_files = scan(deployed)
    if source_dirs != expected_directory_set or source_files != expected:
        fail("reviewed_tree_has_untracked_entries")
    if deployed_dirs != expected_directory_set or deployed_files != expected:
        fail("deployed_tree_not_exact")
    for relative in expected:
        if sha256(source / relative) != sha256(deployed / relative):
            fail("deployed_tree_content_mismatch")


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        return 2
    try:
        verify(Path(argv[1]), Path(argv[2]), Path(argv[3]))
    except TreeError as error:
        print(f"ERROR={error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
