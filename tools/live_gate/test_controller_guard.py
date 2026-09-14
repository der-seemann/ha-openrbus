from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from controller_guard import scan


class ControllerGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.proc = self.root / "proc"
        self.proc.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.python_real = self.bin / "python-real"
        self.python_real.touch()
        self.python = self.bin / "python"
        self.python.symlink_to(self.python_real.name)
        self.other_python = self.bin / "other-python"
        self.other_python.touch()
        self.hass = self.bin / "hass"
        self.hass.touch()
        self.config = self.root / "config"
        self.config.mkdir()
        self.cwd = self.root / "cwd"
        self.cwd.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def add_process(
        self,
        pid: int,
        argv: list[str],
        *,
        executable: Path | None = None,
        cwd: Path | None = None,
        zombie: bool = False,
        malformed: bool = False,
    ) -> None:
        proc_dir = self.proc / str(pid)
        proc_dir.mkdir()
        (proc_dir / "exe").symlink_to(
            os.path.relpath(executable or self.python_real, proc_dir)
        )
        (proc_dir / "cwd").symlink_to(os.path.relpath(cwd or self.cwd, proc_dir))
        state = "Z" if zombie else "S"
        (proc_dir / "stat").write_text(
            f"{pid} (synthetic proc) {state} 1 1 1 0 0\n", encoding="utf-8"
        )
        raw = b"\0".join(os.fsencode(argument) for argument in argv)
        (proc_dir / "cmdline").write_bytes(raw if malformed else raw + b"\0")

    def result(self):
        return scan(
            self.proc,
            ha_python=self.python,
            hass_launcher=self.hass,
            ha_config=self.config,
        )

    def test_exact_hass_and_module_invocations(self) -> None:
        self.add_process(101, [str(self.hass), "--config", str(self.config)])
        self.add_process(
            102, [str(self.python), "-m", "homeassistant", "-c", str(self.config)]
        )
        self.assertEqual(self.result().server_pids, (101, 102))

    def test_auth_helper_and_codex_wrapper_are_not_servers(self) -> None:
        self.add_process(
            103,
            [str(self.hass), "--script", "auth", "-c", str(self.config)],
        )
        self.add_process(
            104,
            ["codex wrapper contains homeassistant and verify_live_read.py"],
            executable=self.other_python,
        )
        result = self.result()
        self.assertEqual(result.server_pids, ())
        self.assertEqual(result.debug_pids, ())

    def test_module_tokens_after_a_script_are_not_a_server(self) -> None:
        self.add_process(
            115,
            [
                str(self.python),
                "wrapper.py",
                "-m",
                "homeassistant",
                "-c",
                str(self.config),
            ],
        )
        self.assertEqual(self.result().server_pids, ())

    def test_wrong_duplicate_and_relative_configs(self) -> None:
        wrong = self.root / "wrong"
        wrong.mkdir()
        relative = self.cwd / "relative"
        relative.mkdir()
        self.add_process(105, [str(self.hass), "-c", str(wrong)])
        self.add_process(
            106,
            [str(self.hass), "-c", str(self.config), "--config", str(self.config)],
        )
        self.config.rmdir()
        self.config.symlink_to(relative)
        self.add_process(107, [str(self.hass), "--config=relative"], cwd=self.cwd)
        self.assertEqual(self.result().server_pids, (107,))

    def test_python_realpath_zombie_and_malformed(self) -> None:
        self.add_process(
            108,
            [str(self.python), "-m", "homeassistant", "-c", str(self.config)],
            executable=self.python_real,
        )
        self.add_process(
            109, [str(self.hass), "-c", str(self.config)], zombie=True
        )
        self.add_process(
            110, [str(self.hass), "-c", str(self.config)], malformed=True
        )
        self.assertEqual(self.result().server_pids, (108,))

    def test_debug_is_exact_and_interpreter_independent(self) -> None:
        self.add_process(
            111, [str(self.other_python), "/tmp/verify_live_read.py"], executable=self.other_python
        )
        self.add_process(
            112, [str(self.other_python), "/tmp/not_verify_live_read.py"], executable=self.other_python
        )
        self.add_process(
            113, [str(self.other_python), "/tmp/test-gateway-auth"], executable=self.other_python
        )
        self.assertEqual(self.result().debug_pids, (111, 113))

    def test_cli_protocol(self) -> None:
        self.add_process(
            114,
            [sys.executable, "-m", "homeassistant", "-c", str(self.config)],
            executable=Path(sys.executable).resolve(),
        )
        script = Path(__file__).with_name("controller_guard.py")
        environment = {
            **os.environ,
            "OPENRBUS_PROC_ROOT": str(self.proc),
            "HA_PYTHON": sys.executable,
            "HASS_LAUNCHER": str(self.hass),
            "HA_CONFIG": str(self.config),
        }
        result = subprocess.run(
            [sys.executable, str(script)],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            result.stdout.splitlines(),
            ["SERVER_COUNT=1", "SERVER_PID=114", "DEBUG_COUNT=0"],
        )


if __name__ == "__main__":
    unittest.main()
