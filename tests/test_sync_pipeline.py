import hashlib
import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import subtitle_translator as app


ORIGINAL = "1\n00:00:03,000 --> 00:00:04,500\n<i>Hello.</i>\n"
SYNCED = ORIGINAL.replace("00:00:03,000", "00:00:01,000").replace("00:00:04,500", "00:00:02,500")


class SyncPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "Movie.en.srt"
        self.source.write_bytes(ORIGINAL.encode())
        self.sha = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.job_id = "job_" + "a" * 32
        self.request = self.root / (self.source.name + app.REQUEST_SUFFIX)
        self.status = self.root / (self.source.name + app.STATUS_SUFFIX)
        self.payload = {"version": 2, "job_id": self.job_id, "source": self.source.name,
                        "sync": {"required": True, "source_sha256": self.sha}}
        self.events = []
        self.store = Mock()
        self.store.consume.side_effect = lambda *_args: self.events.append("consume") or object()
        self.engine = Mock()
        self.engine.synchronize.side_effect = self.synchronize
        self.translate = Mock(side_effect=lambda texts: self.events.append("translate") or ["<i>Hallo.</i>"])

    def synchronize(self, path, reference):
        self.events.append("sync")
        self.assertNotEqual(path, self.source)
        self.assertEqual(path.read_bytes(), self.source.read_bytes())
        self.assertEqual(reference, "http://127.0.0.1/opaque/reference.mkv")
        return SimpleNamespace(content=SYNCED, offset_seconds=-2.0, scale_factor=1.0)

    @contextmanager
    def proxy(self, reference):
        self.events.append("proxy_open")
        try:
            yield SimpleNamespace(url="http://127.0.0.1/opaque/reference.mkv")
        finally:
            self.events.append("proxy_close")

    def run_job(self, *, enabled=True):
        self.request.write_text(json.dumps(self.payload))
        scanner = app.ConfirmedJobScanner(
            self.root, self.translate,
            synchronizer=self.engine if enabled else None,
            reference_store=self.store if enabled else None,
            reference_proxy_factory=self.proxy if enabled else None,
        )
        scanner.scan_once()
        scanner.scan_once()
        scanner.scan_once()
        return json.loads(self.status.read_text())

    def test_confirmed_job_syncs_before_translation_then_reports_corrected_output(self):
        status = self.run_job()
        self.assertEqual(status["version"], 2)
        self.assertEqual(status["state"], "complete")
        self.assertEqual(status["sync"], {"state": "complete", "offset_seconds": -2.0, "scale_factor": 1.0})
        self.assertEqual(self.events, ["consume", "proxy_open", "sync", "proxy_close", "translate"])
        self.store.consume.assert_called_once_with(self.job_id, self.source.name, self.sha)
        self.assertEqual(self.source.read_text(), ORIGINAL)
        self.assertEqual((self.root / "Movie.nl.srt").read_text(), SYNCED.replace("Hello.", "Hallo."))

    def test_no_reference_engine_or_changed_source_never_falls_back_to_unsynced_translation(self):
        status = self.run_job(enabled=False)
        self.assertEqual(status["state"], "failed")
        self.translate.assert_not_called()
        self.status.unlink()
        self.source.write_text(ORIGINAL.replace("Hello", "Changed"))
        status = self.run_job()
        self.assertEqual(status["state"], "failed")
        self.store.consume.assert_not_called()
        self.translate.assert_not_called()

    def test_low_quality_failure_never_calls_codex_or_leaks_stream_secret(self):
        self.engine.synchronize.side_effect = RuntimeError("https://media.invalid/?token=private")
        with self.assertLogs(app.LOGGER, level="WARNING") as logs:
            status = self.run_job()
        self.assertEqual(status["state"], "failed")
        self.assertNotIn("private", str(status) + str(logs.output))
        self.translate.assert_not_called()
        self.assertFalse((self.root / "Movie.nl.srt").exists())
        self.assertIn("proxy_close", self.events)

    def test_private_reference_does_not_appear_in_shared_progress_or_result(self):
        self.store.consume.side_effect = None
        self.store.consume.return_value = SimpleNamespace(url="https://media.invalid/private-token")
        statuses = []
        write = app._atomic_write_json
        def record(path, payload):
            statuses.append(payload)
            write(path, payload)
        with patch.object(app, "_atomic_write_json", side_effect=record):
            self.run_job()
        self.assertEqual([s["state"] for s in statuses], ["syncing", "processing", "processing", "complete"])
        self.assertNotIn("private-token", json.dumps(statuses))
        self.assertEqual(statuses[-2]["progress"], 100)

    def test_version_two_cannot_opt_out_or_smuggle_a_url(self):
        for bad_sync in ({"required": False, "source_sha256": self.sha},
                         {"required": True, "source_sha256": "bad"},
                         {"required": True, "source_sha256": self.sha, "url": "secret"}):
            with self.subTest(sync=bad_sync):
                self.payload["sync"] = bad_sync
                status = self.run_job()
                self.assertEqual(status["state"], "failed")
                self.translate.assert_not_called()
                self.status.unlink()

    def test_existing_version_one_requests_still_work_without_sync(self):
        self.payload = {"version": 1, "job_id": self.job_id, "source": self.source.name}
        status = self.run_job(enabled=False)
        self.assertEqual(status["version"], 1)
        self.assertEqual(status["state"], "complete")
        self.assertEqual((self.root / "Movie.nl.srt").read_text(), ORIGINAL.replace("Hello.", "Hallo."))
        self.engine.synchronize.assert_not_called()


class ExistingProgressPreservationTests(unittest.TestCase):
    def test_deployed_batch_progress_remains_supported(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "Movie.en.srt"
            source.write_text(ORIGINAL + "\n2\n00:00:05,000 --> 00:00:06,000\nBye.\n")
            progress = Mock()
            translate = Mock(side_effect=lambda texts: ["NL " + text for text in texts])
            app.process_file(source, translate, batch_size=1, progress=progress)
            self.assertEqual(translate.call_count, 2)
            self.assertEqual(progress.call_args_list[-1].args, (2, 2))
