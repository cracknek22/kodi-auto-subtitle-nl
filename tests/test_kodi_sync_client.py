import hashlib
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ADDON_LIB = Path(__file__).resolve().parents[1] / "kodi-addon" / "service.autosubtranslate.nl" / "resources" / "lib"
sys.path.insert(0, str(ADDON_LIB))

import sync_client  # noqa: E402


class PlaybackReferenceTests(unittest.TestCase):
    def test_extracts_kodi_headers_without_changing_url_or_tokens(self):
        url, headers = sync_client.parse_playback_url(
            "https://cdn.example/movie.mkv?token=a%2Bb"
            "|User-Agent=Kodi+21&Cookie=session%3Dprivate&Referer=https%3A%2F%2Fexample.com"
        )
        self.assertEqual(url, "https://cdn.example/movie.mkv?token=a%2Bb")
        self.assertEqual(headers, {"User-Agent": "Kodi 21", "Cookie": "session=private", "Referer": "https://example.com"})
        self.assertEqual(sync_client.parse_playback_url("http://cdn.example:80/movie.mkv"), ("http://cdn.example:80/movie.mkv", {}))

    def test_rejects_unresolved_unsafe_or_conflicting_references(self):
        invalid = [
            "plugin://video/item", "smb://box/share/movie.mkv", "/local/movie.mkv", "",
            "https://user:password@cdn.example/video", "https://cdn.example:8080/video",
            "https://cdn.example/video#fragment", "https://cdn.example/video\r\nsecret",
            "https://cdn.example/video|Cookie=x%0D%0AX-Evil%3Ay",
            "https://cdn.example/video|Cookie=x&cookie=y", "https://cdn.example/video|Cookie=x&Host=evil",
            "https://cdn.example/video|Cookie=x|other", "https://cdn.example/video|Authorization=",
            "https://cdn.example/video|Cookie", "https://cdn.example/video|X-Unknown=secret",
        ]
        for reference in invalid:
            with self.subTest(reference=reference):
                with self.assertRaises(ValueError) as caught:
                    sync_client.parse_playback_url(reference)
                self.assertNotIn("password", str(caught.exception))
                self.assertNotIn("secret", str(caught.exception))


class PinnedBrokerTests(unittest.TestCase):
    def setUp(self):
        self.certificate = b"fixture-server-certificate"
        self.fingerprint = hashlib.sha256(self.certificate).hexdigest()
        self.token = "fixture-bearer-token-" + "a" * 32
        self.payload = {
            "job_id": "job_" + "1" * 32, "source": "Movie.en.srt", "source_sha256": "a" * 64,
            "url": "https://cdn.example/video?token=private", "headers": {"Cookie": "session=private"},
        }
        self.events = []
        self.response = Mock(status=201)
        self.response.read.return_value = json.dumps({"job_id": self.payload["job_id"]}).encode()
        self.connection = Mock()
        self.connection.connect.side_effect = lambda: self.events.append("connect")
        self.connection.sock.getpeercert.side_effect = lambda **_kw: self.events.append("certificate") or self.certificate
        self.connection.request.side_effect = lambda *_args, **_kwargs: self.events.append("request")
        self.connection.getresponse.return_value = self.response
        self.factory = patch.object(sync_client.http.client, "HTTPSConnection", return_value=self.connection)
        self.connection_factory = self.factory.start()
        self.addCleanup(self.factory.stop)

    def register(self, server="https://192.168.2.60:8766", fingerprint=None, token=None, payload=None):
        return sync_client.register_reference(
            server, fingerprint if fingerprint is not None else self.fingerprint,
            token if token is not None else self.token,
            payload if payload is not None else self.payload,
        )

    def test_verifies_certificate_before_sending_token_and_reference(self):
        self.register()
        self.assertEqual(self.events, ["connect", "certificate", "request"])
        self.connection.sock.getpeercert.assert_called_once_with(binary_form=True)
        args, kwargs = self.connection.request.call_args
        self.assertEqual(args, ("POST", "/api/v1/references"))
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer " + self.token)
        self.assertEqual(json.loads(kwargs["body"]), self.payload)
        self.assertEqual(self.connection.auto_open, 0)
        self.connection.close.assert_called_once()

    def test_pin_mismatch_never_sends_secrets(self):
        with self.assertRaisesRegex(ValueError, "certificaat"):
            self.register(fingerprint="b" * 64)
        self.assertEqual(self.events, ["connect", "certificate"])
        self.connection.request.assert_not_called()
        self.connection.close.assert_called_once()

    def test_plaintext_missing_pin_or_invalid_token_never_connect(self):
        for kwargs in (
            {"server": "http://192.168.2.60:8766"}, {"server": "https://user:pass@localhost"},
            {"server": "https://localhost/path?secret=token"}, {"fingerprint": ""},
            {"fingerprint": "not-a-pin"}, {"token": "short"}, {"token": "x" * 32 + "\r\n"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.register(**kwargs)
        self.connection_factory.assert_not_called()

    def test_validates_payload_and_rejects_untrusted_headers_before_connect(self):
        for payload in (
            {**self.payload, "source": "../Movie.en.srt"},
            {**self.payload, "source_sha256": "bad"}, {**self.payload, "job_id": "../bad"},
            {**self.payload, "job_id": "job_12345678"},
            {**self.payload, "url": "plugin://unresolved"},
            {**self.payload, "headers": {"Host": "evil"}},
            {**self.payload, "headers": {"Cookie": "x\nsecret"}},
            {**self.payload, "headers": {"Cookie": "x", "cookie": "y"}},
            {**self.payload, "unexpected": "secret"},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.register(payload=payload)
        self.connection_factory.assert_not_called()

    def test_rejects_http_failure_wrong_job_or_invalid_response_without_leaks(self):
        cases = [(302, b'{"url":"https://secret"}'), (401, b'private-token'), (201, b'not-json-private'), (201, b'{"job_id":"wrong"}'), (201, b'x' * 8193)]
        for status, content in cases:
            self.response.status = status
            self.response.read.return_value = content
            with self.subTest(status=status, content=content[:20]), self.assertRaises(ValueError) as caught:
                self.register()
            self.assertNotIn("private", str(caught.exception))
            self.assertNotIn("secret", str(caught.exception))

    def test_network_error_is_generic_and_closes_connection(self):
        self.connection.connect.side_effect = OSError("private-url-token")
        with self.assertRaisesRegex(ValueError, "synchronisatie") as caught:
            self.register()
        self.assertNotIn("private", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)
        self.connection.request.assert_not_called()
        self.connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
