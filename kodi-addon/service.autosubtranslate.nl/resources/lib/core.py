from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Mapping


REQUEST_SUFFIX = ".translate.request.json"
STATUS_SUFFIX = ".translate.status.json"
TAG_RE = re.compile(r"<[^<>\n]{1,200}>|\{\\[^{}\n]{1,200}\}")
DUTCH_RE = re.compile(
    r"(^|[.\s_(\[\-])"
    r"(nl(?:[-_](?:nl|be))?|nld|dut|dutch|nederlands)"
    r"(?=$|[.\s_)\]\-])"
)


def is_dutch_subtitle(name: str) -> bool:
    return bool(DUTCH_RE.search(name.casefold()))


def is_source_subtitle(path: str) -> bool:
    lowered = path.casefold()
    return lowered.endswith(".srt") and not is_dutch_subtitle(
        lowered.rsplit("/", 1)[-1]
    )


def select_stable_candidate(
    baseline: Mapping[str, tuple[int, int]],
    previous: Mapping[str, tuple[int, int]],
    current: Mapping[str, tuple[int, int]],
) -> str | None:
    """Return one new/changed SRT only after its size and timestamp are stable."""
    candidates = stable_candidates(baseline, previous, current)
    return candidates[0] if len(candidates) == 1 else None


def stable_candidates(
    baseline: Mapping[str, tuple[int, int]],
    previous: Mapping[str, tuple[int, int]],
    current: Mapping[str, tuple[int, int]],
) -> list[str]:
    """List new/changed SRTs whose size and timestamp stopped changing."""
    return sorted(
        path
        for path, signature in current.items()
        if is_source_subtitle(path)
        and previous.get(path) == signature
        and baseline.get(path) != signature
    )


def _basename(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1]


def translated_name(source_path: str) -> str:
    source_name = _basename(source_path)
    if not source_name.casefold().endswith(".srt"):
        raise ValueError("geen geldige SRT")

    stem = source_name[:-4]
    lowered = stem.casefold()
    for english_suffix in (".en", ".eng"):
        if lowered.endswith(english_suffix):
            stem = stem[: -len(english_suffix)]
            break
    return f"{stem}.nl.srt"


def request_path(source_path: str) -> str:
    return f"{source_path}{REQUEST_SUFFIX}"


def status_path(source_path: str) -> str:
    return f"{source_path}{STATUS_SUFFIX}"


def build_request(source_path: str, job_id: str) -> dict:
    source_name = _basename(source_path)
    if not is_source_subtitle(source_name):
        raise ValueError("geen geldige Engelse SRT")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", job_id):
        raise ValueError("ongeldig opdracht-ID")

    return {
        "version": 1,
        "job_id": job_id,
        "source": source_name,
        "requested_at": datetime.now(timezone.utc).isoformat(),
    }


def video_fingerprint(video: str) -> str:
    if not video:
        return ""
    return hashlib.sha256(video.encode("utf-8", errors="strict")).hexdigest()


def validate_completed_status(source_path: str, status: Mapping[str, object]) -> str:
    source_name = _basename(source_path)
    expected_output = translated_name(source_path)
    if status.get("version") != 1 or status.get("state") != "complete":
        raise ValueError("ongeldige gereedmelding van de Radxa")
    if status.get("source") != source_name:
        raise ValueError("ongeldige bron in gereedmelding")
    if status.get("output") != expected_output:
        raise ValueError("ongeldige uitvoernaam van de Radxa")
    return expected_output


def first_dialogue(content: str, max_length: int = 120) -> str:
    normalized = content[1:] if content.startswith("\ufeff") else content
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    for block in re.split(r"\n{2,}", normalized):
        lines = [line for line in block.split("\n") if line.strip()]
        timestamp_index = next(
            (index for index, line in enumerate(lines) if "-->" in line),
            None,
        )
        if timestamp_index is None or timestamp_index + 1 >= len(lines):
            continue
        dialogue = " ".join(lines[timestamp_index + 1 :])
        dialogue = TAG_RE.sub("", dialogue)
        dialogue = re.sub(r"\s+", " ", dialogue).strip()
        if dialogue:
            if len(dialogue) > max_length:
                return f"{dialogue[: max_length - 1].rstrip()}…"
            return dialogue
    return "(geen voorbeeldtekst gevonden)"
