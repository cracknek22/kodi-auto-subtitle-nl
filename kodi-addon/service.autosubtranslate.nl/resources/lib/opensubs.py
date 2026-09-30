"""Use Kodi's installed OpenSubtitles provider without reading its credentials."""

from __future__ import annotations

import json
import os
import re
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit


PROVIDER_ID = "service.subtitles.opensubtitles-com"
PROVIDER_URL = f"plugin://{PROVIDER_ID}/"
_ENGLISH = {"english", "en", "eng"}
_DIRECTORY_ERROR = "OpenSubtitles kon de ondertitels niet ophalen."


def _url(parameters: dict[str, str]) -> str:
    return PROVIDER_URL + "?" + urlencode(
        {**parameters, "autosub_request": uuid.uuid4().hex}
    )


def search_url() -> str:
    """Search only English, using a new Kodi directory-cache key each time."""
    return _url(
        {
            "action": "search",
            "languages": "English",
            "preferredlanguage": "English",
        }
    )


def _parameters(url: str) -> dict[str, str] | None:
    if not isinstance(url, str) or any(ord(char) < 32 or ord(char) == 127 for char in url):
        return None
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "plugin"
            or parsed.netloc != PROVIDER_ID
            or parsed.path not in {"", "/"}
            or parsed.fragment
        ):
            return None
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
        params = dict(pairs)
        if len(params) != len(pairs):
            return None
        return params
    except (TypeError, ValueError):
        return None


def _is_download(params: dict[str, str]) -> bool:
    return (
        params.get("action") == "download"
        and re.fullmatch(r"[1-9][0-9]{0,31}", params.get("id", "")) is not None
        and params.get("language", "").casefold() in _ENGLISH
    )


def download_url(entries: list[dict]) -> str | None:
    """Select the first English result in the provider's own matching order."""
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("filetype", "file") != "file":
            continue
        params = _parameters(entry.get("file"))
        if params is not None and _is_download(params):
            # The provider accepts English labels; its download action uses this
            # only for the local filename. Normalize to an ISO language marker.
            return _url({"action": "download", "id": params["id"], "language": "en"})
    return None


def _valid_request(url: str) -> bool:
    params = _parameters(url)
    if params is None:
        return False
    nonce = params.get("autosub_request", "")
    if not re.fullmatch(r"[a-f0-9]{32}", nonce):
        return False
    if params.get("action") == "search":
        return (
            set(params) == {"action", "languages", "preferredlanguage", "autosub_request"}
            and params["languages"] == "English"
            and params["preferredlanguage"] == "English"
        )
    return set(params) == {"action", "id", "language", "autosub_request"} and _is_download(params)


def directory_files(execute_json_rpc, url: str) -> list[dict]:
    """Execute a provider directory request; never propagate provider errors."""
    if not _valid_request(url):
        raise ValueError(_DIRECTORY_ERROR)
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "Files.GetDirectory",
        "params": {"directory": url, "media": "files", "sort": {"method": "none"}},
    }
    try:
        payload = json.loads(execute_json_rpc(json.dumps(request)))
        if not isinstance(payload, dict) or "error" in payload:
            raise ValueError
        result = payload.get("result")
        if not isinstance(result, dict):
            raise ValueError
        entries = result.get("files")
        if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
            raise ValueError
    except Exception:
        # Kodi/provider exceptions can include login tokens or playback URLs.
        raise ValueError(_DIRECTORY_ERROR) from None
    return entries


def _local_path(path, translate_path) -> str | None:
    if not isinstance(path, str) or not path:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in path):
        return None
    if ".." in path.replace("\\", "/").split("/") or "%" in path:
        return None
    if "://" in path and not path.startswith("special://"):
        return None
    translated = translate_path(path)
    if not isinstance(translated, str) or not os.path.isabs(translated) or "://" in translated:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in translated):
        return None
    if ".." in translated.replace("\\", "/").split("/"):
        return None
    return os.path.abspath(translated)


def subtitle_path(entries: list[dict], provider_temp: str, translate_path) -> str | None:
    """Return an existing SRT confined to the provider's local temporary folder.

    Both lexical and resolved paths must remain in that folder, rejecting sibling
    prefixes, traversal and symlink escapes. The caller still checks file size
    when staging, and rechecks playback identity before asking for consent.
    """
    if not isinstance(entries, list):
        return None
    try:
        root = _local_path(provider_temp, translate_path)
        if root is None or not os.path.isdir(root):
            return None
        resolved_root = os.path.realpath(root)
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("filetype", "file") != "file":
            continue
        try:
            candidate = _local_path(entry.get("file"), translate_path)
            if candidate is None or not candidate.casefold().endswith(".srt"):
                continue
            if os.path.commonpath([root, candidate]) != root:
                continue
            resolved = os.path.realpath(candidate)
            if (
                os.path.commonpath([resolved_root, resolved]) == resolved_root
                and resolved.casefold().endswith(".srt")
                and os.path.isfile(resolved)
            ):
                return resolved
        except (OSError, RuntimeError, TypeError, ValueError):
            continue
    return None
