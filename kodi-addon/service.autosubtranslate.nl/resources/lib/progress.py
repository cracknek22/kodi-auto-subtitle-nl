"""Safe, non-blocking presentation of subtitle translation progress."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol


HEADING = "Ondertitels vertalen"


class ProgressDisplay(Protocol):
    def create(self, heading: str, message: str) -> None: ...

    def update(self, percent: int, heading: str, message: str) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class _Frame:
    job_id: str
    percent: int
    message: str


class ProgressPresenter:
    """Select one pending job and render only fixed, validated UI text."""

    def __init__(
        self,
        display_factory: Callable[[], ProgressDisplay],
        *,
        on_error: Callable[[], None] | None = None,
    ) -> None:
        self._display_factory = display_factory
        self._on_error = on_error
        self._display: ProgressDisplay | None = None
        self._last_frame: _Frame | None = None

    def refresh(
        self,
        jobs: Mapping[str, object],
        statuses: Mapping[str, object],
        active_video_fingerprint: str,
    ) -> None:
        candidates = self._pending_jobs(jobs, statuses)
        if not candidates:
            self.close()
            return

        job_id, _job = next(
            (
                item
                for item in candidates
                if active_video_fingerprint
                and item[1].get("video_fingerprint")
                == active_video_fingerprint
            ),
            candidates[0],
        )
        status = statuses.get(job_id)
        frame = self._frame_for(job_id, status, len(candidates))
        if frame == self._last_frame:
            return
        self._render(frame)

    def close(self) -> None:
        display = self._display
        self._display = None
        self._last_frame = None
        if display is None:
            return
        try:
            display.close()
        except Exception:
            self._report_error()

    @staticmethod
    def _pending_jobs(
        jobs: Mapping[str, object],
        statuses: Mapping[str, object],
    ) -> list[tuple[str, Mapping[str, object]]]:
        pending: list[tuple[str, Mapping[str, object]]] = []
        for job_id, job in jobs.items():
            if not isinstance(job_id, str) or not isinstance(job, Mapping):
                continue
            status = statuses.get(job_id)
            if (
                isinstance(status, Mapping)
                and status.get("job_id") == job_id
                and status.get("state") == "failed"
            ):
                continue
            pending.append((job_id, job))
        return pending

    @staticmethod
    def _frame_for(job_id: str, status: object, count: int) -> _Frame:
        percent = 0
        message = "Wachten op bevestigde serverstatus…"
        if isinstance(status, Mapping) and status.get("job_id") == job_id:
            state = status.get("state")
            if state == "syncing":
                message = "Synchroniseren…"
            elif state == "processing":
                reported = status.get("progress")
                if type(reported) is int and 0 <= reported <= 100:
                    percent = reported
                    message = f"Vertalen… {reported}%"
                else:
                    message = "Vertaling gestart; voortgang nog niet bevestigd…"
            elif state == "complete":
                percent = 100
                message = "Vertaling gereed; ophalen…"

        if count > 1:
            message = f"{message} · {count} actief"
        return _Frame(job_id=job_id, percent=percent, message=message)

    def _render(self, frame: _Frame) -> None:
        display = self._display
        try:
            if display is None:
                display = self._display_factory()
                self._display = display
                display.create(HEADING, frame.message)
            display.update(frame.percent, HEADING, frame.message)
        except Exception:
            self._display = None
            self._last_frame = None
            if display is not None:
                try:
                    display.close()
                except Exception:
                    pass
            self._report_error()
            return
        self._last_frame = frame

    def _report_error(self) -> None:
        if self._on_error is None:
            return
        try:
            self._on_error()
        except Exception:
            pass
