import io
import json
import os
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from subtitle_sync import SubtitleSynchronizer, SynchronizationError


ORIGINAL = (
    "\ufeff7\r\n00:00:05,000  -->  00:00:07,000 X1:2\r\n"
    "<i>Hello!</i>\r\nStill here.\r\n\r\n"
    "19\r\n00:00:10,000 --> 00:00:12,000\r\nBye.\r\n"
)
CORRECTED = (
    "1\n00:00:07,000 --> 00:00:09,000\n<i>Hello!</i>\nStill here.\n\n"
    "2\n00:00:12,000 --> 00:00:14,000\nBye.\n\n"
)


class SubtitleSynchronizerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / "Original.en.srt"
        self.source.write_bytes(ORIGINAL.encode("utf-8"))
        self.reference = "http://127.0.0.1:45678/opaque-reference"
        self.seen = []

    def runner(self, output=CORRECTED, result=None, returncode=0):
        if result is None:
            result = {"sync_was_successful": True, "offset_seconds": 2.0,
                      "framerate_scale_factor": 1.0}

        def invoke(args, **kwargs):
            payload = json.loads(kwargs["input"])
            self.seen.append((args, kwargs, payload))
            if output is not None:
                Path(payload["output"]).write_bytes(output.encode("utf-8"))
            return SimpleNamespace(returncode=returncode, stdout=json.dumps(result))
        return invoke

    def test_only_timestamps_change_and_private_files_are_removed(self):
        result = SubtitleSynchronizer(runner=self.runner()).synchronize(self.source, self.reference)
        expected = ORIGINAL.replace("00:00:05,000", "00:00:07,000").replace(
            "00:00:07,000 X1", "00:00:09,000 X1").replace(
            "00:00:10,000", "00:00:12,000").replace(
            "00:00:12,000\r", "00:00:14,000\r")
        self.assertEqual(result.content, expected)
        self.assertEqual(result.offset_seconds, 2.0)
        self.assertEqual(result.scale_factor, 1.0)
        self.assertEqual(self.source.read_bytes(), ORIGINAL.encode("utf-8"))
        self.assertFalse(Path(self.seen[0][2]["output"]).parent.exists())

    def test_reference_and_paths_travel_only_via_stdin(self):
        SubtitleSynchronizer(timeout=21, python_binary="/safe/python", runner=self.runner()).synchronize(
            self.source, self.reference)
        args, kwargs, payload = self.seen[0]
        self.assertEqual(args[0], "/safe/python")
        self.assertNotIn(self.reference, " ".join(args))
        self.assertNotIn(str(self.source), " ".join(args))
        self.assertFalse(kwargs.get("shell", False))
        self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(kwargs["timeout"], 21)
        self.assertEqual(payload["reference"], self.reference)

    def test_rejects_quality_failure_even_when_worker_exits_zero(self):
        with self.assertRaisesRegex(SynchronizationError, "betrouwbare"):
            SubtitleSynchronizer(runner=self.runner(result={"sync_was_successful": False})).synchronize(
                self.source, self.reference)

    def test_rejects_modified_missing_or_reordered_cues(self):
        for output in (CORRECTED.replace("Bye.", "Changed."),
                       CORRECTED.split("\n\n")[0],
                       CORRECTED.replace("Bye.", "<i>Hello!</i>\nStill here."),
                       CORRECTED.replace("00:00:14,000", "00:00:13,000")):
            with self.subTest(output=output), self.assertRaises(SynchronizationError):
                SubtitleSynchronizer(runner=self.runner(output=output)).synchronize(self.source, self.reference)

    def test_rejects_nonfinite_or_unsafe_correction_metadata(self):
        for offset, scale in ((float("nan"), 1), (0, float("inf")), (0, 0), (0, -1),
                              (True, 1), (5000, 1), (0, 5)):
            with self.subTest(offset=offset, scale=scale), self.assertRaises(SynchronizationError):
                SubtitleSynchronizer(runner=self.runner(result={
                    "sync_was_successful": True, "offset_seconds": offset,
                    "framerate_scale_factor": scale})).synchronize(self.source, self.reference)

    def test_errors_do_not_leak_paths_or_worker_diagnostics(self):
        for failure in (subprocess.TimeoutExpired(["secret"], 1, stderr="private-url"),
                        OSError("private-url")):
            def runner(*_args, **_kwargs):
                raise failure
            with self.subTest(failure=failure), self.assertRaises(SynchronizationError) as caught:
                SubtitleSynchronizer(runner=runner).synchronize(self.source, self.reference)
            self.assertNotIn("private-url", str(caught.exception))
            self.assertNotIn(str(self.source), str(caught.exception))

    def test_invalid_worker_or_missing_output_is_rejected(self):
        for output, metadata, code in ((None, {}, 1), (None, {"sync_was_successful": True}, 0),
                                        (None, {"error": "private-url"}, 1)):
            with self.subTest(metadata=metadata), self.assertRaises(SynchronizationError):
                SubtitleSynchronizer(runner=self.runner(output, metadata, code)).synchronize(
                    self.source, self.reference)

    def test_rejects_remote_reference_bypassing_loopback_proxy(self):
        for reference in ("https://cdn.example/video?token=secret", "file:///etc/passwd",
                          "http://localhost:1234/a", "http://127.0.0.1:1234/a?secret=x",
                          "http://user:password@127.0.0.1:1234/a", "udp://127.0.0.1/a"):
            with self.subTest(reference=reference), self.assertRaises(SynchronizationError):
                SubtitleSynchronizer(runner=self.runner()).synchronize(self.source, reference)
        self.assertEqual(self.seen, [])

    def test_supports_explicit_local_media_fixture(self):
        media = Path(self.temp.name) / "synthetic.wav"
        media.write_bytes(b"test fixture")
        result = SubtitleSynchronizer(runner=self.runner()).synchronize(self.source, str(media))
        self.assertEqual(result.offset_seconds, 2.0)

    def test_source_must_be_bounded_valid_srt(self):
        for data in (b"plain text", b"x" * (2 * 1024 * 1024 + 1), b"\xff\xfe"):
            self.source.write_bytes(data)
            with self.subTest(size=len(data)), self.assertRaises(SynchronizationError):
                SubtitleSynchronizer(runner=self.runner()).synchronize(self.source, self.reference)
        self.assertEqual(self.seen, [])

    def test_rejects_missing_symlink_and_invalid_timing_source(self):
        self.source.unlink()
        with self.assertRaises(SynchronizationError):
            SubtitleSynchronizer(runner=self.runner()).synchronize(self.source, self.reference)
        target = self.source.with_name("target.srt")
        target.write_bytes(ORIGINAL.encode("utf-8"))
        self.source.symlink_to(target)
        with self.assertRaises(SynchronizationError):
            SubtitleSynchronizer(runner=self.runner()).synchronize(self.source, self.reference)
        self.source.unlink()
        for content in ("1\n", "1\n00:00:04,000 --> 00:00:03,000\nHello\n",
                        "1\n00:00:04,000 --> 00:00:05,000\n"):
            self.source.write_text(content)
            with self.assertRaises(SynchronizationError):
                SubtitleSynchronizer(runner=self.runner()).synchronize(self.source, self.reference)

    def test_corrects_dot_timecodes_and_unindexed_source(self):
        source = "\ufeff00:00:05.000 --> 00:00:07.000\r\nHello\r\n"
        corrected = "1\n00:00:07,000 --> 00:00:09,000\nHello\n"
        self.source.write_bytes(source.encode("utf-8"))
        result = SubtitleSynchronizer(runner=self.runner(output=corrected)).synchronize(self.source, self.reference)
        self.assertEqual(result.content, "\ufeff00:00:07.000 --> 00:00:09.000\r\nHello\r\n")

    def test_decodes_cp1252_without_changing_source_bytes(self):
        source = "1\r\n00:00:05,000 --> 00:00:07,000\r\nCaf\xe9\r\n"
        corrected = "1\n00:00:07,000 --> 00:00:09,000\nCaf\xe9\n"
        self.source.write_bytes(source.encode("cp1252"))
        result = SubtitleSynchronizer(runner=self.runner(output=corrected)).synchronize(self.source, self.reference)
        self.assertIn("Caf\xe9\r\n", result.content)
        self.assertEqual(self.source.read_bytes(), source.encode("cp1252"))

    def test_rejects_malformed_worker_metadata_and_config(self):
        for stdout in ("x" * 4097, "not json", "[]", "null"):
            with self.subTest(stdout=stdout[:20]), self.assertRaises(SynchronizationError):
                SubtitleSynchronizer(runner=lambda *a, **k: SimpleNamespace(
                    returncode=0, stdout=stdout)).synchronize(self.source, self.reference)
        for timeout in (-1, 0, True, "3", float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                SubtitleSynchronizer(timeout=timeout)

    def test_missing_dependency_and_worker_timeout_have_safe_messages(self):
        for code, message in (("dependency", "geïnstalleerd"), ("timeout", "duurde te lang")):
            with self.subTest(code=code), self.assertRaisesRegex(SynchronizationError, message):
                SubtitleSynchronizer(runner=self.runner(result={"error": code}, returncode=1)).synchronize(
                    self.source, self.reference)


class WorkerTests(unittest.TestCase):
    def test_wrapper_forwards_literal_arguments_and_refuses_whitelist_overrides(self):
        import ffsubsync_worker
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "paths with spaces and 'quotes'"
            root.mkdir()
            fake = root / "fake media tool"
            fake.write_text("#!" + sys.executable + "\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n")
            fake.chmod(0o700)
            with patch.object(ffsubsync_worker.shutil, "which", return_value=str(fake)):
                wrappers = ffsubsync_worker.restricted_binaries("http://127.0.0.1:1234/reference.mkv", root)
            args = [str(Path(wrappers) / "ffmpeg"), "-i", "literal $(not-a-command) \"quote\""]
            result = subprocess.run(args, check=True, text=True, capture_output=True, timeout=5)
            received = json.loads(result.stdout)
            self.assertEqual(received[:2], ["-protocol_whitelist", "tcp,http"])
            self.assertEqual(received[-2:], args[-2:])
            result = subprocess.run([args[0], "-protocol_whitelist", "file"],
                                    text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 64)
            self.assertEqual(result.stdout, "")

    def test_media_wrappers_reject_playlists_and_hide_real_reference(self):
        import ffsubsync_worker
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(ffsubsync_worker.shutil, "which", side_effect=lambda name: "/usr/bin/" + name):
                wrappers = ffsubsync_worker.restricted_binaries("http://127.0.0.1:1234/opaque/reference.mkv", Path(temporary))
            self.assertEqual(Path(wrappers).parent, Path(temporary))
            for name in ("ffmpeg", "ffprobe"):
                wrapper = Path(wrappers) / name
                self.assertTrue(wrapper.stat().st_mode & 0o100)
                script = (Path(wrappers) / (name + ".py")).read_text()
                self.assertIn("-format_whitelist", script)
                self.assertIn("tcp,http", script)
                self.assertNotIn("file,tcp,http", script)
                self.assertNotIn("opaque", script)
                self.assertNotIn("hls", script)
                self.assertNotIn("concat", script)
                self.assertNotIn("dash", script)

    def test_local_fixture_protocol_allowance_and_missing_media_dependency(self):
        import ffsubsync_worker
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(ffsubsync_worker.shutil, "which", return_value="/usr/bin/ffmpeg"):
                wrappers = ffsubsync_worker.restricted_binaries("/safe/fixture.wav", Path(temporary))
            self.assertIn("file,tcp,http", (Path(wrappers) / "ffmpeg.py").read_text())
            with patch.object(ffsubsync_worker.shutil, "which", return_value=None):
                self.assertIsNone(ffsubsync_worker.restricted_binaries("/safe/fixture.npz", Path(temporary)))
                with self.assertRaises(FileNotFoundError):
                    ffsubsync_worker.restricted_binaries("http://127.0.0.1:1234/reference.mkv", Path(temporary))

    def test_worker_passes_only_restricted_tool_directory_to_library(self):
        import ffsubsync_worker
        seen = []
        api = SimpleNamespace(make_parser=lambda: SimpleNamespace(parse_args=lambda values: seen.extend(values)),
                              run=lambda _: {"retval": 0, "sync_was_successful": True})
        ffsubsync_worker.synchronize({"source": "/safe/source.srt", "output": "/safe/output.srt",
                                      "reference": "http://127.0.0.1:1234/reference.mkv"}, api,
                                     ffmpeg_path="/private/allowed-tools")
        self.assertEqual(seen[seen.index("--ffmpeg-path") + 1], "/private/allowed-tools")

    def test_worker_requests_lightweight_sampling_and_checks_quality(self):
        import ffsubsync_worker
        parsed = SimpleNamespace()
        parser = SimpleNamespace(parse_args=lambda values: self.assert_worker_args(values) or parsed)
        api = SimpleNamespace(make_parser=lambda: parser, run=lambda args: {
            "retval": 0, "sync_was_successful": False, "offset_seconds": None,
            "framerate_scale_factor": None})
        result = ffsubsync_worker.synchronize({"source": "/safe/source.srt",
            "output": "/safe/result.srt", "reference": "http://127.0.0.1:1234/media"}, api)
        self.assertFalse(result["sync_was_successful"])

    def test_main_hides_dependency_and_runtime_diagnostics(self):
        import ffsubsync_worker
        payload = json.dumps({"timeout": 5})
        for version in ("other-version", ffsubsync_worker.importlib.metadata.PackageNotFoundError("secret")):
            output = io.StringIO()
            with patch.object(ffsubsync_worker.sys, "stdin", io.StringIO(payload)), \
                    patch.object(ffsubsync_worker.sys, "stdout", output), \
                    patch.object(ffsubsync_worker, "_start_watchdog", return_value=999), \
                    patch.object(ffsubsync_worker.os, "kill"), \
                    patch.object(ffsubsync_worker.os, "waitpid"), \
                    patch.object(ffsubsync_worker.logging, "disable"), \
                    patch.object(ffsubsync_worker.importlib.metadata, "version",
                        **({"side_effect": version} if isinstance(version, Exception) else {"return_value": version})):
                self.assertEqual(ffsubsync_worker.main(), 1)
            self.assertEqual(json.loads(output.getvalue()), {"error": "dependency"})

    def test_main_rejects_bad_input_without_a_traceback(self):
        import ffsubsync_worker
        for payload in ("invalid", json.dumps({"timeout": float("inf")}), json.dumps({"timeout": "secret"})):
            output = io.StringIO()
            with patch.object(ffsubsync_worker.sys, "stdin", io.StringIO(payload)), \
                    patch.object(ffsubsync_worker.sys, "stdout", output):
                self.assertEqual(ffsubsync_worker.main(), 1)
            self.assertEqual(json.loads(output.getvalue()), {"error": "execution"})

    def test_watchdog_requires_a_separate_process_group(self):
        import ffsubsync_worker
        with patch.object(ffsubsync_worker.os, "getpgrp", return_value=1), \
                patch.object(ffsubsync_worker.os, "getpid", return_value=2):
            with self.assertRaises(ValueError):
                ffsubsync_worker._start_watchdog(1)

    def test_watchdog_parent_returns_child_pid(self):
        import ffsubsync_worker
        with patch.object(ffsubsync_worker.os, "getpgrp", return_value=2), \
                patch.object(ffsubsync_worker.os, "getpid", return_value=2), \
                patch.object(ffsubsync_worker.os, "fork", return_value=123):
            self.assertEqual(ffsubsync_worker._start_watchdog(1), 123)

    def assert_worker_args(self, values):
        self.assertIn("--multi-segment-sync", values)
        self.assertIn("--skip-sync-on-low-quality", values)
        self.assertEqual(values[values.index("--min-score") + 1], "1")
        self.assertEqual(values[values.index("--vad") + 1], "webrtc")
        self.assertEqual(values[values.index("--parallel-workers") + 1], "1")
        self.assertNotIn("--overwrite-input", values)
        self.assertNotIn("--log-dir-path", values)

    def test_worker_does_not_treat_successful_exit_as_reliable_sync(self):
        import ffsubsync_worker
        api = SimpleNamespace(make_parser=lambda: SimpleNamespace(parse_args=lambda _: object()),
                              run=lambda _: {"retval": 0, "offset_seconds": 1.0,
                                             "framerate_scale_factor": 1.0})
        result = ffsubsync_worker.synchronize({"source": "/safe/source.srt",
            "output": "/safe/result.srt", "reference": "http://127.0.0.1:1234/media"}, api)
        self.assertFalse(result["sync_was_successful"])


@unittest.skipUnless(os.environ.get("RUN_FFSUBSYNC_INTEGRATION") == "1",
                     "Optional actual ffsubsync fixture: set RUN_FFSUBSYNC_INTEGRATION=1")
class ActualFfsubsyncTests(unittest.TestCase):
    def test_recovers_known_shift_from_synthetic_speech_activity(self):
        """No real movie, network, translation API, or FFmpeg needed.

        A precomputed synthetic 100 Hz VAD signal exercises ffsubsync's actual
        FFT alignment, framerate choice, subtitle output and the isolated worker.
        It deliberately does not claim to test audio extraction / real speech.
        """
        import numpy as np
        rng = random.Random(49821)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference = root / "synthetic.npz"
            source = root / "synthetic.en.srt"
            speech = np.zeros(30000)
            start = 8.0
            cues = []
            def timestamp(seconds):
                milliseconds = round(seconds * 1000)
                minutes, remainder = divmod(milliseconds, 60000)
                whole, fraction = divmod(remainder, 1000)
                return f"00:{minutes:02d}:{whole:02d},{fraction:03d}"
            for index in range(40):
                duration = rng.uniform(0.7, 3.2)
                end = start + duration
                speech[round(start * 100):round(end * 100)] = 1
                cues.append(f"{100 + index}\r\n{timestamp(start + 3)} --> "
                            f"{timestamp(end + 3)}\r\n<i>Synthetic cue {index}.</i>\r\n")
                start = end + rng.uniform(0.7, 4.1)
            original = "\r\n".join(cues)
            source.write_bytes(original.encode("utf-8"))
            np.savez_compressed(reference, speech=speech)
            result = SubtitleSynchronizer(timeout=30, python_binary=sys.executable).synchronize(
                source, str(reference))
            self.assertAlmostEqual(result.offset_seconds, -3.0, delta=0.05)
            self.assertAlmostEqual(result.scale_factor, 1.0, delta=0.001)
            self.assertIn("100\r\n00:00:08,000", result.content)
            self.assertEqual(source.read_bytes(), original.encode("utf-8"))


if __name__ == "__main__":
    unittest.main()
