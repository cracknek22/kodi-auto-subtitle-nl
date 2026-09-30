"""Bounded ffsubsync adapter: preserve every source character except timecodes.

Only an opaque loopback URL from our media proxy may be passed as a remote
reference. Signed URLs and authentication headers must stay in that proxy.
The optional local reference is for administrator-provided offline fixtures.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit


MAX_SUBTITLE_BYTES = 2 * 1024 * 1024
MAX_OFFSET_SECONDS = 30.0
MAX_SCALE_DEVIATION = 0.1
_TIME = r"\d{2,}:[0-5]\d:[0-5]\d[,.]\d{3}"
_TIMING = re.compile(rf"(?P<start>{_TIME})[ \t]+-->[ \t]+(?P<end>{_TIME})(?:[ \t]+[^\r\n]*)?")


class SynchronizationError(RuntimeError):
    """Safe user-facing error; never includes ffmpeg diagnostics or media URLs."""


@dataclass(frozen=True)
class SyncResult:
    content: str
    offset_seconds: float
    scale_factor: float


@dataclass(frozen=True)
class _Cue:
    text: str
    start: str
    end: str
    start_span: tuple[int, int]
    end_span: tuple[int, int]


def _seconds(timestamp: str) -> float:
    hours, minutes, seconds = timestamp.replace(",", ".").split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _cues(content: str) -> list[_Cue]:
    """Read standard SRT blocks, retaining absolute spans into the original."""
    cues = []
    lines = content.splitlines(keepends=True)
    block: list[tuple[int, str]] = []
    offset = 0

    def finish():
        if not block:
            return
        first = block[0][1].rstrip("\r\n").lstrip("\ufeff")
        timing_index = 1 if first.isdigit() else 0
        if timing_index >= len(block):
            raise SynchronizationError("Geen geldige SRT voor synchronisatie.")
        line_offset, line = block[timing_index]
        timing_line = line.rstrip("\r\n")
        if timing_line.startswith("\ufeff"):
            timing_line = timing_line[1:]
            line_offset += 1
        match = _TIMING.fullmatch(timing_line)
        text = "".join(value for _, value in block[timing_index + 1:]).rstrip("\r\n")
        if not match or not text or _seconds(match["end"]) <= _seconds(match["start"]):
            raise SynchronizationError("Geen geldige SRT voor synchronisatie.")
        cues.append(_Cue(
            text.replace("\r\n", "\n"), match["start"], match["end"],
            tuple(line_offset + value for value in match.span("start")),
            tuple(line_offset + value for value in match.span("end")),
        ))

    for line in lines:
        if not line.strip():
            finish()
            block = []
        else:
            block.append((offset, line))
        offset += len(line)
    finish()
    if not cues:
        raise SynchronizationError("Geen geldige SRT voor synchronisatie.")
    return cues


def _read_subtitle(path: Path) -> str:
    try:
        if path.is_symlink() or not path.is_file():
            raise OSError()
        with path.open("rb") as handle:
            data = handle.read(MAX_SUBTITLE_BYTES + 1)
        if not data or len(data) > MAX_SUBTITLE_BYTES:
            raise OSError()
        encodings = ("utf-16",) if data.startswith((b"\xff\xfe", b"\xfe\xff")) else ("utf-8", "cp1252")
        for encoding in encodings:
            try:
                return data.decode(encoding)
            except UnicodeError:
                continue
    except OSError:
        pass
    raise SynchronizationError("Ondertitel ontbreekt, is te groot of is niet leesbaar.")


def _validated_reference(reference: str) -> str:
    try:
        if not isinstance(reference, str) or not reference or len(reference) > 4096:
            raise ValueError()
        if any(ord(char) < 32 for char in reference):
            raise ValueError()
        parsed = urlsplit(reference)
        if parsed.scheme:
            if not (parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
                    and parsed.port and parsed.username is None and parsed.password is None
                    and not parsed.query and not parsed.fragment and parsed.path.startswith("/")):
                raise ValueError()
        elif not Path(reference).is_absolute() or not Path(reference).is_file():
            raise ValueError()
        return reference
    except (ValueError, OSError):
        raise SynchronizationError("Ongeldige afgeschermde mediareferentie.") from None


def _retime(original: str, corrected: str, offset: float, scale: float) -> str:
    source_cues, synced_cues = _cues(original), _cues(corrected)
    if len(source_cues) != len(synced_cues):
        raise SynchronizationError("Synchronisatie veranderde het aantal ondertitelregels.")
    edits = []
    for source, synced in zip(source_cues, synced_cues):
        if source.text != synced.text:
            raise SynchronizationError("Synchronisatie veranderde de tekst of volgorde.")
        for name in ("start", "end"):
            old_time, new_time = getattr(source, name), getattr(synced, name)
            expected = _seconds(old_time) * scale + offset
            if abs(_seconds(new_time) - expected) > 0.005:
                raise SynchronizationError("Synchronisatie gaf inconsistente tijdcodes.")
            if "." in old_time:
                new_time = new_time.replace(",", ".")
            edits.append((*getattr(source, name + "_span"), new_time))
    for start, end, value in reversed(edits):
        original = original[:start] + value + original[end:]
    return original


class SubtitleSynchronizer:
    def __init__(self, timeout=300, python_binary=sys.executable, runner=subprocess.run):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Synchronisatietime-out moet positief en eindig zijn.")
        self.timeout = timeout
        self.python_binary = python_binary
        self.runner = runner

    def synchronize(self, source: Path, reference: str) -> SyncResult:
        reference = _validated_reference(reference)
        original = _read_subtitle(Path(source))
        _cues(original)
        worker = Path(__file__).with_name("ffsubsync_worker.py")
        with tempfile.TemporaryDirectory(prefix="subtitle-sync-") as directory:
            private = Path(directory)
            staged_source = private / "source.srt"
            output = private / "corrected.srt"
            staged_source.write_bytes(original.encode("utf-8"))
            staged_source.chmod(0o600)
            payload = {"source": str(staged_source), "reference": reference,
                       "output": str(output), "timeout": self.timeout * 0.95}
            try:
                result = self.runner(
                    [str(self.python_binary), "-I", str(worker)],
                    input=json.dumps(payload), text=True, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, timeout=self.timeout,
                    start_new_session=True, check=False,
                )
            except subprocess.TimeoutExpired:
                raise SynchronizationError("Synchronisatie duurde te lang; er is niets gewijzigd.") from None
            except (OSError, subprocess.SubprocessError):
                raise SynchronizationError("Synchronisatie kon niet worden gestart.") from None
            try:
                if not isinstance(result.stdout, str) or len(result.stdout) > 4096:
                    raise ValueError()
                metadata = json.loads(result.stdout)
                if not isinstance(metadata, dict):
                    raise ValueError()
                if metadata.get("error") == "dependency":
                    raise SynchronizationError("ffsubsync 0.5.1 is nog niet geïnstalleerd op de server.")
                if metadata.get("error") == "media_dependency":
                    raise SynchronizationError("FFmpeg en ffprobe moeten op de server worden geïnstalleerd.")
                if metadata.get("error") == "timeout":
                    raise SynchronizationError("Synchronisatie duurde te lang; er is niets gewijzigd.")
                if result.returncode != 0 or metadata.get("error"):
                    raise ValueError()
                if metadata.get("sync_was_successful") is not True:
                    raise SynchronizationError("Geen betrouwbare synchronisatie gevonden; er is niets gewijzigd.")
                offset = metadata["offset_seconds"]
                scale = metadata["framerate_scale_factor"]
                if (any(isinstance(value, bool) or not isinstance(value, (int, float))
                        or not math.isfinite(value) for value in (offset, scale))
                        or abs(offset) > MAX_OFFSET_SECONDS or abs(scale - 1) > MAX_SCALE_DEVIATION):
                    raise ValueError()
                content = _retime(original, _read_subtitle(output), offset, scale)
                return SyncResult(content, float(offset), float(scale))
            except (ValueError, TypeError, KeyError):
                raise SynchronizationError("Synchronisatie leverde geen geldig resultaat op.") from None
