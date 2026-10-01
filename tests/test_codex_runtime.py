import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_runtime import CODEX_ISOLATION_ARGS, run_codex


class CodexIsolationArgsTests(unittest.TestCase):
    def test_isolation_args_fail_closed_without_changing_account_auth(self):
        self.assertEqual(
            CODEX_ISOLATION_ARGS,
            (
                "--disable",
                "plugins",
                "--disable",
                "apps",
            ),
        )
        self.assertFalse(
            any("auth" in value.casefold() for value in CODEX_ISOLATION_ARGS)
        )


@unittest.skipUnless(os.name == "posix", "process-group isolation requires POSIX")
class CodexRunnerTests(unittest.TestCase):
    def test_returns_a_complete_completed_process_on_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            completed = run_codex(
                [
                    sys.executable,
                    "-c",
                    (
                        "import sys; "
                        "sys.stdin.read(); "
                        "print('structured-output'); "
                        "print('diagnostic', file=sys.stderr)"
                    ),
                ],
                input="private subtitle prompt",
                text=True,
                capture_output=True,
                timeout=5,
                cwd=tmp,
                check=False,
            )

        self.assertIsInstance(completed, subprocess.CompletedProcess)
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "structured-output\n")
        self.assertEqual(completed.stderr, "diagnostic\n")

    def test_check_true_raises_for_a_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(subprocess.CalledProcessError) as raised:
                run_codex(
                    [sys.executable, "-c", "raise SystemExit(7)"],
                    input="private subtitle prompt",
                    text=True,
                    capture_output=True,
                    timeout=5,
                    cwd=tmp,
                    check=True,
                )

        self.assertEqual(raised.exception.returncode, 7)

    def test_timeout_kills_the_entire_process_group_and_redacts_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            pid_path = Path(tmp) / "pids.txt"
            child_script = (
                "import signal,time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "time.sleep(60)"
            )
            parent_script = (
                "import os,pathlib,signal,subprocess,sys,time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "child=subprocess.Popen([sys.executable,'-c',sys.argv[2]]); "
                "pathlib.Path(sys.argv[1]).write_text("
                "f'{os.getpid()}\\n{child.pid}\\n', encoding='utf-8'); "
                "print(sys.stdin.read(), file=sys.stderr, flush=True); "
                "time.sleep(60)"
            )
            command = [
                sys.executable,
                "-c",
                parent_script,
                str(pid_path),
                child_script,
            ]

            with self.assertRaises(subprocess.TimeoutExpired) as raised:
                run_codex(
                    command,
                    input="private subtitle prompt\nsensitive partial stderr",
                    text=True,
                    capture_output=True,
                    timeout=0.5,
                    cwd=tmp,
                    check=False,
                )

            self.assertTrue(pid_path.exists())
            pids = [int(value) for value in pid_path.read_text().splitlines()]
            self.assertEqual(len(pids), 2)
            for pid in pids:
                self.assertTrue(self._wait_until_gone(pid), f"PID {pid} survived")

        error = raised.exception
        self.assertIsNone(error.output)
        self.assertIsNone(error.stderr)
        self.assertNotIn("private subtitle prompt", str(error))
        self.assertNotIn("sensitive partial stderr", str(error))

    def test_success_removes_a_detached_child_that_closed_standard_io(self):
        with tempfile.TemporaryDirectory() as tmp:
            pid_path = Path(tmp) / "child-pid.txt"
            child_script = (
                "import os,signal,time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "[os.close(fd) for fd in (0,1,2)]; "
                "time.sleep(60)"
            )
            parent_script = (
                "import pathlib,subprocess,sys; "
                "child=subprocess.Popen([sys.executable,'-c',sys.argv[2]]); "
                "pathlib.Path(sys.argv[1]).write_text("
                "str(child.pid), encoding='utf-8'); "
                "print('structured-output', flush=True)"
            )

            completed = run_codex(
                [
                    sys.executable,
                    "-c",
                    parent_script,
                    str(pid_path),
                    child_script,
                ],
                input="private subtitle prompt",
                text=True,
                capture_output=True,
                timeout=5,
                cwd=tmp,
                check=False,
            )

            child_pid = int(pid_path.read_text())
            self.assertTrue(
                self._wait_until_gone(child_pid),
                f"PID {child_pid} survived successful completion",
            )

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "structured-output\n")

    def test_repeated_cleanup_decode_errors_cannot_skip_sigkill(self):
        with tempfile.TemporaryDirectory() as tmp:
            pid_path = Path(tmp) / "repeated-error-pids.txt"
            child_script = (
                "import signal,time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "time.sleep(60)"
            )
            parent_script = (
                "import os,pathlib,signal,subprocess,sys,time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "child=subprocess.Popen([sys.executable,'-c',sys.argv[2]]); "
                "pathlib.Path(sys.argv[1]).write_text("
                "f'{os.getpid()}\\n{child.pid}\\n', encoding='utf-8'); "
                "time.sleep(60)"
            )
            actual = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    parent_script,
                    str(pid_path),
                    child_script,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

            deadline = time.monotonic() + 2
            while not pid_path.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(pid_path.exists())
            pids = [int(value) for value in pid_path.read_text().splitlines()]

            class RepeatedDecodeErrorProcess:
                pid = actual.pid
                stdin = None
                stdout = None
                stderr = None

                @property
                def returncode(self):
                    return actual.returncode

                def communicate(self, *args, **kwargs):
                    raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")

                def poll(self):
                    return actual.poll()

                def kill(self):
                    return actual.kill()

                def wait(self, *args, **kwargs):
                    return actual.wait(*args, **kwargs)

            try:
                with patch(
                    "codex_runtime.subprocess.Popen",
                    return_value=RepeatedDecodeErrorProcess(),
                ):
                    with self.assertRaises(UnicodeDecodeError):
                        run_codex(
                            ["codex"],
                            input="private subtitle prompt",
                            text=True,
                            capture_output=True,
                            timeout=5,
                            cwd=tmp,
                            check=False,
                        )

                for pid in pids:
                    self.assertTrue(
                        self._wait_until_gone(pid),
                        f"PID {pid} survived repeated cleanup failure",
                    )
            finally:
                try:
                    os.killpg(actual.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    actual.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    actual.kill()
                    actual.wait(timeout=2)

    @staticmethod
    def _wait_until_gone(pid: int, timeout: float = 5) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            time.sleep(0.05)
        return False


if __name__ == "__main__":
    unittest.main()
