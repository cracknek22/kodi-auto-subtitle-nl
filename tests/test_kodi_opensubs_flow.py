import json
import os
from unittest.mock import patch

from test_kodi_service import (
    DEFAULT_FOLDER, FakeAddon, FakePlayer, KodiServiceTestCase, SequenceMonitor,
)


class OpenSubtitlesFlowTests(KodiServiceTestCase):
    def setUp(self):
        super().setUp()
        self.player = FakePlayer(video="https://video.invalid/movie?token=private")
        self.source = os.path.join(self.profile.name, "provider", "temp", "English.srt")
        self.content = "1\r\n00:00:01,123 --> 00:00:03,456\r\n<i>Let's go.</i>\r\n"
        self.vfs.files[self.source] = self.content
        self.jobs = {}
        self.baseline = {}
        self.service.xbmc.getCondVisibility = lambda _condition: True
        self.service.xbmc.getInfoLabel = lambda _label: "Example Movie"
        self.service.xbmc.executeJSONRPC = lambda _request: "{}"
        self.service.xbmcaddon.Addon = lambda _id: FakeAddon(
            os.path.join(self.profile.name, "provider")
        )
        self.listing = self.enter_patch(
            patch.object(self.service.opensubs, "directory_files", return_value=[])
        )
        self.enter_patch(patch.object(
            self.service.opensubs, "download_url", return_value="plugin://safe-download"
        ))
        self.enter_patch(patch.object(
            self.service.opensubs, "subtitle_path", return_value=self.source
        ))

    def enter_patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def run_search(self):
        self.service.search_opensubs(
            self.player, DEFAULT_FOLDER, self.jobs, self.baseline
        )

    def request_files(self):
        return [p for p in self.vfs.files if p.endswith(".translate.request.json")]

    def test_yes_submits_original_srt_once_with_preview_and_no_video_url(self):
        self.run_search()
        requests = self.request_files()
        self.assertEqual(len(requests), 1)
        source = requests[0].removesuffix(".translate.request.json")
        self.assertEqual(self.vfs.files[source], self.content)
        self.assertIn(source, self.baseline)
        message = self.dialog.yesno_calls[0][0][1]
        self.assertIn("Let's go.", message)
        self.assertIn("OpenSubtitles", message)
        self.assertIn("Example Movie", message)
        self.assertNotIn("private", str(self.vfs.files))
        self.assertEqual(self.listing.call_count, 2)
        payload = json.loads(self.vfs.files[requests[0]])
        self.assertEqual(payload["source"], source.rsplit("/", 1)[-1])

    def test_no_never_copies_or_submits(self):
        self.dialog.yesno_result = False
        self.run_search()
        self.assertFalse(self.request_files())
        self.assertFalse(self.vfs.copied)
        self.assertEqual(self.jobs, {})

    def test_video_change_after_search_does_not_download_or_prompt(self):
        def changed(*_args):
            self.player.video = "next.mkv"
            return []
        self.listing.side_effect = changed
        self.run_search()
        self.assertEqual(self.listing.call_count, 1)
        self.assertFalse(self.dialog.yesno_calls)
        self.assertFalse(self.request_files())

    def test_video_change_after_download_does_not_prompt(self):
        def changed(*_args):
            if self.listing.call_count == 2:
                self.player.video = "next.mkv"
            return []
        self.listing.side_effect = changed
        self.run_search()
        self.assertFalse(self.dialog.yesno_calls)
        self.assertFalse(self.request_files())

    def test_queued_kodi_restart_callback_is_processed_before_using_search_result(self):
        pending = []
        self.player.playback_generation = 0
        def search(*_args):
            pending.append("restart")
            return []
        def pump(_milliseconds):
            if pending:
                pending.clear()
                self.player.playback_generation += 1
        self.listing.side_effect = search
        self.service.xbmc.sleep = pump
        self.run_search()
        self.assertEqual(self.listing.call_count, 1)
        self.assertFalse(self.dialog.yesno_calls)
        self.assertFalse(self.request_files())

    def test_stop_or_restart_during_confirmation_never_submits(self):
        for restart in (False, True):
            with self.subTest(restart=restart):
                self.player.playing = True
                self.player.playback_generation = 1
                def changed(*_args, **_kwargs):
                    self.player.playback_generation += 1
                    self.player.playing = restart
                    return True
                with patch.object(self.dialog, "yesno", side_effect=changed):
                    self.run_search()
                self.assertFalse(self.request_files())
                self.assertFalse(self.vfs.copied)

    def test_changed_subtitle_after_confirmation_is_not_submitted(self):
        def changed(*_args, **_kwargs):
            self.vfs.files[self.source] = self.content.replace("Let's go.", "Wrong movie.")
            return True
        with patch.object(self.dialog, "yesno", side_effect=changed):
            self.run_search()
        self.assertFalse(self.request_files())
        self.assertFalse(self.vfs.copied)

    def test_changed_copy_is_not_submitted_or_left_for_folder_watcher(self):
        original_copy = self.vfs.copy
        def changed(source, target):
            result = original_copy(source, target)
            self.vfs.files[target] = self.content.replace("Let's go.", "Wrong movie.")
            return result
        with patch.object(self.vfs, "copy", side_effect=changed):
            self.run_search()
        self.assertFalse(self.request_files())
        self.assertFalse([p for p in self.vfs.files if p.startswith(DEFAULT_FOLDER)])

    def test_unavailable_provider_and_errors_do_not_leak_details(self):
        self.service.xbmc.getCondVisibility = lambda _condition: False
        self.run_search()
        self.listing.assert_not_called()
        self.service.xbmc.getCondVisibility = lambda _condition: True
        self.listing.side_effect = RuntimeError("password=private-secret")
        self.run_search()
        self.assertFalse(self.request_files())
        self.assertNotIn("private-secret", str(self.service.xbmc.logs))
        self.assertNotIn("private-secret", str(self.dialog.notifications))

    def test_no_match_or_unusable_file_does_not_prompt(self):
        with patch.object(self.service.opensubs, "download_url", return_value=None):
            self.run_search()
        self.assertEqual(self.listing.call_count, 1)
        with patch.object(self.service.opensubs, "subtitle_path", return_value=None):
            self.run_search()
        self.assertFalse(self.dialog.yesno_calls)
        self.assertFalse(self.request_files())

    def test_empty_oversized_and_invalid_srt_are_rejected_before_prompt(self):
        for content in ("", "x" * (self.service.MAX_SUBTITLE_BYTES + 1), "<html>Error</html>"):
            with self.subTest(size=len(content)):
                self.vfs.files[self.source] = content
                self.run_search()
        self.assertFalse(self.dialog.yesno_calls)
        self.assertFalse(self.request_files())


