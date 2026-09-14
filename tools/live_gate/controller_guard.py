#!/usr/bin/env python3
"""Identify the one local test-HA server without substring matching."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


DEBUG_BASENAMES = frozenset(
    {"verify_live_read.py", "test-gateway-auth", "test_gateway_auth.py"}
)


class GuardError(RuntimeError):
    """The global guard configuration or procfs scan is unusable."""


@dataclass(frozen=True)
class ScanResult:
    server_pids: tuple[int, ...]
    debug_pids: tuple[int, ...]


def _read_argv(proc_dir: Path) -> tuple[str, ...] | None:
    try:
        raw = (proc_dir / "cmdline").read_bytes()
    except OSError:
        return None
    if not raw or not raw.endswith(b"\0"):
        return None
    fields = raw[:-1].split(b"\0")
    if not fields or any(not field for field in fields):
        return None
    return tuple(os.fsdecode(field) for field in fields)


def _read_link(path: Path) -> Path:
    return path.resolve(strict=True)


def _is_zombie(proc_dir: Path) -> bool:
    try:
        text = (proc_dir / "stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True
    closing = text.rfind(")")
    fields = text[closing + 2 :].split() if closing >= 0 else ()
    return not fields or fields[0] == "Z"


def _canonical(value: str, cwd: Path) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = cwd / candidate
    return candidate.resolve(strict=False)


def _config_values(argv: tuple[str, ...]) -> tuple[str, ...] | None:
    values: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in {"-c", "--config"}:
            if index + 1 >= len(argv) or not argv[index + 1]:
                return None
            values.append(argv[index + 1])
            index += 2
            continue
        if token.startswith("--config="):
            value = token.split("=", 1)[1]
            if not value:
                return None
            values.append(value)
        index += 1
    return tuple(values)


def _is_launcher(argv: tuple[str, ...], cwd: Path, hass_launcher: Path) -> bool:
    if _canonical(argv[0], cwd) == hass_launcher:
        return True
    return len(argv) >= 3 and argv[1:3] == ("-m", "homeassistant")


def scan(
    proc_root: Path,
    *,
    ha_python: Path,
    hass_launcher: Path,
    ha_config: Path,
) -> ScanResult:
    try:
        expected_python = ha_python.resolve(strict=True)
        expected_launcher = hass_launcher.resolve(strict=True)
        expected_config = ha_config.resolve(strict=True)
        entries = tuple(proc_root.iterdir())
    except (OSError, RuntimeError) as error:
        raise GuardError("controller_scan_unavailable") from error

    servers: list[int] = []
    debuggers: list[int] = []
    for proc_dir in entries:
        if not proc_dir.name.isdecimal() or _is_zombie(proc_dir):
            continue
        argv = _read_argv(proc_dir)
        if argv is None:
            continue
        pid = int(proc_dir.name)
        if any(Path(token).name in DEBUG_BASENAMES for token in argv):
            debuggers.append(pid)
            continue
        if any(token == "--script" or token.startswith("--script=") for token in argv):
            continue
        try:
            executable = _read_link(proc_dir / "exe")
            cwd = _read_link(proc_dir / "cwd")
        except (OSError, RuntimeError):
            continue
        if executable != expected_python:
            continue
        if not _is_launcher(argv, cwd, expected_launcher):
            continue
        configs = _config_values(argv)
        if configs is None or len(configs) != 1:
            continue
        if _canonical(configs[0], cwd) == expected_config:
            servers.append(pid)
    return ScanResult(tuple(sorted(servers)), tuple(sorted(debuggers)))


def main() -> int:
    try:
        ha_python = Path(os.environ["HA_PYTHON"])
        hass_launcher = Path(os.environ["HASS_LAUNCHER"])
        ha_config = Path(os.environ["HA_CONFIG"])
        result = scan(
            Path(os.environ.get("OPENRBUS_PROC_ROOT", "/proc")),
            ha_python=ha_python,
            hass_launcher=hass_launcher,
            ha_config=ha_config,
        )
    except (GuardError, KeyError, OSError, RuntimeError, ValueError):
        print("SERVER_COUNT=0")
        print("SERVER_PID=")
        print("DEBUG_COUNT=0")
        return 2
    server_pid = str(result.server_pids[0]) if len(result.server_pids) == 1 else ""
    print(f"SERVER_COUNT={len(result.server_pids)}")
    print(f"SERVER_PID={server_pid}")
    print(f"DEBUG_COUNT={len(result.debug_pids)}")
    return int(len(result.server_pids) != 1 or bool(result.debug_pids))


if __name__ == "__main__":
    raise SystemExit(main())
