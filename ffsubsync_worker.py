"""Private stdin/stdout adapter for ffsubsync==0.5.1; no raw diagnostics escape.

Run by SubtitleSynchronizer in a new process group. This file deliberately does
not import the main application so Python's isolated mode can be used.
"""
import contextlib
import importlib.metadata
import json
import logging
import math
import os
from pathlib import Path
import shlex
import shutil
import signal
import sys
import time


def restricted_binaries(reference, private_directory):
    """Constrain probing to self-contained media, never playlists or URLs in it.

    ffsubsync delegates audio extraction to ffmpeg/ffprobe. Both must carry
    identical input restrictions: otherwise a playlist could cause either tool
    to fetch nested media outside the authenticated, allowlisted proxy.
    """
    remote = reference.startswith("http://127.0.0.1:")
    if not remote and Path(reference).suffix.lower() in (".npz", ".npy", ".srt", ".ass", ".ssa"):
        return None  # These local fixtures use no FFmpeg process.
    binaries = {name: shutil.which(name) for name in ("ffmpeg", "ffprobe")}
    if not all(binaries.values()):
        raise FileNotFoundError("media tools unavailable")
    directory = Path(private_directory) / "media-tools"
    directory.mkdir(mode=0o700)
    protocols = "tcp,http" if remote else "file,tcp,http"
    formats = "matroska,webm,mov,avi,mpegts,mpeg,flv,ogg,wav,flac,mp3,aac"
    for name, binary in binaries.items():
        driver = directory / (name + ".py")
        driver.write_text(
            "import os, sys\n"
            "blocked = {'-protocol_whitelist', '-protocol_blacklist', '-format_whitelist'}\n"
            "if any(value.split('=', 1)[0] in blocked for value in sys.argv[1:]):\n"
            "    sys.exit(64)\n"
            f"binary = {str(Path(binary).resolve())!r}\n"
            f"prefix = ['-protocol_whitelist', {protocols!r}, '-format_whitelist', {formats!r}]\n"
            "os.execv(binary, [binary, *prefix, *sys.argv[1:]])\n",
            encoding="utf-8",
        )
        driver.chmod(0o600)
        wrapper = directory / name
        # Only our fixed interpreter/driver paths are quoted into this launcher;
        # all library arguments are forwarded literally, never shell-evaluated.
        wrapper.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable)
                           + " -I " + shlex.quote(str(driver)) + ' "$@"\n', encoding="utf-8")
        wrapper.chmod(0o700)
    return str(directory)


def synchronize(payload, api, ffmpeg_path=None):
    values = [
        payload["reference"], "-i", payload["source"], "-o", payload["output"],
        "--encoding", "utf-8-sig", "--output-encoding", "utf-8",
        "--vad", "webrtc", "--multi-segment-sync", "--segment-count", "8",
        "--parallel-workers", "1", "--skip-intro-outro",
        "--skip-sync-on-low-quality", "--quality-max-offset-seconds", "30",
        "--min-score", "1",
        "--max-framerate-deviation", "0.1", "--max-offset-seconds", "30",
    ]
    if ffmpeg_path is not None:
        values.extend(["--ffmpeg-path", ffmpeg_path])
    args = api.make_parser().parse_args(values)
    result = api.run(args)
    return {
        "sync_was_successful": result.get("sync_was_successful") is True
        and result.get("retval") == 0,
        "offset_seconds": result.get("offset_seconds"),
        "framerate_scale_factor": result.get("framerate_scale_factor"),
    }


def _start_watchdog(timeout):
    """A separate POSIX watchdog kills the whole group, including ffmpeg.

    subprocess.run only kills the immediate child on timeout. A separate process
    remains able to stop descendants even if scientific code blocks Python or
    the worker dies. The worker is always launched in its own process group.
    """
    if not hasattr(os, "fork") or os.getpgrp() != os.getpid():
        raise ValueError("isolated POSIX process group required")
    group = os.getpgrp()
    child = os.fork()
    if child == 0:
        time.sleep(timeout)
        try:
            os.write(1, b'{"error":"timeout"}\n')
            os.killpg(group, signal.SIGKILL)
        finally:
            os._exit(124)
    return child


def main():
    watchdog = None
    result = {"error": "execution"}
    try:
        payload = json.loads(sys.stdin.read(16385))
        timeout = payload["timeout"]
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or not 0 < timeout <= 86400):
            raise ValueError()
        watchdog = _start_watchdog(timeout)
        # Both Python logging and accidental third-party prints are discarded.
        # ffmpeg receives only our opaque loopback proxy URL, never credentials.
        logging.disable(logging.CRITICAL)
        with open(os.devnull, "w") as null, contextlib.redirect_stdout(null), contextlib.redirect_stderr(null):
            try:
                if importlib.metadata.version("ffsubsync") != "0.5.1":
                    result = {"error": "dependency"}
                else:
                    from ffsubsync import ffsubsync as api
                    ffmpeg_path = restricted_binaries(payload["reference"], Path(payload["output"]).parent)
                    result = synchronize(payload, api, ffmpeg_path=ffmpeg_path)
            except (ImportError, importlib.metadata.PackageNotFoundError):
                result = {"error": "dependency"}
            except FileNotFoundError:
                result = {"error": "media_dependency"}
    except (Exception, SystemExit):
        result = {"error": "execution"}
    finally:
        if watchdog is not None:
            try:
                os.kill(watchdog, signal.SIGTERM)
                os.waitpid(watchdog, 0)
            except (ProcessLookupError, ChildProcessError):
                pass
    print(json.dumps(result, allow_nan=False), flush=True)
    return 1 if "error" in result else 0


if __name__ == "__main__":
    sys.exit(main())
