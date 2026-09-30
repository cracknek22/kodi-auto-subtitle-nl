import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit


ADDON_LIB = (
    Path(__file__).resolve().parents[1]
    / "kodi-addon"
    / "service.autosubtranslate.nl"
    / "resources"
    / "lib"
)
sys.path.insert(0, str(ADDON_LIB))

from opensubs import (  # noqa: E402
    PROVIDER_ID,
    directory_files,
    download_url,
    search_url,
    subtitle_path,
)


def result_entry(file_id="123", language="English"):
    return {
        "file": (
            f"plugin://{PROVIDER_ID}/?action=download"
            f"&id={file_id}&language={language}"
        ),
        "filetype": "file",
        "label": language,
    }


class ProviderUrlTests(unittest.TestCase):
    def test_search_requests_english_and_has_unique_cache_key(self):
        first, second = search_url(), search_url()
        self.assertNotEqual(first, second)
        parsed = urlsplit(first)
        self.assertEqual(parsed.scheme, "plugin")
        self.assertEqual(parsed.netloc, "service.subtitles.opensubtitles-com")
        query = parse_qs(parsed.query)
        self.assertEqual(query["action"], ["search"])
        self.assertEqual(query["languages"], ["English"])
        self.assertEqual(query["preferredlanguage"], ["English"])
        self.assertRegex(query["autosub_request"][0], r"^[a-f0-9]{32}$")

    def test_first_valid_english_result_preserves_provider_ranking(self):
        entries = [None, result_entry("50", "Dutch"), result_entry("456"), result_entry("12")]
        selected = download_url(entries)
        query = parse_qs(urlsplit(selected).query)
        self.assertEqual(query["id"], ["456"])
        self.assertEqual(query["language"], ["en"])
        self.assertNotEqual(selected, download_url(entries))

    def test_english_aliases_are_accepted_and_unknown_params_not_forwarded(self):
        for language in ("English", "english", "en", "eng", "EN"):
            with self.subTest(language=language):
                entry = result_entry(language=language)
                entry["file"] += "&token=do-not-forward&autosub_request=old"
                selected = download_url([entry])
                query = parse_qs(urlsplit(selected).query)
                self.assertEqual(query["language"], ["en"])
                self.assertNotIn("token", query)
                self.assertNotIn("old", query["autosub_request"])

    def test_no_usable_results_returns_none(self):
        for entries in ([], None, {}, "bad", [None, {}, {"file": 42}]):
            with self.subTest(entries=entries):
                self.assertIsNone(download_url(entries))

    def test_rejects_other_addons_actions_ids_and_ambiguous_urls(self):
        base = result_entry()["file"]
        invalid = [
            base.replace("plugin:", "https:"),
            base.replace(PROVIDER_ID, "service.subtitles.evil"),
            base.replace(PROVIDER_ID, "user:secret@" + PROVIDER_ID),
            base.replace(PROVIDER_ID, PROVIDER_ID + ":80"),
            base.replace("/?", "/other?"),
            base.replace("download", "delete"),
            base.replace("id=123", "id=0"),
            base.replace("id=123", "id=-2"),
            base.replace("id=123", "id=12.0"),
            base.replace("id=123", "id=１２３"),
            base.replace("id=123", "id=" + "1" * 33),
            base + "&id=999",
            base + "&language=Dutch",
            base + "#fragment",
            base + "\n",
        ]
        for url in invalid:
            with self.subTest(url=url):
                self.assertIsNone(download_url([{"file": url}]))
        self.assertIsNone(download_url([{**result_entry(), "filetype": "directory"}]))


