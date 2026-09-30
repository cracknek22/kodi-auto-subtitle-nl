"""Loopback-only streaming proxy: ffmpeg never sees provider URL credentials.

Every outgoing connection resolves once, rejects all non-public answers and uses
that numeric address directly. TLS still verifies the original hostname with SNI.
Only a caller-provided host allowlist is permitted, including after redirects.
"""

from __future__ import annotations

import http.client
import ipaddress
import re
import secrets
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler
from urllib.parse import urljoin, urlsplit

from sync_reference import _BoundedHTTPServer, is_public_address, normalize_allowed_hosts, validate_media_url


MAX_REDIRECTS = 4
NETWORK_TIMEOUT_SECONDS = 30
_RANGE_RE = re.compile(r"bytes=(?:[0-9]{1,18}-[0-9]{0,18}|-[0-9]{1,18})\Z")


def public_addresses(host, port, resolver=socket.getaddrinfo):
    try:
        answers = resolver(host, port, type=socket.SOCK_STREAM)
        addresses = []
        for answer in answers:
            value = ipaddress.ip_address(answer[4][0])
            if not is_public_address(value):
                raise ValueError("Non-public media destination")
            if str(value) not in addresses:
                addresses.append(str(value))
        if not addresses:
            raise ValueError("Media destination unavailable")
        return addresses
    except (OSError, ValueError, IndexError, TypeError):
        raise ValueError("Media destination is unavailable or not public") from None


class _PinnedConnection(http.client.HTTPConnection):
    def __init__(self, scheme, host, port, address, timeout):
        super().__init__(host, port=port, timeout=timeout)
        self._scheme = scheme
        self._address = address

    def connect(self):
        family = socket.AF_INET6 if ipaddress.ip_address(self._address).version == 6 else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect((self._address, self.port))
            if self._scheme == "https":
                context = ssl.create_default_context()
                context.minimum_version = ssl.TLSVersion.TLSv1_2
                sock = context.wrap_socket(sock, server_hostname=self.host)
            self.sock = sock
        except Exception:
            sock.close()
            raise


def open_public_media(reference, allowed_hosts, method, range_header,
                      resolver=socket.getaddrinfo, connector=_PinnedConnection):
    """Return (HTTPResponse, connection); caller closes both, even after errors."""
    if method not in ("GET", "HEAD"):
        raise ValueError("Unsupported media request")
    if range_header is not None and not _RANGE_RE.fullmatch(range_header):
        raise ValueError("Invalid media range")
    allowed_hosts = normalize_allowed_hosts(allowed_hosts)
    current = reference.url
    headers = dict(reference.headers)
    connection = response = None
    try:
        for hop in range(MAX_REDIRECTS + 1):
            parts = validate_media_url(current, allowed_hosts)
            host = parts.hostname
            port = parts.port or (443 if parts.scheme == "https" else 80)
            # Reject a mixed public/private DNS set; do not choose a convenient answer.
            addresses = public_addresses(host, port, resolver=resolver)
            connection = connector(parts.scheme, host, port, addresses[0], NETWORK_TIMEOUT_SECONDS)
            sent_headers = dict(headers)
            sent_headers.update({"Host": "[" + host + "]" if ":" in host else host,
                                 "Accept-Encoding": "identity", "Connection": "close"})
            if range_header is not None:
                sent_headers["Range"] = range_header
            target = parts.path or "/"
            if parts.query:
                target += "?" + parts.query
            connection.request(method, target, headers=sent_headers)
            response = connection.getresponse()
            if response.status not in (301, 302, 303, 307, 308):
                return response, connection
            location = response.getheader("Location")
            if not location or hop == MAX_REDIRECTS:
                raise ValueError("Media redirect unavailable")
            next_url = urljoin(current, location)
            next_parts = validate_media_url(next_url, allowed_hosts)
            if parts.scheme == "https" and next_parts.scheme != "https":
                raise ValueError("Insecure media redirect")
            if (parts.scheme, parts.hostname, parts.port) != (next_parts.scheme, next_parts.hostname, next_parts.port):
                headers = {key: value for key, value in headers.items() if key.lower() == "user-agent"}
            response.close()
            connection.close()
            connection = response = None
            current = next_url
    except Exception:
        if response is not None:
            response.close()
        if connection is not None:
            connection.close()
        raise ValueError("Media stream could not be opened safely") from None


class MediaProxy:
    def __init__(self, reference, allowed_hosts, opener=open_public_media):
        self._reference = reference
        self._allowed_hosts = normalize_allowed_hosts(allowed_hosts)
        self._opener = opener
        self._server = None
        self.url = ""

    def __repr__(self):
        return "MediaProxy(active=" + str(self._server is not None) + ")"

    def __enter__(self):
        if self._server is not None:
            raise ValueError("Media proxy is already active")
        proxy = self
        path = "/" + secrets.token_urlsafe(32) + "/reference.mkv"

        class Handler(BaseHTTPRequestHandler):
            server_version = "SubtitleMedia"
            sys_version = ""

            def log_message(self, *args):
                pass

            def send_error(self, code, message=None, explain=None):
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True

            def do_HEAD(self):
                self.stream()

            def do_GET(self):
                self.stream()

            def stream(self):
                if self.path != path:
                    self.send_error(404)
                    return
                ranges = self.headers.get_all("Range", [])
                if len(ranges) > 1 or (ranges and not _RANGE_RE.fullmatch(ranges[0])):
                    self.send_error(400)
                    return
                upstream = connection = None
                started = False
                try:
                    upstream, connection = proxy._opener(proxy._reference, proxy._allowed_hosts,
                                                         self.command, ranges[0] if ranges else None)
                    if upstream.status not in (200, 206, 416):
                        self.send_error(502)
                        return
                    forwarded = {}
                    for name in ("Content-Length", "Content-Range", "Accept-Ranges"):
                        value = upstream.getheader(name)
                        if value is not None:
                            if (len(value) > 256 or any(ord(char) < 32 or ord(char) > 126 for char in value)
                                    or (name == "Content-Length" and not re.fullmatch(r"[0-9]{1,15}", value))):
                                raise ValueError("Invalid media response")
                            forwarded[name] = value
                    self.send_response(upstream.status)
                    for name, value in forwarded.items():
                        self.send_header(name, value)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Connection", "close")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    started = True
                    if self.command == "GET":
                        while True:
                            block = upstream.read(65536)
                            if not block:
                                break
                            self.wfile.write(block)
                except Exception:
                    if not started:
                        self.send_error(502)
                finally:
                    if upstream is not None:
                        upstream.close()
                    if connection is not None:
                        connection.close()
                    self.close_connection = True

        server = _BoundedHTTPServer(("127.0.0.1", 0), Handler)
        self._server = server
        self.url = "http://127.0.0.1:" + str(server.server_port) + path
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        self.url = ""
