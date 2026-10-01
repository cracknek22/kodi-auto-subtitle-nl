import importlib.util
import hashlib
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
ADDON_PATH = ROOT / "kodi-addon" / "service.autosubtranslate.nl"
SERVICE_PATH = ADDON_PATH / "service.py"
DEFAULT_FOLDER = "smb://192.168.2.60/share/subtitles/"


class FakeAddon:
    def __init__(self, profile_path: str, setting: str = DEFAULT_FOLDER):
        self.profile_path = profile_path
        self.setting = setting

    def getAddonInfo(self, key):
        return {
            "id": "service.autosubtranslate.nl",
            "name": "Automatische Nederlandse Ondertitels",
            "path": str(ADDON_PATH),
            "profile": self.profile_path,
        }[key]

    def getSettingString(self, _key):
        return self.setting

    def getSettingBool(self, _key):
        return False


class FakeStat:
    def __init__(self, size: int, mtime: int = 0):
        self._size = size
        self._mtime = mtime

    def st_size(self):
        return self._size

    def st_mtime(self):
        return self._mtime


class FakeFile:
    def __init__(self, vfs, path: str, mode: str):
        self.vfs = vfs
        self.path = path
        self.mode = mode
        self.closed = False
        if "r" in mode and path not in vfs.files:
            raise OSError(f"missing: {path}")
        if "w" in mode:
            vfs.files[path] = ""

    def read(self):
        if self.path in self.vfs.read_errors:
            raise self.vfs.read_errors[self.path]
        return self.vfs.files[self.path]

    def readBytes(self, size=0):
        content = self.read()
        raw = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        return bytearray(raw[:size] if size else raw)

    def write(self, content):
        current = self.vfs.files.get(self.path, "")
        self.vfs.files[self.path] = current + content
        return len(content)

    def close(self):
        self.closed = True
        self.vfs.closed_handles.append(self.path)


class FakeVFS(types.ModuleType):
    def __init__(self, profile_path: str):
        super().__init__("xbmcvfs")
        self.files = {}
        self.dirs = {DEFAULT_FOLDER.rstrip("/"), profile_path}
        self.metadata = {}
        self.stat_errors = {}
        self.read_errors = {}
        self.deleted = []
        self.renamed = []
        self.copied = []
        self.closed_handles = []
        self.fail_rename = False
        self.fail_copy = False
        self.exists_error = None
        self.listdir_error = None

    @staticmethod
    def translatePath(path):
        return path

    def File(self, path, mode):
        return FakeFile(self, path, mode)

    def exists(self, path):
        if self.exists_error is not None:
            raise self.exists_error
        return path in self.files or path.rstrip("/") in self.dirs

    def listdir(self, folder):
        if self.listdir_error is not None:
            raise self.listdir_error
        prefix = folder.rstrip("/") + "/"
        files = sorted(
            path[len(prefix) :]
            for path in self.files
            if path.startswith(prefix) and "/" not in path[len(prefix) :]
        )
        return [], files

    def Stat(self, path):
        if path in self.stat_errors:
            raise self.stat_errors[path]
        if path not in self.files and path not in self.metadata:
            raise OSError(f"missing: {path}")
        if path in self.metadata:
            size, mtime = self.metadata[path]
        else:
            content = self.files[path]
            size = len(content.encode("utf-8") if isinstance(content, str) else content)
            mtime = 0
        return FakeStat(size, mtime)

    def delete(self, path):
        self.deleted.append(path)
        self.files.pop(path, None)
        self.metadata.pop(path, None)
        return True

    def rename(self, source, target):
        self.renamed.append((source, target))
        if self.fail_rename or source not in self.files:
            return False
        self.files[target] = self.files.pop(source)
        if source in self.metadata:
            self.metadata[target] = self.metadata.pop(source)
        return True

    def mkdirs(self, path):
        self.dirs.add(path.rstrip("/"))
        return True

    def copy(self, source, target):
        self.copied.append((source, target))
        if self.fail_copy or source not in self.files:
            return False
        self.files[target] = self.files[source]
        return True


class FakeDialog:
    def __init__(self):
        self.notifications = []
        self.yesno_calls = []
        self.select_calls = []
        self.yesno_result = True
        self.select_result = 0
        self.reject_default_button = False

    def notification(self, *args, **kwargs):
        self.notifications.append((args, kwargs))

    def yesno(self, *args, **kwargs):
        self.yesno_calls.append((args, kwargs))
        if self.reject_default_button and "defaultbutton" in kwargs:
            raise TypeError("legacy Kodi")
        return self.yesno_result

    def select(self, *args, **kwargs):
        self.select_calls.append((args, kwargs))
        return self.select_result


class FakePlayer:
    def __init__(self, video="", subtitle="English (External)", playing=True):
        self.video = video
        self.subtitle = subtitle
        self.playing = playing
        self.loaded_subtitles = []
        self.visibility = []
        self.raise_playing = False
        self.raise_subtitles = False

    def isPlayingVideo(self):
        if self.raise_playing:
            raise RuntimeError("player unavailable")
        return self.playing

    def getPlayingFile(self):
        return self.video

    def getSubtitles(self):
        if self.raise_subtitles:
            raise RuntimeError("subtitle unavailable")
        return self.subtitle

    def setSubtitles(self, path):
        self.loaded_subtitles.append(path)

    def showSubtitles(self, value):
        self.visibility.append(value)


class SequenceMonitor:
    def __init__(self, results):
        self.results = iter(results)

    def waitForAbort(self, _seconds):
        return next(self.results)


