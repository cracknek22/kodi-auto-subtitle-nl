import math
import sys
import unittest
from pathlib import Path


ADDON_LIB = (
    Path(__file__).resolve().parents[1]
    / "kodi-addon"
    / "service.autosubtranslate.nl"
    / "resources"
    / "lib"
)
sys.path.insert(0, str(ADDON_LIB))

from progress import ProgressPresenter  # noqa: E402


class FakeDisplay:
    def __init__(self, *, fail_on=None):
        self.fail_on = fail_on
        self.events = []

    def create(self, heading, message):
        self.events.append(("create", heading, message))
        if self.fail_on == "create":
            raise RuntimeError("private display detail")

    def update(self, percent, heading, message):
        self.events.append(("update", percent, heading, message))
        if self.fail_on == "update":
            raise RuntimeError("private display detail")

    def close(self):
        self.events.append(("close",))
        if self.fail_on == "close":
            raise RuntimeError("private display detail")


class ProgressPresenterTests(unittest.TestCase):
    @staticmethod
    def job(job_id, fingerprint=""):
        return {
            "job_id": job_id,
            "source": f"smb://secret:password@server/{job_id}.en.srt",
            "video_fingerprint": fingerprint,
        }

    def setUp(self):
        self.displays = []
        self.errors = []

        def display_factory():
            display = FakeDisplay()
            self.displays.append(display)
            return display

        self.presenter = ProgressPresenter(
            display_factory,
            on_error=lambda: self.errors.append("generic"),
        )

    def test_prefers_current_video_and_shows_real_progress_with_job_count(self):
        jobs = {
            "job_first": self.job("job_first", "a" * 64),
            "job_current": self.job("job_current", "b" * 64),
        }
        statuses = {
            "job_first": {
                "job_id": "job_first",
                "state": "processing",
                "progress": 10,
            },
            "job_current": {
                "job_id": "job_current",
                "state": "processing",
                "progress": 42,
            },
        }

        self.presenter.refresh(jobs, statuses, "b" * 64)

        self.assertEqual(len(self.displays), 1)
        self.assertEqual(
            self.displays[0].events,
            [
                ("create", "Ondertitels vertalen", "Vertalen… 42% · 2 actief"),
                (
                    "update",
                    42,
                    "Ondertitels vertalen",
                    "Vertalen… 42% · 2 actief",
                ),
            ],
        )
        rendered = str(self.displays[0].events)
        self.assertNotIn("job_", rendered)
        self.assertNotIn("smb://", rendered)
        self.assertNotIn("password", rendered)

    def test_unchanged_state_does_not_update_the_display(self):
        jobs = {"job_one": self.job("job_one")}
        statuses = {
            "job_one": {
                "job_id": "job_one",
                "state": "processing",
                "progress": 25,
            }
        }

        self.presenter.refresh(jobs, statuses, "")
        self.presenter.refresh(jobs, statuses, "")

        self.assertEqual(len(self.displays[0].events), 2)

    def test_new_or_mismatched_job_resets_old_percent_and_waits_honestly(self):
        self.presenter.refresh(
            {"job_old": self.job("job_old")},
            {
                "job_old": {
                    "job_id": "job_old",
                    "state": "processing",
                    "progress": 73,
                }
            },
            "",
        )

        self.presenter.refresh(
            {"job_new": self.job("job_new")},
            {
                "job_new": {
                    "job_id": "another_job",
                    "state": "processing",
                    "progress": 99,
                    "message": "private server message",
                }
            },
            "",
        )

        self.assertEqual(
            self.displays[0].events[-1],
            (
                "update",
                0,
                "Ondertitels vertalen",
                "Wachten op bevestigde serverstatus…",
            ),
        )
        self.assertNotIn("99", str(self.displays[0].events[-1]))
        self.assertNotIn("private", str(self.displays[0].events[-1]))

    def test_syncing_and_invalid_progress_never_claim_a_percentage(self):
        jobs = {"job_sync": self.job("job_sync")}
        self.presenter.refresh(
            jobs,
            {"job_sync": {"job_id": "job_sync", "state": "syncing"}},
            "",
        )
        self.assertEqual(self.displays[0].events[-1][1], 0)
        self.assertNotIn("%", self.displays[0].events[-1][-1])
        self.assertIn("Synchroniseren", self.displays[0].events[-1][-1])

        invalid_values = [True, "50", 50.0, math.nan, math.inf, -1, 101]
        for index, value in enumerate(invalid_values):
            job_id = f"job_invalid_{index}"
            self.presenter.refresh(
                {job_id: self.job(job_id)},
                {
                    job_id: {
                        "job_id": job_id,
                        "state": "processing",
                        "progress": value,
                    }
                },
                "",
            )
            event = self.displays[0].events[-1]
            self.assertEqual(event[1], 0)
            self.assertNotIn("%", event[-1])
            self.assertIn("nog niet bevestigd", event[-1])

    def test_complete_stays_visible_during_load_retry(self):
        jobs = {
            "job_one": {
                **self.job("job_one"),
                "load_attempts": 2,
                "load_retry_at": 123,
            }
        }
        statuses = {
            "job_one": {
                "job_id": "job_one",
                "state": "complete",
                "message": "do not render this server message",
            }
        }

        self.presenter.refresh(jobs, statuses, "")

        self.assertEqual(
            self.displays[0].events[-1],
            (
                "update",
                100,
                "Ondertitels vertalen",
                "Vertaling gereed; ophalen…",
            ),
        )
        self.assertNotIn("server message", str(self.displays[0].events))

    def test_failed_jobs_are_skipped_and_no_pending_job_closes(self):
        jobs = {
            "job_failed": self.job("job_failed", "a" * 64),
            "job_waiting": self.job("job_waiting", "b" * 64),
        }
        statuses = {
            "job_failed": {
                "job_id": "job_failed",
                "state": "failed",
                "message": "private backend failure",
            },
            "job_waiting": {
                "job_id": "job_waiting",
                "state": "processing",
                "progress": 5,
            },
        }

        self.presenter.refresh(jobs, statuses, "a" * 64)
        self.assertEqual(self.displays[0].events[-1][1], 5)
        self.assertNotIn("private", str(self.displays[0].events))

        self.presenter.refresh(
            {"job_failed": jobs["job_failed"]},
            {"job_failed": statuses["job_failed"]},
            "a" * 64,
        )
        self.assertEqual(self.displays[0].events[-1], ("close",))

        self.presenter.refresh({}, {}, "")
        self.assertEqual(self.displays[0].events.count(("close",)), 1)

    def test_display_failures_are_swallowed_and_retried_without_details(self):
        failing = FakeDisplay(fail_on="update")
        healthy = FakeDisplay()
        displays = iter([failing, healthy])
        presenter = ProgressPresenter(
            lambda: next(displays),
            on_error=lambda: self.errors.append("generic"),
        )
        jobs = {"job_one": self.job("job_one")}
        statuses = {
            "job_one": {
                "job_id": "job_one",
                "state": "processing",
                "progress": 20,
            }
        }

        presenter.refresh(jobs, statuses, "")
        presenter.refresh(jobs, statuses, "")

        self.assertEqual(self.errors, ["generic"])
        self.assertEqual(healthy.events[-1][1], 20)
        self.assertNotIn("private display detail", str(self.errors))

    def test_close_is_idempotent_even_when_the_view_raises(self):
        display = FakeDisplay(fail_on="close")
        presenter = ProgressPresenter(
            lambda: display,
            on_error=lambda: self.errors.append("generic"),
        )
        presenter.refresh(
            {"job_one": self.job("job_one")},
            {"job_one": {"job_id": "job_one", "state": "syncing"}},
            "",
        )

        presenter.close()
        presenter.close()

        self.assertEqual(display.events.count(("close",)), 1)
        self.assertEqual(self.errors, ["generic"])


if __name__ == "__main__":
    unittest.main()
