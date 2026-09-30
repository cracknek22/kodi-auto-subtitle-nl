import http.client
import io
import unittest
from unittest.mock import Mock, patch

from media_proxy import MediaProxy, _PinnedConnection, open_public_media, public_addresses
from sync_reference import ReferenceStore


def reference(url="https://cdn.example.com/video?TEST_SECRET"):
    store = ReferenceStore(["*.example.com"])
    store.register({"job_id": "job_" + "a" * 32, "source": "Film.srt", "source_sha256": "b" * 64,
                    "url": url, "headers": {"Authorization": "Bearer TEST_SECRET", "User-Agent": "Kodi"}})
    return store.consume("job_" + "a" * 32, "Film.srt", "b" * 64)


def resolve_public(host, port, **_kwargs):
    return [(2, 1, 6, "", ("93.184.216.34", port))]


class FakeResponse(io.BytesIO):
    def __init__(self, status=200, headers=None, body=b"media"):
        super().__init__(body)
        self.status = status
        self.headers = headers or {"Content-Length": str(len(body)), "Content-Type": "video/x-matroska"}

    def getheader(self, name, default=None):
        return next((v for k, v in self.headers.items() if k.lower() == name.lower()), default)

    def getheaders(self):
        return list(self.headers.items())


class FakeConnection:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def request(self, method, path, headers):
        self.requests.append((method, path, headers))

    def getresponse(self):
        return self.response

    def close(self):
        pass


class MediaNetworkTests(unittest.TestCase):
    def test_all_dns_answers_must_be_public(self):
        for address in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "192.168.1.1", "::1", "fc00::1", "100.64.0.1", "224.0.0.1", "ff00::1", "::ffff:93.184.216.34"):
            def resolver(host, port, **kwargs):
                return resolve_public(host, port) + [(2, 1, 6, "", (address, port))]
            with self.subTest(address=address), self.assertRaises(ValueError):
                public_addresses("cdn.example.com", 443, resolver=resolver)

    def test_pinned_connection_uses_numeric_ip_and_verified_sni(self):
        sock = Mock()
        context = Mock()
        with patch("media_proxy.socket.socket", return_value=sock), patch(
                "media_proxy.ssl.create_default_context", return_value=context) as creator:
            connection = _PinnedConnection("https", "cdn.example.com", 443, "93.184.216.34", 30)
            connection.connect()
        sock.connect.assert_called_once_with(("93.184.216.34", 443))
        creator.assert_called_once_with()
        context.wrap_socket.assert_called_once_with(sock, server_hostname="cdn.example.com")

    def test_invalid_method_and_range_rejected(self):
        for method, byte_range in (("POST", None), ("GET", "bytes=0-2,4-6"), ("GET", "bytes=0-4\r\nX: secret")):
            with self.assertRaises(ValueError):
                open_public_media(reference(), ["*.example.com"], method, byte_range)

    def test_uses_resolved_ip_and_keeps_host_range_and_identity(self):
        calls = []
        conn = FakeConnection(FakeResponse(206, {"Content-Range": "bytes 0-4/100", "Content-Length": "5"}))
        def connector(scheme, host, port, address, timeout):
            calls.append((scheme, host, port, address))
            return conn
        response, _ = open_public_media(reference(), ["*.example.com"], "GET", "bytes=0-4",
                                       resolver=resolve_public, connector=connector)
        self.assertEqual(response.status, 206)
        self.assertEqual(calls, [("https", "cdn.example.com", 443, "93.184.216.34")])
        self.assertEqual(conn.requests[0][2]["Range"], "bytes=0-4")
        self.assertEqual(conn.requests[0][2]["Host"], "cdn.example.com")

    def test_redirect_revalidates_and_strips_cross_host_credentials(self):
        first = FakeConnection(FakeResponse(302, {"Location": "https://other.example.com/video"}))
        second = FakeConnection(FakeResponse())
        queue = [first, second]
        open_public_media(reference(), ["*.example.com"], "HEAD", None,
                          resolver=resolve_public, connector=lambda *args: queue.pop(0))
        self.assertNotIn("Authorization", second.requests[0][2])
        self.assertEqual(second.requests[0][2]["User-Agent"], "Kodi")

    def test_rebinding_private_redirect_downgrade_and_redirect_limit_rejected(self):
        for location in ("http://cdn.example.com/a", "https://evil.test/a", "https://127.0.0.1/a"):
            with self.subTest(location=location), self.assertRaises(ValueError):
                open_public_media(reference(), ["*.example.com"], "GET", None,
                                  resolver=resolve_public,
                                  connector=lambda *args: FakeConnection(FakeResponse(302, {"Location": location})))
        resolutions = [resolve_public("x", 443), [(2, 1, 6, "", ("127.0.0.1", 443))]]
        with self.assertRaises(ValueError):
            open_public_media(reference(), ["*.example.com"], "GET", None,
                              resolver=lambda *args, **kwargs: resolutions.pop(0),
                              connector=lambda *args: FakeConnection(FakeResponse(302, {"Location": "/next"})))
        with self.assertRaises(ValueError):
            open_public_media(reference(), ["*.example.com"], "GET", None,
                              resolver=resolve_public,
                              connector=lambda *args: FakeConnection(FakeResponse(302, {"Location": "/loop"})))


class MediaProxyTests(unittest.TestCase):
    def test_proxy_opaque_url_range_head_and_untrusted_path(self):
        seen = []
        def opener(ref, allowed_hosts, method, range_header):
            seen.append((method, range_header))
            response = FakeResponse(206, {"Content-Length": "5", "Content-Range": "bytes 0-4/20"})
            return response, FakeConnection(response)
        with MediaProxy(reference(), ["*.example.com"], opener=opener) as proxy:
            self.assertNotIn("TEST_SECRET", proxy.url)
            self.assertNotIn("TEST_SECRET", repr(proxy))
            from urllib.parse import urlsplit
            parts = urlsplit(proxy.url)
            connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=3)
            connection.request("GET", parts.path, headers={"Range": "bytes=0-4"})
            response = connection.getresponse()
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), b"media")
            connection.close()
            connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=3)
            connection.request("HEAD", parts.path)
            response = connection.getresponse()
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), b"")
            connection.close()
            connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=3)
            connection.request("GET", "/unknown")
            self.assertEqual(connection.getresponse().status, 404)
            connection.close()
        self.assertEqual(seen, [("GET", "bytes=0-4"), ("HEAD", None)])

    def test_upstream_exception_is_not_returned_or_logged(self):
        def failing(*args):
            raise ValueError("TEST_SECRET")
        with MediaProxy(reference(), ["*.example.com"], opener=failing) as proxy:
            from urllib.parse import urlsplit
            parts = urlsplit(proxy.url)
            connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=3)
            connection.request("GET", parts.path)
            response = connection.getresponse()
            self.assertEqual(response.status, 502)
            self.assertNotIn(b"TEST_SECRET", response.read())
            connection.close()


if __name__ == "__main__":
    unittest.main()