def load_service(profile_path: str):
    dialog = FakeDialog()
    vfs = FakeVFS(profile_path)

    xbmc = types.ModuleType("xbmc")
    xbmc.LOGINFO = 1
    xbmc.LOGWARNING = 2
    xbmc.LOGERROR = 3
    xbmc.logs = []
    xbmc.log = lambda *args, **kwargs: xbmc.logs.append((args, kwargs))
    xbmc.sleep = lambda _milliseconds: None
    xbmc.Player = FakePlayer
    xbmc.Monitor = lambda: SequenceMonitor([True])

    addon = FakeAddon(profile_path)
    xbmcaddon = types.ModuleType("xbmcaddon")
    xbmcaddon.Addon = lambda: addon

    xbmcgui = types.ModuleType("xbmcgui")
    xbmcgui.NOTIFICATION_INFO = 1
    xbmcgui.NOTIFICATION_ERROR = 2
    xbmcgui.DLG_YESNO_NO_BTN = 0
    xbmcgui.Dialog = lambda: dialog

    modules = {
        "xbmc": xbmc,
        "xbmcaddon": xbmcaddon,
        "xbmcgui": xbmcgui,
        "xbmcvfs": vfs,
    }
    spec = importlib.util.spec_from_file_location(
        "kodi_service_under_test",
        SERVICE_PATH,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module, vfs, dialog


class KodiServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.profile = tempfile.TemporaryDirectory()
        self.service, self.vfs, self.dialog = load_service(self.profile.name)

    def tearDown(self):
        self.profile.cleanup()


class VfsAndConfigurationTests(KodiServiceTestCase):
    def test_path_and_label_helpers_normalize_untrusted_text(self):
        self.assertEqual(
            self.service.join_vfs("smb://server/subs///", "Movie.en.srt"),
            "smb://server/subs/Movie.en.srt",
        )
        self.assertEqual(
            self.service.parent_vfs("smb://server/subs/Movie.en.srt"),
            "smb://server/subs/",
        )
        self.assertEqual(
            self.service.safe_label(" [COLOR red]  bad\n label ] ", 18),
            "(COLOR red) bad la",
        )

    def test_read_text_handles_utf8_bom_as_bytes_and_text_and_closes(self):
        byte_path = "smb://server/bytes.srt"
        text_path = "smb://server/text.srt"
        self.vfs.files[byte_path] = b"\xef\xbb\xbfEnglish"
        self.vfs.files[text_path] = "\ufeffEnglish"

        self.assertEqual(self.service.read_text(byte_path), "English")
        self.assertEqual(self.service.read_text(text_path), "English")
        self.assertEqual(self.vfs.closed_handles, [byte_path, text_path])

    def test_read_text_closes_handle_when_read_raises(self):
        path = "smb://server/broken.srt"
        self.vfs.files[path] = "content"
        self.vfs.read_errors[path] = UnicodeError("bad encoding")

        with self.assertRaises(UnicodeError):
            self.service.read_text(path)
        self.assertIn(path, self.vfs.closed_handles)

    def test_write_text_is_atomic_and_replaces_existing_destination(self):
        path = os.path.join(self.profile.name, "jobs.json")
        self.vfs.files[path] = "old"

        self.service.write_text(path, "new")
        self.service.write_text(path, "newer")

        self.assertEqual(self.vfs.files[path], "newer")
        self.assertIn(path, self.vfs.deleted)
        temporary_paths = [
            source for source, destination in self.vfs.renamed if destination == path
        ]
        self.assertEqual(len(temporary_paths), 2)
        self.assertNotEqual(temporary_paths[0], temporary_paths[1])
        for temporary in temporary_paths:
            self.assertNotEqual(temporary, f"{path}.tmp")
            self.assertTrue(temporary.startswith(f"{path}."))
            self.assertTrue(temporary.endswith(".tmp"))
            self.assertIn(temporary, self.vfs.closed_handles)

    def test_write_text_cleans_temporary_file_when_rename_fails(self):
        path = os.path.join(self.profile.name, "jobs.json")
        self.vfs.fail_rename = True

        with self.assertRaisesRegex(OSError, "Kon bestand niet opslaan"):
            self.service.write_text(path, "new")

        temporary = self.vfs.renamed[-1][0]
        self.assertNotEqual(temporary, f"{path}.tmp")
        self.assertNotIn(temporary, self.vfs.files)
        self.assertIn(temporary, self.vfs.deleted)

    def test_json_helpers_accept_only_dictionary_documents(self):
        missing = os.path.join(self.profile.name, "missing.json")
        invalid = os.path.join(self.profile.name, "invalid.json")
        array = os.path.join(self.profile.name, "array.json")
        valid = os.path.join(self.profile.name, "valid.json")
        self.vfs.files[invalid] = "{"
        self.vfs.files[array] = "[]"
        self.vfs.files[valid] = '{"naam": "één"}'

        self.assertIsNone(self.service.read_json(missing))
        self.assertIsNone(self.service.read_json(invalid))
        self.assertIsNone(self.service.read_json(array))
        self.assertEqual(self.service.read_json(valid), {"naam": "één"})

        written = os.path.join(self.profile.name, "written.json")
        self.service.write_json(written, {"naam": "één"})
        self.assertEqual(json.loads(self.vfs.files[written]), {"naam": "één"})
        self.assertIn("één", self.vfs.files[written])

    def test_configured_folder_trims_setting_and_supports_legacy_kodi(self):
        self.service.ADDON = FakeAddon(self.profile.name, " smb://box/subs/// ")
        self.assertEqual(self.service.configured_folder(), "smb://box/subs/")

        class LegacyAddon:
            def getSettingString(self, _key):
                raise AttributeError

            def getSetting(self, _key):
                return ""

        self.service.ADDON = LegacyAddon()
        self.assertEqual(self.service.configured_folder(), DEFAULT_FOLDER)

    def test_snapshot_filters_files_records_metadata_and_skips_bad_stat(self):
        folder = DEFAULT_FOLDER
        movie = f"{folder}Movie.en.srt"
        broken = f"{folder}Broken.en.srt"
        self.vfs.files[movie] = "subtitle"
        self.vfs.files[broken] = "broken"
        self.vfs.files[f"{folder}notes.txt"] = "ignore"
        self.vfs.metadata[movie] = (321, 456)
        self.vfs.stat_errors[broken] = RuntimeError("disconnected")

        result = self.service.snapshot(folder)

        self.assertEqual(result, {movie: (321, 456)})
        self.assertTrue(
            any("Broken.en.srt" in args[0] for args, _kwargs in self.service.xbmc.logs)
        )

    def test_snapshot_reports_missing_and_temporary_smb_failures_safely(self):
        with self.assertRaisesRegex(OSError, "niet bereikbaar"):
            self.service.snapshot("smb://missing/subtitles/")

        self.vfs.exists_error = RuntimeError("SMB disconnected")
        with self.assertRaisesRegex(OSError, "tijdelijk niet bereikbaar"):
            self.service.snapshot(DEFAULT_FOLDER)


class TempSubtitleStagingTests(KodiServiceTestCase):
    def setUp(self):
        super().setUp()
        self.vfs.dirs.add(self.service.TEMP_PATH.rstrip("/"))

    def test_stages_a_temp_srt_atomically_under_a_safe_unique_name(self):
        source = f"{self.service.TEMP_PATH}Original.en.srt"
        self.vfs.files[source] = "English subtitle"

        with patch.object(
            self.service.uuid,
            "uuid4",
            return_value=types.SimpleNamespace(hex="1234567890abcdef"),
        ):
            destination = self.service.stage_temp_subtitle(source, DEFAULT_FOLDER)

        self.assertEqual(
            destination,
            f"{DEFAULT_FOLDER}Kodi-1234567890ab-Original.en.srt",
        )
        self.assertEqual(self.vfs.files[destination], "English subtitle")
        temporary, renamed_to = self.vfs.renamed[-1]
        self.assertEqual(renamed_to, destination)
        self.assertTrue(temporary.startswith(f"{destination}."))
        self.assertTrue(temporary.endswith(".tmp"))
        self.assertNotIn(temporary, self.vfs.files)

    def test_rejects_unsafe_oversized_or_colliding_temp_subtitles(self):
        invalid = f"{self.service.TEMP_PATH}Movie.ass"
        self.vfs.files[invalid] = "not an srt"
        with self.assertRaisesRegex(ValueError, "SRT"):
            self.service.stage_temp_subtitle(invalid, DEFAULT_FOLDER)

        oversized = f"{self.service.TEMP_PATH}Huge.en.srt"
        self.vfs.files[oversized] = "subtitle"
        self.vfs.metadata[oversized] = (self.service.MAX_SUBTITLE_BYTES + 1, 1)
        with self.assertRaisesRegex(ValueError, "groter dan 2 MB"):
            self.service.stage_temp_subtitle(oversized, DEFAULT_FOLDER)

        source = f"{self.service.TEMP_PATH}Movie.en.srt"
        self.vfs.files[source] = "subtitle"
        collision = f"{DEFAULT_FOLDER}Kodi-aaaaaaaaaaaa-Movie.en.srt"
        self.vfs.files[collision] = "do not overwrite"
        with (
            patch.object(
                self.service.uuid,
                "uuid4",
                return_value=types.SimpleNamespace(hex="a" * 32),
            ),
            self.assertRaisesRegex(OSError, "bestaat al"),
        ):
            self.service.stage_temp_subtitle(source, DEFAULT_FOLDER)
        self.assertEqual(self.vfs.files[collision], "do not overwrite")

    def test_cleans_partial_files_when_copy_or_rename_fails(self):
        source = f"{self.service.TEMP_PATH}Movie.en.srt"
        self.vfs.files[source] = "subtitle"

        self.vfs.fail_copy = True
        with (
            patch.object(
                self.service.uuid,
                "uuid4",
                return_value=types.SimpleNamespace(hex="b" * 32),
            ),
            self.assertRaisesRegex(OSError, "tijdelijke ondertitel"),
        ):
            self.service.stage_temp_subtitle(source, DEFAULT_FOLDER)
        copy_temporary = self.vfs.copied[-1][1]
        self.assertIn(copy_temporary, self.vfs.deleted)
        self.assertNotIn(copy_temporary, self.vfs.files)

        self.vfs.fail_copy = False
        self.vfs.fail_rename = True
        with (
            patch.object(
                self.service.uuid,
                "uuid4",
                return_value=types.SimpleNamespace(hex="c" * 32),
            ),
            self.assertRaisesRegex(OSError, "server opslaan"),
        ):
            self.service.stage_temp_subtitle(source, DEFAULT_FOLDER)
        rename_temporary = self.vfs.renamed[-1][0]
        self.assertIn(rename_temporary, self.vfs.deleted)
        self.assertNotIn(rename_temporary, self.vfs.files)


class PlayerAndConfirmationTests(KodiServiceTestCase):
    def test_sync_enabled_confirmation_describes_audio_synchronization(self):
        self.service.ADDON.getSettingBool = lambda key: key == "sync_enabled"
        self.service.ask_yes_no("Movie.en.srt", "English", "Hello.")
        self.assertIn("filmaudio synchroniseren", self.dialog.yesno_calls[-1][0][1])

    def test_player_helpers_handle_empty_and_runtime_failures(self):
        player = FakePlayer(video="movie.mkv", subtitle="", playing=True)
        self.assertEqual(self.service.active_subtitle_name(player), "onbekend")
        self.assertEqual(self.service.current_video(player), "movie.mkv")
        self.assertEqual(len(self.service.current_video_fingerprint(player)), 64)

        player.raise_subtitles = True
        self.assertEqual(self.service.active_subtitle_name(player), "onbekend")
        player.raise_playing = True
        self.assertEqual(self.service.current_video(player), "")
        self.assertEqual(self.service.current_video_fingerprint(player), "")

        stopped = FakePlayer(video="movie.mkv", playing=False)
        self.assertEqual(self.service.current_video(stopped), "")

    def test_confirmation_is_sanitized_defaults_to_no_and_has_legacy_fallback(self):
        self.dialog.yesno_result = True

        self.assertTrue(
            self.service.ask_yes_no(
                "[B]Movie.en.srt[/B]",
                "[COLOR red]English[/COLOR]",
                "First\nline",
            )
        )
        args, kwargs = self.dialog.yesno_calls[0]
        self.assertEqual(args[0], "Ondertitel vertalen?")
        self.assertNotIn("[", args[1])
        self.assertEqual(kwargs["defaultbutton"], self.service.xbmcgui.DLG_YESNO_NO_BTN)

        self.dialog.reject_default_button = True
        self.dialog.yesno_result = False
        self.assertFalse(self.service.ask_yes_no("Movie.en.srt", "English", "Preview"))
        self.assertEqual(len(self.dialog.yesno_calls), 3)
        self.assertNotIn("defaultbutton", self.dialog.yesno_calls[-1][1])

    def test_choose_candidate_returns_single_without_reading(self):
        path = "smb://server/subs/Movie.en.srt"
        with patch.object(self.service, "read_text") as read:
            self.assertEqual(self.service.choose_candidate([path]), path)
        read.assert_not_called()

    def test_choose_candidate_skips_unreadable_and_respects_selection(self):
        first = "smb://server/subs/Broken.en.srt"
        second = "smb://server/subs/Movie.en.srt"
        third = "smb://server/subs/Other.en.srt"
        self.vfs.files[second] = (
            "1\n00:00:01,000 --> 00:00:02,000\n<i>First line.</i>\n"
        )
        self.vfs.files[third] = (
            "1\n00:00:01,000 --> 00:00:02,000\nSecond line.\n"
        )
        self.dialog.select_result = 1

        self.assertEqual(
            self.service.choose_candidate([first, second, third]),
            third,
        )
        labels = self.dialog.select_calls[0][0][1]
        self.assertEqual(len(labels), 2)
        self.assertIn("First line.", labels[0])

        self.dialog.select_result = -1
        self.assertIsNone(self.service.choose_candidate([second, third]))
        self.assertIsNone(self.service.choose_candidate([first, "smb://missing.srt"]))


class JobPersistenceAndLoadingTests(KodiServiceTestCase):
    def test_load_jobs_keeps_only_well_formed_records(self):
        valid = {
            "job_id": "job_valid",
            "source": "smb://server/subs/Movie.en.srt",
            "video_fingerprint": "a" * 64,
        }
        empty_fingerprint = {
            "job_id": "job_without_video",
            "source": "smb://server/subs/Other.en.srt",
            "video_fingerprint": "",
        }
        invalid = [
            {"job_id": 1, "source": "a", "video_fingerprint": ""},
            {"job_id": "bad", "source": 1, "video_fingerprint": ""},
            {"job_id": "bad2", "source": "a", "video_fingerprint": "short"},
            "not a job",
        ]
        self.vfs.files[self.service.JOBS_PATH] = json.dumps(
            {"jobs": [valid, empty_fingerprint, *invalid]}
        )

        jobs = self.service.load_jobs()

        self.assertEqual(set(jobs), {"job_valid", "job_without_video"})
        self.assertEqual(jobs["job_valid"], valid)

        self.vfs.files[self.service.JOBS_PATH] = '{"jobs": {}}'
        self.assertEqual(self.service.load_jobs(), {})
        self.vfs.files[self.service.JOBS_PATH] = "{}"
        self.assertEqual(self.service.load_jobs(), {})

    def test_save_and_start_job_persist_without_stream_url_or_token(self):
        jobs = {}
        stream_url = "https://stream.invalid/movie?token=secret"
        fingerprint = self.service.video_fingerprint(stream_url)

        with patch.object(
            self.service.uuid,
            "uuid4",
            return_value=types.SimpleNamespace(hex="1234567890abcdef"),
        ):
            self.service.start_job(
                "smb://server/subtitles/Movie.en.srt",
                fingerprint,
                jobs,
            )

        job_id = "job_1234567890abcdef"
        request_file = (
            "smb://server/subtitles/Movie.en.srt.translate.request.json"
        )
        request_payload = json.loads(self.vfs.files[request_file])
        stored_payload = json.loads(self.vfs.files[self.service.JOBS_PATH])
        self.assertEqual(request_payload["job_id"], job_id)
        self.assertNotIn("video", request_payload)
        self.assertNotIn("secret", str(stored_payload))
        self.assertEqual(jobs[job_id]["video_fingerprint"], fingerprint)
        self.assertTrue(self.dialog.notifications)

    def _completed_job(self, fingerprint):
        job = {
            "job_id": "job_12345678",
            "source": "smb://server/subtitles/Movie.en.srt",
            "video_fingerprint": fingerprint,
        }
        status = {
            "version": 1,
            "job_id": job["job_id"],
            "state": "complete",
            "source": "Movie.en.srt",
            "output": "Movie.nl.srt",
        }
        return job, status

    def test_forged_output_name_is_rejected_before_loading(self):
        job, status = self._completed_job("a" * 64)
        status["output"] = "Other.nl.srt"

        with self.assertRaisesRegex(ValueError, "uitvoernaam"):
            self.service.output_path(job, status)

    def test_completed_subtitle_loads_for_the_same_video(self):
        video = "smb://server/movies/Movie.mkv"
        fingerprint = self.service.video_fingerprint(video)
        player = FakePlayer(video=video)
        job, status = self._completed_job(fingerprint)
        remote = "smb://server/subtitles/Movie.nl.srt"
        local = os.path.join(
            self.service.TRANSLATED_PATH,
            f"{job['job_id']}.nl.srt",
        )
        self.vfs.files[remote] = "Dutch subtitle"
        self.vfs.files[local] = "stale local copy"

        self.service.load_completed_subtitle(player, job, status)

        self.assertEqual(self.vfs.files[local], "Dutch subtitle")
        self.assertIn(local, self.vfs.deleted)
        self.assertEqual(player.loaded_subtitles, [local])
        self.assertEqual(player.visibility, [True])
        self.assertIn("geladen", self.dialog.notifications[-1][0][1])

    def test_completed_subtitle_for_a_different_video_is_only_saved(self):
        player = FakePlayer(video="different.mkv")
        job, status = self._completed_job("a" * 64)
        remote = "smb://server/subtitles/Movie.nl.srt"
        self.vfs.files[remote] = "Dutch subtitle"

        self.service.load_completed_subtitle(player, job, status)

        self.assertEqual(player.loaded_subtitles, [])
        self.assertIn("server opgeslagen", self.dialog.notifications[-1][0][1])

    def test_completed_subtitle_rejects_missing_oversize_and_copy_failure(self):
        player = FakePlayer(video="movie.mkv")
        job, status = self._completed_job("")
        remote = "smb://server/subtitles/Movie.nl.srt"

        with self.assertRaisesRegex(OSError, "ontbreekt"):
            self.service.load_completed_subtitle(player, job, status)

        self.vfs.files[remote] = "subtitle"
        self.vfs.metadata[remote] = (self.service.MAX_SUBTITLE_BYTES + 1, 0)
        with self.assertRaisesRegex(ValueError, "groter dan 2 MB"):
            self.service.load_completed_subtitle(player, job, status)

        self.vfs.metadata[remote] = (8, 0)
        self.vfs.fail_copy = True
        with self.assertRaisesRegex(OSError, "niet lokaal kopiëren"):
            self.service.load_completed_subtitle(player, job, status)


class SynchronizedJobTests(KodiServiceTestCase):
    def setUp(self):
        super().setUp()
        self.source = DEFAULT_FOLDER + "Movie.en.srt"
        self.raw = b"\xef\xbb\xbf1\r\n00:00:01,000 --> 00:00:02,000\r\nEnglish.\r\n"
        self.vfs.files[self.source] = self.raw
        self.digest = hashlib.sha256(self.raw).hexdigest()
        self.player = FakePlayer(video="https://cdn.example/movie?token=private|Cookie=session%3Dprivate")
        self.fingerprint = self.service.current_video_fingerprint(self.player)
        self.jobs = {}
        self.settings = {"sync_server": "https://192.168.2.60:8766", "sync_cert_sha256": "a" * 64, "sync_token": "private-sync-token-" + "b" * 32}
        self.service.ADDON.getSettingBool = lambda key: key == "sync_enabled"
        self.service.ADDON.getSettingString = lambda key: self.settings.get(key, DEFAULT_FOLDER)

    def start(self, approved=None):
        return self.service.start_job(self.source, self.fingerprint, self.jobs, player=self.player, approved_sha256=self.digest if approved is None else approved)

    def test_registers_raw_byte_digest_before_writing_request_and_keeps_secrets_private(self):
        calls = []
        def register(_server, _pin, _token, payload):
            self.assertFalse(any(p.endswith(".translate.request.json") for p in self.vfs.files))
            calls.append(payload)
        with patch.object(self.service.sync_client, "register_reference", side_effect=register):
            self.start()
        self.assertEqual(calls[0]["source_sha256"], self.digest)
        self.assertEqual(calls[0]["url"], "https://cdn.example/movie?token=private")
        self.assertEqual(calls[0]["headers"], {"Cookie": "session=private"})
        request = json.loads(self.vfs.files[self.service.request_path(self.source)])
        self.assertEqual(request["version"], 2)
        self.assertEqual(request["sync"], {"required": True, "source_sha256": self.digest})
        self.assertEqual(request["job_id"], calls[0]["job_id"])
        self.assertNotIn("private", str(self.vfs.files))
        self.assertNotIn("private", str(self.jobs))
        self.assertIn("synchroniseren", self.dialog.notifications[-1][0][1])

    def test_registration_failure_does_not_create_unsynchronized_fallback(self):
        with patch.object(self.service.sync_client, "register_reference", side_effect=ValueError("synchronisatie mislukt")), self.assertRaises(ValueError):
            self.start()
        self.assertFalse(any(p.endswith(".translate.request.json") for p in self.vfs.files))
        self.assertEqual(self.jobs, {})

    def test_changed_source_or_unresolved_video_is_rejected_before_registration(self):
        with patch.object(self.service.sync_client, "register_reference") as register:
            with self.assertRaises(ValueError):
                self.start(approved="c" * 64)
            self.player.video = "plugin://unresolved"
            self.fingerprint = self.service.current_video_fingerprint(self.player)
            with self.assertRaises(ValueError):
                self.start()
            register.assert_not_called()
        self.assertEqual(self.jobs, {})

    def test_source_change_while_registering_cannot_create_request(self):
        def changed(*_args):
            self.vfs.files[self.source] = self.raw.replace(b"English", b"Changed")
        with patch.object(self.service.sync_client, "register_reference", side_effect=changed), self.assertRaises(ValueError):
            self.start()
        self.assertFalse(any(p.endswith(".translate.request.json") for p in self.vfs.files))

    def test_queued_playback_restart_after_https_cannot_create_request(self):
        pending = []
        self.player.playback_generation = 0
        def register(*_args):
            pending.append(True)
        def pump(_milliseconds):
            if pending:
                pending.clear()
                self.player.playback_generation += 1
        self.service.xbmc.sleep = pump
        with patch.object(self.service.sync_client, "register_reference", side_effect=register), self.assertRaises(ValueError):
            self.start()
        self.assertFalse(any(p.endswith(".translate.request.json") for p in self.vfs.files))

    def test_sync_enabled_requires_player_and_approved_source(self):
        with patch.object(self.service.sync_client, "register_reference") as register:
            with self.assertRaises(ValueError):
                self.service.start_job(self.source, self.fingerprint, self.jobs)
            with self.assertRaises(ValueError):
                self.service.start_job(self.source, self.fingerprint, self.jobs, player=self.player)
            register.assert_not_called()

    def test_synchronized_job_rejects_version_one_complete_downgrade(self):
        job = {"source": self.source, "sync_required": True}
        status = {"version": 1, "state": "complete", "source": "Movie.en.srt", "output": "Movie.nl.srt"}
        with self.assertRaisesRegex(ValueError, "synchronisatie"):
            self.service.output_path(job, status)


class PollingTests(KodiServiceTestCase):
    @staticmethod
    def job(job_id="job_one"):
        return {
            "job_id": job_id,
            "source": f"smb://server/subtitles/{job_id}.en.srt",
            "video_fingerprint": "",
        }

    def test_pending_missing_and_mismatched_statuses_do_not_change_jobs(self):
        jobs = {
            "job_one": self.job("job_one"),
            "job_two": self.job("job_two"),
        }
        statuses = iter([None, {"job_id": "another", "state": "complete"}])
        with (
            patch.object(self.service, "read_json", side_effect=lambda _path: next(statuses)),
            patch.object(self.service, "save_jobs") as save,
        ):
            self.assertFalse(self.service.poll_jobs(FakePlayer(), jobs))

        self.assertEqual(set(jobs), {"job_one", "job_two"})
        save.assert_not_called()

    def test_complete_job_is_loaded_removed_and_persisted(self):
        job = self.job()
        jobs = {"job_one": job}
        status = {"job_id": "job_one", "state": "complete"}
        with (
            patch.object(self.service, "read_json", return_value=status),
            patch.object(self.service, "load_completed_subtitle") as load,
            patch.object(self.service, "save_jobs") as save,
        ):
            changed = self.service.poll_jobs(FakePlayer(), jobs)

        self.assertTrue(changed)
        self.assertEqual(jobs, {})
        load.assert_called_once()
        save.assert_called_once_with({})

    def test_complete_job_validation_error_is_reported_and_job_is_removed(self):
        jobs = {"job_one": self.job()}
        status = {"job_id": "job_one", "state": "complete"}
        with (
            patch.object(self.service, "read_json", return_value=status),
            patch.object(
                self.service,
                "load_completed_subtitle",
                side_effect=ValueError("[bad] remote"),
            ),
            patch.object(self.service, "save_jobs") as save,
        ):
            self.assertTrue(self.service.poll_jobs(FakePlayer(), jobs))

        self.assertEqual(jobs, {})
        self.assertIn("(bad) remote", self.dialog.notifications[-1][0][1])
        save.assert_called_once_with({})

    def test_temporary_completed_load_error_retries_after_delay_without_retranslation(self):
        jobs = {"job_one": self.job()}
        status = {"job_id": "job_one", "state": "complete"}
        for failure in (OSError("private SMB failure"), RuntimeError("private player failure")):
            with self.subTest(failure=type(failure).__name__):
                jobs = {"job_one": self.job()}
                self.dialog.notifications.clear()
                with (
                    patch.object(self.service, "read_json", return_value=status),
                    patch.object(self.service, "load_completed_subtitle", side_effect=[failure, None]) as load,
                    patch.object(self.service, "save_jobs") as save,
                    patch.object(self.service, "start_job") as translate,
                    patch.object(self.service.time, "time", return_value=100) as clock,
                ):
                    self.assertTrue(self.service.poll_jobs(FakePlayer(), jobs))
                    self.assertIn("job_one", jobs)
                    self.assertEqual(jobs["job_one"]["load_attempts"], 1)
                    self.assertEqual(jobs["job_one"]["load_retry_at"], 105)
                    self.assertEqual(len(self.dialog.notifications), 1)
                    self.assertNotIn("private", str(self.dialog.notifications))
                    clock.return_value = 104
                    self.assertFalse(self.service.poll_jobs(FakePlayer(), jobs))
                    self.assertEqual(load.call_count, 1)
                    self.assertEqual(len(self.dialog.notifications), 1)
                    clock.return_value = 105
                    self.assertTrue(self.service.poll_jobs(FakePlayer(), jobs))
                    self.assertEqual(load.call_count, 2)
                    self.assertEqual(jobs, {})
                    self.assertEqual(save.call_count, 2)
                    translate.assert_not_called()

    def test_completed_load_has_five_total_attempts_and_only_initial_and_final_popups(self):
        jobs = {"job_one": self.job()}
        status = {"job_id": "job_one", "state": "complete"}
        with (
            patch.object(self.service, "read_json", return_value=status),
            patch.object(self.service, "load_completed_subtitle", side_effect=OSError("private SMB failure")) as load,
            patch.object(self.service, "save_jobs") as save,
            patch.object(self.service.time, "time", return_value=100) as clock,
        ):
            for attempt in range(1, 6):
                clock.return_value = 100 + (attempt - 1) * 5
                self.assertTrue(self.service.poll_jobs(FakePlayer(), jobs))
                self.assertEqual(load.call_count, attempt)
                if attempt < 5:
                    self.assertEqual(jobs["job_one"]["load_attempts"], attempt)
                    self.assertEqual(len(self.dialog.notifications), 1)
            self.assertEqual(jobs, {})
            self.assertEqual(len(self.dialog.notifications), 2)
            self.assertIn("server", self.dialog.notifications[-1][0][1])
            self.assertNotIn("private", str(self.dialog.notifications))
            self.assertEqual(save.call_count, 5)
            self.assertFalse(self.service.poll_jobs(FakePlayer(), jobs))
            self.assertEqual(load.call_count, 5)

    def test_completed_copy_retry_survives_jobs_reload_and_loads_the_existing_srt(self):
        video = "movie.mkv"
        player = FakePlayer(video=video)
        job = self.job()
        job["video_fingerprint"] = self.service.video_fingerprint(video)
        jobs = {"job_one": job}
        status = {"version": 1, "job_id": "job_one", "state": "complete",
                  "source": "job_one.en.srt", "output": "job_one.nl.srt"}
        remote = "smb://server/subtitles/job_one.nl.srt"
        original = "1\n00:00:01,000 --> 00:00:02,000\nHallo.\n"
        self.vfs.files[remote] = original
        self.vfs.files[self.service.status_path(job["source"])] = json.dumps(status)
        self.vfs.fail_copy = True
        with patch.object(self.service.time, "time", return_value=100) as clock:
            self.assertTrue(self.service.poll_jobs(player, jobs))
            jobs = self.service.load_jobs()
            self.assertEqual(jobs["job_one"]["load_attempts"], 1)
            self.assertEqual(jobs["job_one"]["load_retry_at"], 105)
            self.vfs.fail_copy = False
            clock.return_value = 104
            self.assertFalse(self.service.poll_jobs(player, jobs))
            self.assertEqual(player.loaded_subtitles, [])
            clock.return_value = 105
            self.assertTrue(self.service.poll_jobs(player, jobs))
        self.assertEqual(jobs, {})
        self.assertEqual(self.service.load_jobs(), {})
        self.assertEqual(len(player.loaded_subtitles), 1)
        self.assertEqual(self.vfs.files[player.loaded_subtitles[0]], original)
        self.assertEqual(self.vfs.files[remote], original)
        self.assertEqual(player.visibility, [True])
        self.assertFalse(any(path.endswith(".translate.request.json") for path in self.vfs.files))

    def test_validation_failure_after_a_temporary_error_is_not_retried(self):
        jobs = {"job_one": {**self.job(), "load_attempts": 1, "load_retry_at": 105}}
        status = {"job_id": "job_one", "state": "complete"}
        with (
            patch.object(self.service, "read_json", return_value=status),
            patch.object(self.service, "load_completed_subtitle", side_effect=ValueError("ongeldige uitvoernaam")) as load,
            patch.object(self.service, "save_jobs") as save,
            patch.object(self.service.time, "time", return_value=105),
        ):
            self.assertTrue(self.service.poll_jobs(FakePlayer(), jobs))
        self.assertEqual(jobs, {})
        load.assert_called_once()
        save.assert_called_once_with({})

    def test_invalid_persisted_retry_values_do_not_block_or_crash_loading(self):
        status = {"job_id": "job_one", "state": "complete"}
        for attempts, retry_at in (("bad", None), (-1, float("nan")), (False, "later"), (1, 9999)):
            with self.subTest(attempts=attempts, retry_at=retry_at):
                jobs = {"job_one": {**self.job(), "load_attempts": attempts, "load_retry_at": retry_at}}
                with (
                    patch.object(self.service, "read_json", return_value=status),
                    patch.object(self.service, "load_completed_subtitle") as load,
                    patch.object(self.service, "save_jobs"),
                    patch.object(self.service.time, "time", return_value=100),
                ):
                    self.assertTrue(self.service.poll_jobs(FakePlayer(), jobs))
                    load.assert_called_once()
                self.assertEqual(jobs, {})

    def test_exhausted_persisted_retry_does_not_make_a_sixth_load_attempt(self):
        jobs = {"job_one": {**self.job(), "load_attempts": 5, "load_retry_at": 105}}
        status = {"job_id": "job_one", "state": "complete"}
        with (
            patch.object(self.service, "read_json", return_value=status),
            patch.object(self.service, "load_completed_subtitle") as load,
            patch.object(self.service, "save_jobs") as save,
            patch.object(self.service.time, "time", return_value=105),
        ):
            self.assertTrue(self.service.poll_jobs(FakePlayer(), jobs))
        load.assert_not_called()
        self.assertEqual(jobs, {})
        self.assertEqual(len(self.dialog.notifications), 1)
        save.assert_called_once_with({})

    def test_failed_job_shows_sanitized_message_and_is_removed(self):
        jobs = {"job_one": self.job()}
        status = {
            "job_id": "job_one",
            "state": "failed",
            "message": "[COLOR red]Model failed[/COLOR]",
        }
        with (
            patch.object(self.service, "read_json", return_value=status),
            patch.object(self.service, "save_jobs") as save,
        ):
            self.assertTrue(self.service.poll_jobs(FakePlayer(), jobs))

        self.assertEqual(jobs, {})
        self.assertNotIn("[", self.dialog.notifications[-1][0][1])
        save.assert_called_once_with({})


class MainLoopTests(KodiServiceTestCase):
    def test_synchronized_manual_and_temp_flows_register_only_after_yes(self):
        for from_temp in (False, True):
            for confirmed in (False, True):
                with self.subTest(from_temp=from_temp, confirmed=confirmed):
                    self.service, self.vfs, self.dialog = load_service(self.profile.name)
                    source = (self.service.TEMP_PATH if from_temp else DEFAULT_FOLDER) + "Movie.en.srt"
                    raw = b"\xef\xbb\xbf1\r\n00:00:01,000 --> 00:00:02,000\r\nHello.\r\n"
                    self.vfs.files[source] = raw
                    signature = (len(raw), 1)
                    self.vfs.metadata[source] = signature
                    player = FakePlayer(video="https://cdn.example/movie?token=private")
                    self.service.PlaybackPlayer = lambda: player
                    self.service.xbmc.Monitor = lambda: SequenceMonitor([False, False, True])
                    self.service.ADDON.getSettingBool = lambda key: key == "sync_enabled"
                    self.dialog.yesno_result = confirmed
                    remote = iter([{}, {}, {}] if from_temp else [{}, {source: signature}, {source: signature}])
                    temporary = iter([{}, {source: signature}, {source: signature}] if from_temp else [{}, {}, {}])
                    def register(_server, _pin, _token, payload):
                        self.assertEqual(len(self.dialog.yesno_calls), 1)
                        self.assertEqual(payload["source_sha256"], hashlib.sha256(raw).hexdigest())
                    with (
                        patch.object(self.service, "snapshot", side_effect=lambda _folder: next(remote)),
                        patch.object(self.service, "snapshot_kodi_temp", side_effect=lambda: next(temporary)),
                        patch.object(self.service.sync_client, "register_reference", side_effect=register) as registration,
                    ):
                        self.service.main()
                    requests = [p for p in self.vfs.files if p.endswith(".translate.request.json")]
                    self.assertEqual(len(requests), int(confirmed))
                    self.assertEqual(registration.call_count, int(confirmed))
                    if confirmed:
                        self.assertEqual(json.loads(self.vfs.files[requests[0]])["version"], 2)
                    self.assertNotIn("private", str(self.vfs.files))

    def test_main_reports_initial_unreachable_folder_and_stops_cleanly(self):
        self.service.xbmc.Monitor = lambda: SequenceMonitor([True])
        with patch.object(
            self.service,
            "snapshot",
            side_effect=OSError("share offline"),
        ):
            self.service.main()

        self.assertIn(self.service.PROFILE_PATH.rstrip("/"), self.vfs.dirs)
        self.assertIn(self.service.TRANSLATED_PATH.rstrip("/"), self.vfs.dirs)
        self.assertIn("share offline", self.dialog.notifications[0][0][1])
        self.assertTrue(
            any("Service gestopt" in args[0] for args, _kwargs in self.service.xbmc.logs)
        )

    def test_main_handles_status_error_and_folder_change(self):
        player = FakePlayer(playing=True)
        self.service.PlaybackPlayer = lambda: player
        self.service.xbmc.Monitor = lambda: SequenceMonitor([False, True])
        folders = iter(["smb://old/subs/", "smb://new/subs/"])
        with (
            patch.object(self.service, "configured_folder", side_effect=lambda: next(folders)),
            patch.object(
                self.service,
                "snapshot",
                side_effect=[{}, OSError("new share offline")],
            ),
            patch.object(
                self.service,
                "poll_jobs",
                side_effect=RuntimeError("status unavailable"),
            ),
        ):
            self.service.main()

        self.assertTrue(
            any("Tijdelijke fout" in args[0] for args, _kwargs in self.service.xbmc.logs)
        )
        self.assertTrue(
            any("map gewijzigd" in args[0] for args, _kwargs in self.service.xbmc.logs)
        )

    def test_main_refreshes_baseline_while_no_video_is_playing(self):
        player = FakePlayer(playing=False)
        self.service.PlaybackPlayer = lambda: player
        self.service.xbmc.Monitor = lambda: SequenceMonitor([False, True])
        snapshots = iter([{}, {"smb://server/subs/A.en.srt": (10, 1)}])
        with (
            patch.object(self.service, "snapshot", side_effect=lambda _folder: next(snapshots)),
            patch.object(self.service, "poll_jobs", return_value=False),
        ):
            self.service.main()

    def test_main_confirmed_stable_candidate_starts_job(self):
        source = f"{DEFAULT_FOLDER}Movie.en.srt"
        self.vfs.files[source] = (
            "1\n00:00:01,000 --> 00:00:02,000\nTranslate this.\n"
        )
        self.vfs.metadata[source] = (53, 100)
        player = FakePlayer(video="smb://movies/Movie.mkv", playing=True)
        self.service.PlaybackPlayer = lambda: player
        self.service.xbmc.Monitor = lambda: SequenceMonitor([False, False, True])
        snapshots = iter([{}, {source: (53, 100)}, {source: (53, 100)}])
        self.dialog.yesno_result = True

        with (
            patch.object(self.service, "snapshot", side_effect=lambda _folder: next(snapshots)),
            patch.object(self.service, "poll_jobs", return_value=False),
        ):
            self.service.main()

        requests = [
            path
            for path in self.vfs.files
            if path.endswith(".translate.request.json")
        ]
        self.assertEqual(requests, [f"{source}.translate.request.json"])
        self.assertIn(self.service.JOBS_PATH, self.vfs.files)
        self.assertEqual(len(self.dialog.yesno_calls), 1)

    def test_main_stages_a_stable_kodi_temp_srt_and_starts_job(self):
        temp_source = "special://temp/Original.en.srt"
        self.vfs.files[temp_source] = (
            "1\n00:00:01,000 --> 00:00:02,000\nTranslate this.\n"
        )
        self.vfs.metadata[temp_source] = (53, 100)
        player = FakePlayer(video="plugin://movie/secret-token", playing=True)
        self.service.PlaybackPlayer = lambda: player
        self.service.xbmc.Monitor = lambda: SequenceMonitor([False, False, True])
        remote_snapshots = iter([{}, {}, {}])
        temp_snapshots = iter(
            [
                {},
                {temp_source: (53, 100)},
                {temp_source: (53, 100)},
            ]
        )
        uuids = iter(
            [
                types.SimpleNamespace(hex="1111111111111111"),
                types.SimpleNamespace(hex="2222222222222222"),
                types.SimpleNamespace(hex="3333333333333333"),
                types.SimpleNamespace(hex="4444444444444444"),
            ]
        )
        self.dialog.yesno_result = True

        with (
            patch.object(
                self.service,
                "snapshot",
                side_effect=lambda _folder: next(remote_snapshots),
            ),
            patch.object(
                self.service,
                "snapshot_kodi_temp",
                side_effect=lambda: next(temp_snapshots),
            ),
            patch.object(
                self.service.uuid,
                "uuid4",
                side_effect=lambda: next(uuids),
            ),
            patch.object(self.service, "poll_jobs", return_value=False),
        ):
            self.service.main()

        staged = (
            f"{DEFAULT_FOLDER}Kodi-111111111111-Original.en.srt"
        )
        self.assertIn(staged, self.vfs.files)
        self.assertIn(f"{staged}.translate.request.json", self.vfs.files)
        self.assertNotIn("secret-token", str(self.vfs.files))
        self.assertEqual(len(self.dialog.yesno_calls), 1)

    def test_main_marks_candidates_handled_when_dutch_is_already_active(self):
        source = f"{DEFAULT_FOLDER}Movie.en.srt"
        player = FakePlayer(video="movie.mkv", subtitle="Nederlands", playing=True)
        self.service.PlaybackPlayer = lambda: player
        self.service.xbmc.Monitor = lambda: SequenceMonitor(
            [False, False, False, True]
        )
        snapshots = iter(
            [
                {},
                {source: (10, 1)},
                {source: (10, 1)},
                {source: (10, 1)},
            ]
        )
        with (
            patch.object(self.service, "snapshot", side_effect=lambda _folder: next(snapshots)),
            patch.object(self.service, "poll_jobs", return_value=False),
            patch.object(self.service, "start_job") as start,
        ):
            self.service.main()

        start.assert_not_called()
        self.assertEqual(self.dialog.yesno_calls, [])

    def test_main_reports_oversized_source_without_starting_job(self):
        source = f"{DEFAULT_FOLDER}Huge.en.srt"
        self.vfs.files[source] = "subtitle"
        self.vfs.metadata[source] = (self.service.MAX_SUBTITLE_BYTES + 1, 1)
        player = FakePlayer(video="movie.mkv", playing=True)
        self.service.PlaybackPlayer = lambda: player
        self.service.xbmc.Monitor = lambda: SequenceMonitor([False, False, True])
        snapshots = iter(
            [
                {},
                {source: (self.service.MAX_SUBTITLE_BYTES + 1, 1)},
                {source: (self.service.MAX_SUBTITLE_BYTES + 1, 1)},
            ]
        )
        with (
            patch.object(self.service, "snapshot", side_effect=lambda _folder: next(snapshots)),
            patch.object(self.service, "poll_jobs", return_value=False),
            patch.object(self.service, "start_job") as start,
        ):
            self.service.main()

        start.assert_not_called()
        self.assertIn("groter dan 2 MB", self.dialog.notifications[-1][0][1])


if __name__ == "__main__":
    unittest.main()