class DirectoryBridgeTests(unittest.TestCase):
    def test_executes_provider_directory_and_keeps_result_order(self):
        entries = [result_entry("123"), result_entry("456")]
        execute = Mock(return_value=json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"files": entries}}))
        url = search_url()
        self.assertEqual(directory_files(execute, url), entries)
        request = json.loads(execute.call_args.args[0])
        self.assertEqual(request["method"], "Files.GetDirectory")
        self.assertEqual(request["params"]["directory"], url)
        self.assertEqual(request["params"]["media"], "files")
        self.assertEqual(request["params"]["sort"], {"method": "none"})

    def test_download_invocation_and_empty_list_are_valid(self):
        execute = Mock(return_value=json.dumps({"result": {"files": []}}))
        self.assertEqual(directory_files(execute, download_url([result_entry()])), [])

    def test_response_errors_are_generic_and_hide_provider_details(self):
        secret = "secret-token-do-not-show"
        responses = [
            secret,
            None,
            json.dumps(None),
            json.dumps([]),
            json.dumps({"error": {"message": secret}}),
            json.dumps({"result": None}),
            json.dumps({"result": {"files": {}}}),
            json.dumps({"result": {"files": [None]}}),
            json.dumps({"result": {"files": [secret]}}),
            json.dumps({"error": {}, "result": {"files": []}}),
        ]
        for response in responses:
            with self.subTest(response=response):
                with self.assertRaisesRegex(ValueError, "OpenSubtitles") as caught:
                    directory_files(Mock(return_value=response), search_url())
                self.assertNotIn(secret, str(caught.exception))

    def test_call_failure_does_not_expose_exception_message(self):
        with self.assertRaisesRegex(ValueError, "OpenSubtitles") as caught:
            directory_files(Mock(side_effect=RuntimeError("secret-password")), search_url())
        self.assertNotIn("secret-password", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)

    def test_unsafe_request_is_rejected_before_kodi_invocation(self):
        execute = Mock()
        for url in ("plugin://evil/?action=search", "http://localhost/", search_url() + "&token=secret"):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    directory_files(execute, url)
        execute.assert_not_called()


class SubtitlePathTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.provider_temp = self.root / "provider" / "temp"
        self.provider_temp.mkdir(parents=True)
        self.valid = self.provider_temp / "TempSubtitle.abc.en.srt"
        self.valid.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello.\n")
        self.special = f"special://profile/addon_data/{PROVIDER_ID}/temp/"

    def translate(self, path):
        if path.startswith(self.special):
            return str(self.provider_temp / path[len(self.special):])
        return path

    def find(self, entries, provider_temp=None):
        return subtitle_path(entries, provider_temp or str(self.provider_temp), self.translate)

    def test_returns_existing_local_srt_in_provider_temp(self):
        self.assertEqual(self.find([{"file": str(self.valid)}]), str(self.valid.resolve()))

    def test_resolves_special_paths_and_directory_aliases(self):
        entry = {"file": self.special + self.valid.name}
        self.assertEqual(self.find([entry], self.special), str(self.valid.resolve()))

    def test_accepts_valid_after_invalid_entry_without_leaking_other_paths(self):
        entries = [None, {"file": "/outside.srt"}, {"file": str(self.valid)}]
        self.assertEqual(self.find(entries), str(self.valid.resolve()))

    def test_rejects_traversal_other_protocols_directories_and_wrong_extensions(self):
        invalid_paths = [
            str(self.provider_temp / ".." / "temp" / self.valid.name),
            str(self.provider_temp / "%2e%2e" / "outside.srt"),
            str(self.provider_temp / "missing.srt"),
            str(self.provider_temp),
            "relative.srt",
            "https://example.test/temp/file.srt",
            "file://" + str(self.valid),
            "smb://server/temp/file.srt",
            str(self.valid) + "?token=secret",
            str(self.valid) + "\x00",
        ]
        for path in invalid_paths:
            with self.subTest(path=path):
                self.assertIsNone(self.find([{"file": path}]))
        directory_srt = self.provider_temp / "directory.srt"
        directory_srt.mkdir()
        self.assertIsNone(self.find([{"file": str(directory_srt)}]))
        wrong_extension = self.provider_temp / "file.ass"
        wrong_extension.touch()
        self.assertIsNone(self.find([{"file": str(wrong_extension)}]))

    def test_rejects_sibling_prefix_and_symlink_escape(self):
        other = self.root / "provider" / "temp-other"
        other.mkdir()
        outside = other / "secret.srt"
        outside.write_text("not a subtitle")
        escaped = self.provider_temp / "link.srt"
        escaped.symlink_to(outside)
        subdirectory = self.provider_temp / "nested"
        subdirectory.symlink_to(other, target_is_directory=True)
        for path in (outside, escaped, subdirectory / outside.name):
            with self.subTest(path=path):
                self.assertIsNone(self.find([{"file": str(path)}]))

    def test_invalid_roots_entries_and_translator_fail_closed(self):
        for entries in (None, {}, "bad", [], [{"file": 123}]):
            self.assertIsNone(self.find(entries))
        for root in ("relative", "http://localhost/temp", "", None):
            self.assertIsNone(subtitle_path([{"file": str(self.valid)}], root, self.translate))
        self.assertIsNone(subtitle_path([{"file": str(self.valid)}], str(self.provider_temp), Mock(side_effect=RuntimeError("secret"))))


if __name__ == "__main__":
    unittest.main()
