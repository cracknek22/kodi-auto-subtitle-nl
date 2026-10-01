"""Regression tests for ffsubsync 0.5.1's unknown-versus-silent sample gap bug.

The installed optional synchronization dependency is needed for these tests. No
network, real media, subtitle dialogue or translation API is used here.
"""
import importlib.util
import logging
import random
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import ffsubsync_worker as worker


@unittest.skipUnless(importlib.util.find_spec("ffsubsync") is not None,
                     "Optional ffsubsync 0.5.1 dependency is required")
class SparseReferenceTests(unittest.TestCase):
    def setUp(self):
        import numpy as np
        from ffsubsync import speech_transformers
        self.np = np
        self.module = speech_transformers
        self.base = speech_transformers.MultiSegmentVideoSpeechTransformer
        self.api = SimpleNamespace(MultiSegmentVideoSpeechTransformer=self.base)
        self.addCleanup(logging.disable, logging.root.manager.disable)
        logging.disable(logging.CRITICAL)

    def corrected_class(self):
        worker.install_sparse_reference_fix(self.api)
        return self.api.MultiSegmentVideoSpeechTransformer

    def sampler(self, klass=None, *, count=8, skip=True):
        return (klass or self.corrected_class())(
            vad="webrtc", sample_rate=100, frame_rate=48000,
            non_speech_label=0, segment_count=count, skip_intro_outro=skip,
            parallel_workers=1,
        )

    def probe(self, duration):
        return patch.object(self.module.ffmpeg, "probe", return_value={"format": {"duration": str(duration)}})

    def test_dense_perfect_match_is_not_rejected_as_anticorrelated(self):
        from ffsubsync.aligners import FFTAligner
        from ffsubsync.ffsubsync import assess_alignment_quality
        np = self.np
        randomizer = random.Random(77281)
        full = np.zeros(140515)
        position = 0
        while position < len(full):
            speech, silence = randomizer.randint(400, 900), randomizer.randint(25, 90)
            full[position:min(len(full), position + speech)] = 1
            position += speech + silence
        def extract(instance, _reference, start):
            return start, full[start * 100:min(len(full), (start + 60) * 100)]
        with self.probe(len(full) / 100), patch.object(self.base, "_extract_segment_speech", extract):
            broken = self.sampler(self.base).fit_transform("synthetic-not-opened")
            corrected = self.sampler().fit_transform("synthetic-not-opened")
        old_score, old_offset = FFTAligner(max_offset_samples=3000).fit_transform(broken, full, get_score=True)
        score, offset = FFTAligner(max_offset_samples=3000).fit_transform(corrected, full, get_score=True)
        self.assertLess(old_score, 0, "Reproduction must show upstream rejecting a perfect match")
        self.assertEqual(old_offset, 0)
        self.assertEqual(offset, 0)
        self.assertAlmostEqual(score, 48000, places=5)
        self.assertEqual(assess_alignment_quality(score, offset / 100, 1., min_score=1,
            max_offset_seconds=30, max_framerate_deviation=.1), [])
        self.assertAlmostEqual(old_score, -28981, places=5)

    def test_actual_sampled_silence_stays_zero_but_unknown_gaps_are_neutral(self):
        np = self.np
        def extract(_instance, _reference, start):
            return start, np.array([1., 0., 1.])
        with self.probe(200), patch.object(self.base, "_extract_segment_speech", extract):
            signal = self.sampler(count=2, skip=False).fit_transform("synthetic-not-opened")
        self.assertEqual(signal[:4].tolist(), [1., 0., 1., .5])
        self.assertEqual(signal[14000:14004].tolist(), [1., 0., 1., .5])
        self.assertTrue(np.all(signal[3:14000] == .5))
        self.assertTrue(np.all(signal[-2:] == .5), "Library array padding is not sampled audio")

    def test_failed_windows_and_short_read_tails_are_not_claimed_as_silence(self):
        np = self.np
        def extract(_instance, _reference, start):
            if start == 140:
                raise OSError("synthetic extraction failure")
            return start, np.array([1., 0., 0., 1., 0., 0., 1.])
        with self.probe(200), patch.object(self.base, "_extract_segment_speech", extract):
            signal = self.sampler(count=2, skip=False).fit_transform("synthetic-not-opened")
        self.assertEqual(signal[:7].tolist(), [1., 0., 0., 1., 0., 0., 1.])
        self.assertTrue(np.all(signal[7:] == .5))

    def test_all_failed_empty_and_silent_samples_still_fail_closed(self):
        np = self.np
        def failure(_instance, _reference, _start):
            raise OSError("synthetic extraction failure")
        for extract in (failure, lambda _i, _r, s: (s, np.zeros(6000)),
                        lambda _i, _r, s: (s, np.array([]))):
            with self.subTest(extract=extract):
                sampler = self.sampler(count=2, skip=False)
                with self.probe(200), patch.object(self.base, "_extract_segment_speech", extract):
                    with self.assertRaisesRegex(ValueError, "Unable to detect speech"):
                        sampler.fit("synthetic-not-opened")
                self.assertIsNone(sampler.video_speech_results_)

    def test_each_fit_forgets_previous_successful_ranges(self):
        np = self.np
        sampler = self.sampler(count=2, skip=False)
        selected_start = [0]
        def extract(_instance, _reference, start):
            if start != selected_start[0]:
                raise OSError("synthetic extraction failure")
            return start, np.array([1., 0.])
        with self.probe(200), patch.object(self.base, "_extract_segment_speech", extract):
            first = sampler.fit_transform("synthetic-not-opened")
            selected_start[0] = 140
            second = sampler.fit_transform("synthetic-not-opened")
        self.assertEqual(first[:2].tolist(), [1., 0.])
        self.assertEqual(second[:2].tolist(), [.5, .5])
        self.assertEqual(second[14000:14002].tolist(), [1., 0.])

    def test_overlapping_samples_clipping_and_skipped_intro_outro_keep_timeline(self):
        np = self.np
        def extract(_instance, _reference, start):
            result = np.zeros(6000)
            result[0] = 1
            return start, result
        with self.probe(100), patch.object(self.base, "_extract_segment_speech", extract):
            signal = self.sampler(count=3, skip=False).fit_transform("synthetic-not-opened")
        self.assertTrue(np.all(signal[:10000] != .5))
        self.assertEqual(signal[-2:].tolist(), [.5, .5])
        with self.probe(400), patch.object(self.base, "_extract_segment_speech", extract):
            signal = self.sampler(count=2, skip=True).fit_transform("synthetic-not-opened")
        self.assertTrue(np.all(signal[:3000] == .5))
        self.assertEqual(signal[3000], 1)
        self.assertEqual(signal[3001], 0)
        self.assertTrue(np.all(signal[34000:] == .5))

    def test_installation_is_idempotent_and_changes_only_reference_transformer(self):
        first = self.corrected_class()
        worker.install_sparse_reference_fix(self.api)
        self.assertIs(self.api.MultiSegmentVideoSpeechTransformer, first)
        self.assertTrue(issubclass(first, self.base))
        self.assertEqual(set(vars(self.api)), {"MultiSegmentVideoSpeechTransformer"})

    def test_parallel_out_of_order_completion_uses_union_of_actual_spans(self):
        np = self.np
        last_ready = threading.Event()
        completed = []
        def extract(_instance, _reference, start):
            if start == 0 and not last_ready.wait(timeout=2):
                raise RuntimeError("test synchronization timed out")
            completed.append(start)
            if start == 40:
                last_ready.set()
            signal = np.zeros(3000)
            signal[0] = 1
            return start, signal
        sampler = self.sampler(count=3, skip=False)
        sampler.parallel_workers = 3
        with self.probe(100), patch.object(self.base, "_extract_segment_speech", extract):
            signal = sampler.fit_transform("synthetic-not-opened")
        self.assertLess(completed.index(40), completed.index(0))
        self.assertTrue(np.all(signal[:7000] != .5))
        self.assertTrue(np.all(signal[7000:] == .5))


if __name__ == "__main__":
    unittest.main()
