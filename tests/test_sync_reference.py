import http.client
import json
import ssl
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from sync_reference import ReferenceStore, start_reference_server


def payload(**changes):
    result = {"job_id": "job_" + "a" * 32, "source": "Film.en.srt",
              "source_sha256": "b" * 64,
              "url": "https://cdn.example.com/movie.mkv?secret=TEST_SECRET",
              "headers": {"User-Agent": "Kodi", "Authorization": "Bearer TEST_SECRET"}}
    result.update(changes)
    return result


class ReferenceStoreTests(unittest.TestCase):
    def test_single_use_content_binding_and_secret_safe_repr(self):
        store = ReferenceStore(["*.example.com"])
        store.register(payload())
        with self.assertRaises(ValueError):
            store.consume("job_" + "a" * 32, "Other.srt", "b" * 64)
        ref = store.consume("job_" + "a" * 32, "Film.en.srt", "b" * 64)
        self.assertEqual(ref.url, payload()["url"])
        self.assertNotIn("TEST_SECRET", repr(ref))
        with self.assertRaises(ValueError):
            store.consume("job_" + "a" * 32, "Film.en.srt", "b" * 64)

    def test_expiration_capacity_and_duplicate_are_safe(self):
        now = [0]
        store = ReferenceStore(["cdn.example.com"], ttl_seconds=5, max_entries=1,
                               clock=lambda: now[0])
        store.register(payload())
        with self.assertRaises(ValueError):
            store.register(payload())
        with self.assertRaises(ValueError):
            store.register(payload(job_id="job_" + "c" * 32))
        now[0] = 5
        with self.assertRaises(ValueError):
            store.consume("job_" + "a" * 32, "Film.en.srt", "b" * 64)
        store.register(payload(job_id="job_" + "c" * 32))

    def test_rejects_untrusted_payloads_without_echoing_secrets(self):
        cases = [
            {"job_id": "../../TEST_SECRET"}, {"source": "../TEST_SECRET.srt"},
            {"source": "bad\\TEST_SECRET.srt"}, {"source_sha256": "TEST_SECRET"},
            {"url": "https://user:TEST_SECRET@cdn.example.com/a"},
            {"url": "https://cdn.example.com.evil.test/TEST_SECRET"},
            {"url": "file:///TEST_SECRET"}, {"url": "https://127.0.0.1/TEST_SECRET"},
            {"url": "https://cdn.example.com/TEST_SECRET\r\n"},
            {"url": "https://cdn.example.com:22/TEST_SECRET"},
            {"headers": {"Host": "TEST_SECRET"}},
            {"headers": {"Cookie": "TEST_SECRET\r\nx:y"}},
            {"headers": {"Cookie": "TEST_SECRET", "cookie": "other"}},
            {"headers": {"Authorization": "TEST_SECRET" * 2000}},
            {"unexpected": "TEST_SECRET"},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError) as raised:
                    ReferenceStore(["*.example.com"]).register(payload(**changes))
                self.assertNotIn("TEST_SECRET", str(raised.exception))

    def test_headers_are_copied_and_normalized(self):
        data = payload(headers={"user-agent": "Kodi"})
        store = ReferenceStore(["cdn.example.com"])
        store.register(data)
        data["headers"]["user-agent"] = "changed"
        ref = store.consume("job_" + "a" * 32, "Film.en.srt", "b" * 64)
        self.assertEqual(dict(ref.headers), {"User-Agent": "Kodi"})

    def test_invalid_consumption_values_are_safe_and_do_not_consume(self):
        store = ReferenceStore(["cdn.example.com"])
        store.register(payload())
        for job, digest in (([], "b" * 64), ("job_" + "a" * 32, "é" * 64)):
            with self.assertRaises(ValueError):
                store.consume(job, "Film.en.srt", digest)
        self.assertEqual(store.consume("job_" + "a" * 32, "Film.en.srt", "b" * 64).job_id, "job_" + "a" * 32)

    def test_config_limits_and_allowlist_are_validated(self):
        for hosts in ([], "cdn.example.com", ["*"], ["*.com"], ["bad..example.com"]):
            with self.subTest(hosts=hosts), self.assertRaises(ValueError):
                ReferenceStore(hosts)
        for options in ({"ttl_seconds": 0}, {"max_entries": 129}):
            with self.assertRaises(ValueError):
                ReferenceStore(["cdn.example.com"], **options)


class ReferenceServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.cert, cls.key = cls.root / "cert.pem", cls.root / "key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", str(cls.key), "-out", str(cls.cert), "-days", "1",
                        "-subj", "/CN=localhost"], check=True, capture_output=True)
        cls.key.chmod(0o600)
        cls.token = cls.root / "token"
        cls.token.write_text("test-token-" + "x" * 32)
        cls.token.chmod(0o600)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.store = ReferenceStore(["cdn.example.com"])
        self.server = start_reference_server(self.store, "127.0.0.1", 0,
                                            self.cert, self.key, self.token)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def request(self, method="POST", path="/api/v1/references", data=None, headers=None):
        defaults = {"Authorization": "Bearer " + self.token.read_text(),
                    "Content-Type": "application/json"}
        defaults.update(headers or {})
        conn = http.client.HTTPSConnection("127.0.0.1", self.server.server_port,
                                          context=ssl._create_unverified_context(), timeout=3)
        try:
            conn.request(method, path, body=json.dumps(payload()) if data is None else data,
                         headers=defaults)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def test_https_authenticated_registration_and_duplicate(self):
        status, body = self.request()
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(body), {"job_id": "job_" + "a" * 32})
        self.assertEqual(self.request()[0], 409)
        self.assertNotIn(b"TEST_SECRET", body)

    def test_rejects_missing_auth_oversize_encoding_and_invalid_payload(self):
        cases = [({"headers": {"Authorization": ""}}, 401),
                 ({"headers": {"Content-Type": "text/plain"}}, 415),
                 ({"headers": {"Transfer-Encoding": "chunked"}}, 400),
                 ({"data": "x" * 32769}, 413),
                 ({"data": "not json TEST_SECRET"}, 400),
                 ({"data": json.dumps(payload(source="../TEST_SECRET.srt"))}, 422)]
        for kwargs, expected in cases:
            with self.subTest(expected=expected):
                status, body = self.request(**kwargs)
                self.assertEqual(status, expected)
                self.assertNotIn(b"TEST_SECRET", body)

    def test_health_does_not_expose_references_and_requires_auth(self):
        self.assertEqual(self.request("GET", "/health")[0], 200)
        self.assertEqual(self.request("GET", "/health", headers={"Authorization": ""})[0], 401)
        self.assertEqual(self.request("GET", "/api/v1/references")[0], 404)

    def test_rate_limit_is_bounded(self):
        statuses = [self.request("GET", "/health")[0] for _ in range(61)]
        self.assertEqual(statuses.count(200), 60)
        self.assertEqual(statuses[-1], 429)

    def test_requires_private_secret_file_permissions(self):
        self.token.chmod(0o644)
        try:
            with self.assertRaises(ValueError):
                start_reference_server(self.store, "127.0.0.1", 0,
                                       self.cert, self.key, self.token)
        finally:
            self.token.chmod(0o600)

    def test_idle_store_is_purged_without_new_requests(self):
        now = [0]
        self.store._clock = lambda: now[0]
        self.store.register(payload())
        now[0] = 901
        deadline = time.monotonic() + 2
        while self.store._entries and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.store._entries, {})

    def test_stalled_tls_handshake_does_not_block_other_clients(self):
        with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=3):
            self.assertEqual(self.request("GET", "/health")[0], 200)


if __name__ == "__main__":
    unittest.main()
