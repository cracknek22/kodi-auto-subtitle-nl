"""Short-lived authenticated media references; signed URLs never enter SMB jobs.

This endpoint is deliberately HTTPS-only. Kodi pins its certificate and presents
a privately configured bearer token. Reference values live only in process memory.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import re
import ssl
import stat
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit


MAX_BODY_BYTES = 32768
HEADER_NAMES = {name.lower(): name for name in
                ("User-Agent", "Referer", "Origin", "Cookie", "Authorization")}
_JOB_RE = re.compile(r"job_[a-f0-9]{32}\Z")
_HASH_RE = re.compile(r"[a-f0-9]{64}\Z")
_HOST_RE = re.compile(r"(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\Z")


def _printable(value, maximum):
    return (isinstance(value, str) and 0 < len(value) <= maximum
            and all(32 <= ord(char) <= 126 for char in value))


def _host(value):
    if not isinstance(value, str):
        raise ValueError("Invalid media host")
    value = value.lower()
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        pass
    if (not _HOST_RE.fullmatch(value) or ".." in value
            or any(not label or len(label) > 63 or label.startswith("-")
                   or label.endswith("-") for label in value.split("."))):
        raise ValueError("Invalid media host")
    return value


def normalize_allowed_hosts(allowed_hosts):
    if isinstance(allowed_hosts, str):
        raise ValueError("Media allowlist must be a list")
    result = []
    for item in allowed_hosts:
        if not isinstance(item, str):
            raise ValueError("Invalid media allowlist")
        wildcard = item.startswith("*.")
        name = _host(item[2:] if wildcard else item)
        if wildcard and ("." not in name or ":" in name):
            raise ValueError("Invalid media allowlist")
        result.append("*." + name if wildcard else name)
    if not result or len(result) > 32:
        raise ValueError("Media allowlist is empty or too large")
    return tuple(result)


def is_public_address(address):
    return (address.is_global and not address.is_multicast
            and not address.is_reserved and not address.is_loopback
            and not address.is_link_local and not address.is_unspecified
            and not (address.version == 6 and address.ipv4_mapped is not None))


def validate_media_url(url, allowed_hosts):
    """Validate URL syntax and exact/suffix allowlist without network access."""
    if not _printable(url, 16384) or "\\" in url:
        raise ValueError("Invalid media URL")
    try:
        parts = urlsplit(url)
        hostname = _host(parts.hostname)
        port = parts.port
    except (ValueError, TypeError):
        raise ValueError("Invalid media URL") from None
    if (parts.scheme not in ("https", "http") or parts.username is not None
            or parts.password is not None or parts.fragment
            or port not in (None, 443 if parts.scheme == "https" else 80)):
        raise ValueError("Unsupported media URL")
    if not any(hostname == entry or
               (entry.startswith("*.") and hostname.endswith(entry[1:])
                and hostname != entry[2:]) for entry in allowed_hosts):
        raise ValueError("Media host is not allowed")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not is_public_address(address):
        raise ValueError("Media address is not public")
    return parts


@dataclass(frozen=True)
class MediaReference:
    job_id: str
    source: str
    source_sha256: str
    url: str = field(repr=False)
    headers: object = field(repr=False)


class ReferenceConflict(ValueError):
    pass


class ReferenceCapacity(ValueError):
    pass


class ReferenceStore:
    def __init__(self, allowed_hosts, ttl_seconds=900, max_entries=16,
                 clock=time.monotonic):
        self.allowed_hosts = normalize_allowed_hosts(allowed_hosts)
        if not 1 <= ttl_seconds <= 3600 or not 1 <= max_entries <= 128:
            raise ValueError("Invalid reference limits")
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._clock = clock
        self._entries = {}
        self._lock = threading.Lock()

    def _expire(self, now):
        for key in [key for key, (_, expiry) in self._entries.items() if expiry <= now]:
            del self._entries[key]

    def expire(self):
        """Erase expired secrets even while the receiver has no active clients."""
        with self._lock:
            self._expire(self._clock())

    def register(self, payload):
        fields = {"job_id", "source", "source_sha256", "url", "headers"}
        if not isinstance(payload, dict) or set(payload) != fields:
            raise ValueError("Invalid reference fields")
        job, source, digest = (payload[name] for name in ("job_id", "source", "source_sha256"))
        if not isinstance(job, str) or not _JOB_RE.fullmatch(job):
            raise ValueError("Invalid reference job")
        if (not isinstance(source, str) or not 1 <= len(source) <= 240
                or source in (".", "..") or any(char in source for char in "/\\:")
                or any(ord(char) < 32 or ord(char) == 127 for char in source)
                or not source.lower().endswith(".srt") or source.startswith(".")):
            raise ValueError("Invalid reference subtitle")
        if not isinstance(digest, str) or not _HASH_RE.fullmatch(digest):
            raise ValueError("Invalid reference hash")
        validate_media_url(payload["url"], self.allowed_hosts)
        supplied_headers = payload["headers"]
        if not isinstance(supplied_headers, dict) or len(supplied_headers) > len(HEADER_NAMES):
            raise ValueError("Invalid media headers")
        headers = {}
        for name, value in supplied_headers.items():
            canonical = HEADER_NAMES.get(name.lower()) if isinstance(name, str) else None
            if canonical is None or canonical in headers or not _printable(value, 8192):
                raise ValueError("Invalid media headers")
            headers[canonical] = value
        if sum(len(value) for value in headers.values()) > 12000:
            raise ValueError("Media headers too large")
        reference = MediaReference(job, source, digest, payload["url"], MappingProxyType(headers))
        with self._lock:
            now = self._clock()
            self._expire(now)
            if job in self._entries:
                raise ReferenceConflict("Reference already registered")
            if len(self._entries) >= self.max_entries:
                raise ReferenceCapacity("Reference capacity reached")
            self._entries[job] = (reference, now + self.ttl_seconds)
        return reference

    def consume(self, job_id, source, source_sha256):
        if (not isinstance(job_id, str) or not _JOB_RE.fullmatch(job_id)
                or not isinstance(source_sha256, str) or not _HASH_RE.fullmatch(source_sha256)):
            raise ValueError("Media reference unavailable or does not match subtitle")
        with self._lock:
            self._expire(self._clock())
            entry = self._entries.get(job_id)
            if (entry is None or entry[0].source != source
                    or not isinstance(source_sha256, str)
                    or not hmac.compare_digest(entry[0].source_sha256, source_sha256)):
                raise ValueError("Media reference unavailable or does not match subtitle")
            del self._entries[job_id]
            return entry[0]


class _BoundedHTTPServer(ThreadingHTTPServer):
    """Cap worker count before allocating a request thread; suppress raw errors."""
    daemon_threads = True
    request_queue_size = 8
    allow_reuse_address = True
    tls_context = None
    maintenance = None

    def __init__(self, *args, **kwargs):
        self._slots = threading.BoundedSemaphore(8)
        super().__init__(*args, **kwargs)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(10)
        if self.tls_context is not None:
            try:
                request = self.tls_context.wrap_socket(request, server_side=True,
                                                       do_handshake_on_connect=False)
            except Exception:
                request.close()
                raise OSError("TLS connection unavailable") from None
        return request, address

    def service_actions(self):
        if self.maintenance is not None:
            self.maintenance()

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            self.shutdown_request(request)

    def process_request_thread(self, request, client_address):
        try:
            if self.tls_context is not None:
                try:
                    request.do_handshake()
                except (OSError, ValueError):
                    self.shutdown_request(request)
                    return
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address):
        # Neither request targets nor exceptions are safe to log.
        pass


def _private_file(path):
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError("Private sync configuration must have mode 600")
    except OSError:
        raise ValueError("Private sync configuration unavailable") from None


def start_reference_server(store, bind, port, cert_file: Path, key_file: Path,
                           token_file: Path):
    """Start HTTPS receiver. Caller must shutdown() and server_close() on exit."""
    _private_file(Path(key_file))
    _private_file(Path(token_file))
    try:
        token = Path(token_file).read_text(encoding="ascii").strip()
        if not _printable(token, 512) or len(token) < 32 or " " in token:
            raise ValueError("Invalid sync token")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(str(cert_file), str(key_file))
    except (OSError, UnicodeError, ssl.SSLError):
        raise ValueError("Secure reference receiver configuration unavailable") from None
    rate_lock = threading.Lock()
    rate_state = [time.monotonic(), 0]

    class Handler(BaseHTTPRequestHandler):
        server_version = "SubtitleReference"
        sys_version = ""

        def log_message(self, *args):
            pass

        def send_error(self, code, message=None, explain=None):
            self.reply(code, {"error": {"code": "invalid_request"}})

        def reply(self, status, body):
            encoded = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(encoded)
            self.close_connection = True

        def fail(self, status, code):
            self.reply(status, {"error": {"code": code}})

        def authorized(self):
            with rate_lock:
                now = time.monotonic()
                if now - rate_state[0] >= 60:
                    rate_state[:] = [now, 0]
                rate_state[1] += 1
                limited = rate_state[1] > 60
            if limited:
                self.fail(429, "rate_limited")
                return False
            supplied = self.headers.get_all("Authorization", [])
            if (len(supplied) != 1 or not supplied[0].isascii()
                    or not hmac.compare_digest(supplied[0], "Bearer " + token)):
                self.fail(401, "unauthorized")
                return False
            return True

        def do_GET(self):
            if self.authorized():
                if self.path == "/health":
                    self.reply(200, {"status": "ok"})
                else:
                    self.fail(404, "not_found")

        def do_POST(self):
            if not self.authorized():
                return
            if self.path != "/api/v1/references":
                self.fail(404, "not_found")
                return
            lengths = self.headers.get_all("Content-Length", [])
            if (self.headers.get("Transfer-Encoding") is not None or len(lengths) != 1
                    or not re.fullmatch(r"[0-9]{1,8}", lengths[0])):
                self.fail(400, "invalid_framing")
                return
            size = int(lengths[0])
            if size > MAX_BODY_BYTES:
                self.fail(413, "request_too_large")
                return
            if (self.headers.get_content_type() != "application/json"
                    or self.headers.get("Content-Encoding") is not None):
                self.fail(415, "unsupported_media_type")
                return
            try:
                raw = self.rfile.read(size)
                if len(raw) != size:
                    raise ValueError("Incomplete request")
                payload = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeError, OSError):
                self.fail(400, "invalid_json")
                return
            try:
                reference = store.register(payload)
            except ReferenceConflict:
                self.fail(409, "reference_exists")
            except ReferenceCapacity:
                self.fail(429, "reference_capacity")
            except ValueError:
                self.fail(422, "invalid_reference")
            else:
                self.reply(201, {"job_id": reference.job_id})

    server = _BoundedHTTPServer((bind, port), Handler)
    try:
        server.tls_context = context
        server.maintenance = store.expire
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        thread.start()
    except Exception:
        server.server_close()
        raise ValueError("Secure reference receiver could not start") from None
    return server