class AutomaticSearchSchedulingTests(KodiServiceTestCase):
    def setUp(self):
        super().setUp()
        self.player = FakePlayer(video="movie.mkv")
        self.service.ADDON.getSettingBool = lambda _key: True
        self.service.xbmc.getCondVisibility = lambda _condition: False

    def test_search_waits_for_metadata_and_runs_once_even_if_cancelled(self):
        with (
            patch.object(self.service.time, "monotonic", return_value=0) as now,
            patch.object(self.service, "search_opensubs") as search,
        ):
            state = self.service.AutomaticSearch()
            for timestamp in (0, 1, 4, 5, 6, 20):
                now.return_value = timestamp
                state.run_if_due(self.player, DEFAULT_FOLDER, {}, {})
            self.assertEqual(search.call_count, 1)
            self.player.video = "next.mkv"
            state.run_if_due(self.player, DEFAULT_FOLDER, {}, {})
            now.return_value = 25
            state.run_if_due(self.player, DEFAULT_FOLDER, {}, {})
            self.assertEqual(search.call_count, 2)

    def test_disabled_dutch_active_pending_job_or_manual_candidate_prevents_search(self):
        for mode in ("disabled", "dutch", "pending", "manual"):
            with (
                self.subTest(mode=mode),
                patch.object(self.service.time, "monotonic", return_value=0) as now,
                patch.object(self.service, "search_opensubs") as search,
            ):
                self.player.subtitle = "Dutch" if mode == "dutch" else "English"
                self.service.ADDON.getSettingBool = lambda _key: mode != "disabled"
                jobs = {"job": {"video_fingerprint": self.service.current_video_fingerprint(self.player)}} if mode == "pending" else {}
                state = self.service.AutomaticSearch()
                state.observe(self.player)
                if mode == "manual":
                    state.skip_current(self.player)
                now.return_value = 10
                state.run_if_due(self.player, DEFAULT_FOLDER, jobs, {})
                search.assert_not_called()

    def test_native_search_window_defers_automatic_provider_call(self):
        with (
            patch.object(self.service.time, "monotonic", return_value=0) as now,
            patch.object(self.service, "search_opensubs") as search,
        ):
            state = self.service.AutomaticSearch()
            state.observe(self.player)
            now.return_value = 10
            self.service.xbmc.getCondVisibility = lambda _condition: True
            state.run_if_due(self.player, DEFAULT_FOLDER, {}, {})
            search.assert_not_called()
            self.service.xbmc.getCondVisibility = lambda _condition: False
            state.run_if_due(self.player, DEFAULT_FOLDER, {}, {})
            search.assert_called_once()

    def test_main_runs_auto_flow_and_does_not_prompt_again_for_its_staged_file(self):
        self.service.PlaybackPlayer = lambda: self.player
        self.service.xbmc.Monitor = lambda: SequenceMonitor([False] * 4 + [True])
        calls = []
        def search(_player, folder, _jobs, baseline):
            calls.append(True)
            source = folder + "OpenSubtitles.en.srt"
            self.vfs.files[source] = "downloaded source"
            baseline[source] = (len("downloaded source"), 0)
        with (
            patch.object(self.service.time, "monotonic", side_effect=[0, 6, 7, 8]),
            patch.object(self.service, "search_opensubs", side_effect=search),
        ):
            self.service.main()
        self.assertEqual(len(calls), 1)
        self.assertFalse(self.dialog.yesno_calls)
