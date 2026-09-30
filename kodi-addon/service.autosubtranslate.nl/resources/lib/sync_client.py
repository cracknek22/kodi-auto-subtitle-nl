"""Send a playback reference only over certificate-pinned HTTPS."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import re
import ssl
from urllib.parse import parse_qsl, unquote, urlsplit


_HEADERS = {name.casefold(): name for name in ("User-Agent", "Referer", "Origin", "Cookie", "Authorization")}
_REFERENCE_ERROR = "Synchronisatie vereist een directe HTTP(S)-videolink met ondersteunde headers."
_BROKER_ERROR = "Verbinding met de beveiligde synchronisatieserver mislukt. Controleer de instellingen."


class SyncError(ValueError):
    """A safe message which may be shown in Kodi without exposing credentials."""


def _has_control(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def _reference_url(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 16384 or any(char in value for char in "|\\ ") or _has_control(value):
        raise SyncError(_REFERENCE_ERROR)
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname or parsed.username is not None or parsed.password is not None
            or parsed.fragment or parsed.port not in {None, 443 if parsed.scheme == "https" else 80}
            or _has_control(unquote(value))
        ):
            raise ValueError
        value.encode("ascii")
    except (UnicodeError, ValueError):
        raise SyncError(_REFERENCE_ERROR) from None
    return value


def _headers(pairs) -> dict[str, str]:
    result = {}
    seen = set()
    total = 0
    for name, value in pairs:
        if not isinstance(name, str) or not isinstance(value, str):
            raise SyncError(_REFERENCE_ERROR)
        lowered = name.casefold()
        total += len(value)
        if (
            lowered not in _HEADERS or lowered in seen or not 1 <= len(value) <= 8192
            or total > 12000 or any(not 32 <= ord(char) <= 126 for char in value)
        ):
            raise SyncError(_REFERENCE_ERROR)
        seen.add(lowered)
        result[_HEADERS[lowered]] = value
    return result


def parse_playback_url(reference: str) -> tuple[str, dict[str, str]]:
    """Split Kodi's URL|Header=value syntax without writing either to disk."""
    if not isinstance(reference, str) or _has_control(reference):
        raise SyncError(_REFERENCE_ERROR)
    url, separator, encoded_headers = reference.partition("|")
    url = _reference_url(url)
    if "|" in encoded_headers:
        raise SyncError(_REFERENCE_ERROR)
    try:
        pairs = parse_qsl(encoded_headers, keep_blank_values=True, strict_parsing=True) if separator else []
    except ValueError:
        raise SyncError(_REFERENCE_ERROR) from None
    return url, _headers(pairs)


def _validate_payload(payload: dict) -> None:
    if not isinstance(payload, dict) or set(payload) != {"job_id", "source", "source_sha256", "url", "headers"}:
        raise SyncError(_BROKER_ERROR)
    job_id, source, digest = payload["job_id"], payload["source"], payload["source_sha256"]
    if (
        not isinstance(job_id, str) or re.fullmatch(r"job_[a-f0-9]{32}", job_id) is None
        or not isinstance(source, str) or not 1 <= len(source) <= 240 or not source.casefold().endswith(".srt")
        or source.startswith(".") or "/" in source or "\\" in source or ":" in source or _has_control(source)
        or not isinstance(digest, str) or re.fullmatch(r"[a-f0-9]{64}", digest) is None
        or not isinstance(payload["headers"], dict)
    ):
        raise SyncError(_BROKER_ERROR)
    _reference_url(payload["url"])
    _headers(payload["headers"].items())


def register_reference(server: str, fingerprint: str, token: str, payload: dict) -> None:
    """Pin the TLS peer before any bearer token or playback reference is sent."""
    _validate_payload(payload)
    try:
        parsed = urlsplit(server)
        pin = fingerprint.replace(":", "").strip().lower()
        if (
            parsed.scheme != "https" or not parsed.hostname or parsed.path not in {"", "/"}
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or _has_control(server)
            or re.fullmatch(r"[a-f0-9]{64}", pin) is None
            or not isinstance(token, str) or not 32 <= len(token) <= 256
            or any(not 33 <= ord(char) <= 126 for char in token)
        ):
            raise ValueError
        port = parsed.port or 443
    except (AttributeError, TypeError, ValueError):
        raise SyncError(_BROKER_ERROR) from None

    # A private Radxa certificate may be self-signed. Authentication is the
    # out-of-band SHA-256 pin below, verified before application data is sent.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    connection = None
    try:
        connection = http.client.HTTPSConnection(parsed.hostname, port, timeout=20, context=context)
        connection.connect()
        certificate = connection.sock.getpeercert(binary_form=True)
        if not certificate or not hmac.compare_digest(hashlib.sha256(certificate).hexdigest(), pin):
            raise SyncError("Het certificaat van de synchronisatieserver komt niet overeen. Controleer de vingerafdruk.")
        # Never reconnect behind the certificate check if the socket disappears.
        connection.auto_open = 0
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        connection.request(
            "POST", "/api/v1/references", body=body,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json", "Accept": "application/json"},
        )
        response = connection.getresponse()
        if response.status not in {200, 201}:
            raise SyncError(_BROKER_ERROR)
        raw = response.read(8193)
        if len(raw) > 8192:
            raise SyncError(_BROKER_ERROR)
        result = json.loads(raw)
        if not isinstance(result, dict) or result.get("job_id") != payload["job_id"]:
            raise SyncError(_BROKER_ERROR)
    except SyncError:
        raise
    except Exception:
        raise SyncError(_BROKER_ERROR) from None
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
