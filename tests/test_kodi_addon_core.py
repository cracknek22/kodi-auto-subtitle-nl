import sys
import unittest
from pathlib import Path


ADDON_LIB = (
    Path(__file__).resolve().parents[1]
    / "kodi-addon"
    / "service.autosubtranslate.nl"
    / "resources"
    / "lib"
)
sys.path.insert(0, str(ADDON_LIB))

from core import (  # noqa: E402
    build_request,
    first_dialogue,
    is_dutch_subtitle,
    request_path,
    select_stable_candidate,
    staged_subtitle_name,
    stable_candidates,
    status_path,
    translated_name,
    validate_completed_status,
    video_fingerprint,
)


class CandidateSelectionTests(unittest.TestCase):
    def test_selects_one_new_stable_english_srt(self):
        baseline = {
            "smb://server/subs/old.en.srt": (100, 10),
        }
        previous = {
            **baseline,
            "smb://server/subs/Movie.en.srt": (500, 20),
        }
        current = dict(previous)

        self.assertEqual(
            select_stable_candidate(baseline, previous, current),
            "smb://server/subs/Movie.en.srt",
        )

    def test_rejects_unstable_multiple_or_dutch_candidates(self):
        baseline = {}
        previous = {
            "smb://server/subs/Movie.en.srt": (500, 20),
            "smb://server/subs/Other.en.srt": (200, 21),
        }
        self.assertIsNone(
            select_stable_candidate(baseline, previous, dict(previous))
        )
        self.assertIsNone(
            select_stable_candidate(
                baseline,
                {"smb://server/subs/Movie.en.srt": (400, 20)},
                {"smb://server/subs/Movie.en.srt": (500, 21)},
            )
        )
        self.assertIsNone(
            select_stable_candidate(
                baseline,
                {"smb://server/subs/Movie.nl.srt": (500, 20)},
                {"smb://server/subs/Movie.nl.srt": (500, 20)},
            )
        )

    def test_lists_multiple_stable_candidates_for_explicit_user_choice(self):
        previous = {
            "smb://server/subs/A.en.srt": (100, 10),
            "smb://server/subs/B.en.srt": (200, 11),
        }
        self.assertEqual(
            stable_candidates({}, previous, dict(previous)),
            [
                "smb://server/subs/A.en.srt",
                "smb://server/subs/B.en.srt",
            ],
        )


class JobProtocolTests(unittest.TestCase):
    def test_sync_request_contains_hash_only_and_requires_completed_sync(self):
        source = "smb://server/subtitles/Movie.en.srt"
        payload = build_request(source, "job_12345678", source_sha256="a" * 64)
        self.assertEqual(payload["version"], 2)
        self.assertEqual(payload["sync"], {"required": True, "source_sha256": "a" * 64})
        self.assertEqual(set(payload), {"version", "job_id", "source", "requested_at", "sync"})
        with self.assertRaises(ValueError):
            build_request(source, "job_12345678", source_sha256="not-a-hash")
        status = {"version": 2, "state": "complete", "source": "Movie.en.srt", "output": "Movie.nl.srt"}
        for sync in (None, {}, {"state": "failed"}, {"state": "processing"}):
            with self.subTest(sync=sync), self.assertRaisesRegex(ValueError, "synchronisatie"):
                validate_completed_status(source, {**status, "sync": sync})
        self.assertEqual(validate_completed_status(source, {**status, "sync": {"state": "complete", "offset_seconds": 1.25, "scale_factor": 1.0}}), "Movie.nl.srt")

    def test_builds_a_safe_unique_name_for_a_staged_kodi_subtitle(self):
        self.assertEqual(
            staged_subtitle_name(
                "special://temp/[B]Movie: Final?.en.srt",
                "abcdef1234567890",
            ),
            "Kodi-abcdef123456-B_Movie_ Final_.en.srt",
        )
        self.assertEqual(
            staged_subtitle_name("special://temp/.....srt", "a" * 32),
            "Kodi-aaaaaaaaaaaa-subtitle.srt",
        )

        with self.assertRaisesRegex(ValueError, "SRT"):
            staged_subtitle_name("special://temp/Movie.ass", "a" * 32)
        with self.assertRaisesRegex(ValueError, "tijdelijk bestand-ID"):
            staged_subtitle_name("special://temp/Movie.srt", "../bad")

    def test_builds_safe_request_and_matching_paths(self):
        source = "smb://192.168.2.60/share/subtitles/Movie.en.srt"
        payload = build_request(
            source,
            "job_12345678",
        )

        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["source"], "Movie.en.srt")
        self.assertEqual(payload["job_id"], "job_12345678")
        self.assertNotIn("video", payload)
        self.assertEqual(
            request_path(source),
            f"{source}.translate.request.json",
        )
        self.assertEqual(
            status_path(source),
            f"{source}.translate.status.json",
        )

    def test_recognizes_dutch_names_and_streams(self):
        self.assertTrue(is_dutch_subtitle("Movie.nl.srt"))
        self.assertTrue(is_dutch_subtitle("Nederlands (External)"))
        self.assertTrue(is_dutch_subtitle("Dutch"))
        self.assertTrue(is_dutch_subtitle("Movie.nl-NL.srt"))
        self.assertTrue(is_dutch_subtitle("nl-BE (External)"))
        self.assertTrue(is_dutch_subtitle("Dutch SDH"))
        self.assertFalse(is_dutch_subtitle("English (External)"))

    def test_hashes_video_identity_without_storing_the_stream_url(self):
        stream_url = "https://example.invalid/video?token=secret"

        fingerprint = video_fingerprint(stream_url)

        self.assertEqual(len(fingerprint), 64)
        self.assertNotIn("secret", fingerprint)
        self.assertEqual(fingerprint, video_fingerprint(stream_url))

    def test_validates_deterministic_completed_status(self):
        source = "smb://server/subtitles/Movie.en.srt"
        status = {
            "version": 1,
            "job_id": "job_12345678",
            "state": "complete",
            "source": "Movie.en.srt",
            "output": "Movie.nl.srt",
        }

        self.assertEqual(translated_name(source), "Movie.nl.srt")
        self.assertEqual(
            validate_completed_status(source, status),
            "Movie.nl.srt",
        )

        forged = dict(status, output="Other.nl.srt")
        with self.assertRaisesRegex(ValueError, "uitvoernaam"):
            validate_completed_status(source, forged)

    def test_extracts_first_dialogue_for_confirmation(self):
        content = """1
00:00:01,000 --> 00:00:03,000
<i>Come on, give me a break.</i>

2
00:00:04,000 --> 00:00:06,000
Second line.
"""
        self.assertEqual(first_dialogue(content), "Come on, give me a break.")


if __name__ == "__main__":
    unittest.main()
