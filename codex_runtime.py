"""Fail-closed process isolation for non-interactive Codex calls."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path


# These supported Codex 0.146.0 feature switches complement the translator's
# existing read-only sandbox, ignored config/rules, disabled web search, and
# approval-policy arguments. They deliberately do not alter CODEX_HOME or auth.
CODEX_ISOLATION_ARGS = (
    "--disable",
    "plugins",
    "--disable",
    "apps",
)

_TERMINATION_GRACE_SECONDS = 0.25
_GROUP_POLL_SECONDS = 0.01


def _group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _signal_group(process_group_id: int, sig: signal.Signals) -> None:
    try:
        os.killpg(process_group_id, sig)
    except OSError:
        pass


def _wait_for_group_exit(process_group_id: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _group_exists(process_group_id):
            return True
        time.sleep(_GROUP_POLL_SECONDS)
    return not _group_exists(process_group_id)


def _stop_process_group(process: subprocess.Popen[object]) -> None:
    """Stop the private process group without retaining captured output."""

    process_group_id = process.pid
    try:
        _signal_group(process_group_id, signal.SIGTERM)
        try:
            process.wait(timeout=_TERMINATION_GRACE_SECONDS)
        except BaseException:
            pass

        if _wait_for_group_exit(
            process_group_id,
            _TERMINATION_GRACE_SECONDS,
        ):
            return

        _signal_group(process_group_id, signal.SIGKILL)
        try:
            if process.poll() is None:
                process.kill()
        except BaseException:
            pass
        try:
            process.wait(timeout=_TERMINATION_GRACE_SECONDS)
        except BaseException:
            pass
        _wait_for_group_exit(process_group_id, _TERMINATION_GRACE_SECONDS)
    finally:
        for stream_name in ("stdin", "stdout", "stderr"):
            stream = getattr(process, stream_name, None)
            if stream is None:
                continue
            try:
                stream.close()
            except BaseException:
                pass


def _stop_lingering_descendants(process_group_id: int) -> None:
    """Remove descendants that outlived an already-reaped CLI process."""

    if not _group_exists(process_group_id):
        return
    _signal_group(process_group_id, signal.SIGTERM)
    if not _wait_for_group_exit(
        process_group_id,
        _TERMINATION_GRACE_SECONDS,
    ):
        _signal_group(process_group_id, signal.SIGKILL)
        _wait_for_group_exit(process_group_id, _TERMINATION_GRACE_SECONDS)


def run_codex(
    command: Sequence[str | os.PathLike[str]],
    *,
    input: str | bytes | None,
    text: bool,
    capture_output: bool,
    timeout: float | None,
    cwd: str | os.PathLike[str] | None,
    check: bool,
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    """Run one Codex CLI process and guarantee POSIX process-group cleanup.

    Timeout exceptions are recreated without captured stdout, stderr, or stdin,
    so a subtitle prompt and partial model response cannot escape through the
    exception object.
    """

    if os.name != "posix":
        raise RuntimeError("Codex process-group isolation requires POSIX")
    if not command:
        raise ValueError("Codex command must not be empty")

    safe_command = [os.fspath(value) for value in command]
    process = subprocess.Popen(
        safe_command,
        stdin=subprocess.PIPE if input is not None else None,
        stdout=subprocess.PIPE if capture_output else None,
        stderr=subprocess.PIPE if capture_output else None,
        text=text,
        cwd=Path(cwd) if cwd is not None else None,
        start_new_session=True,
    )

    try:
        stdout, stderr = process.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired:
        _stop_process_group(process)
        raise subprocess.TimeoutExpired(
            cmd=safe_command,
            timeout=timeout,
        ) from None
    except BaseException:
        _stop_process_group(process)
        raise

    _stop_lingering_descendants(process.pid)
    completed = subprocess.CompletedProcess(
        safe_command,
        process.returncode,
        stdout,
        stderr,
    )
    if check and completed.returncode != 0:
        raise subprocess.CalledProcessError(
            completed.returncode,
            completed.args,
            output=completed.stdout,
            stderr=completed.stderr,
        )
    return completed
