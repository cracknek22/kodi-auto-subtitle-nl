"""Optional real FFmpeg/WebRTC test using generated, public-safe speech.

Generate a fixture on macOS without playing audio:
    python -m tests.test_sync_audio_integration --generate /private/tmp/sync-audio-fixture

Run on either macOS or Linux with ffmpeg and ffprobe on PATH and ffsubsync 0.5.1:
    RUN_FFSUBSYNC_AUDIO_INTEGRATION=1 FFSUBSYNC_AUDIO_FIXTURE_DIR=/path/to/fixture \
        python -m unittest tests.test_sync_audio_integration -v

No real films, media-provider traffic, Kodi, or translation API are used. The
HTTP proxy and isolated ffsubsync process are real; only its upstream response
and translation output are supplied by this fixture.
"""
import array
import hashlib
import io
import json
import os
from pathlib import Path
import random
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import wave

import subtitle_translator as app
from media_proxy import MediaProxy
from subtitle_sync import SubtitleSynchronizer
from sync_reference import ReferenceStore


SAMPLE_RATE = 16000
PHRASES = (
    "The little boat was waiting near the shore.",
    "Please close the window before you leave.",
    "We have enough time to finish the story.",
    "A yellow bird landed on the empty table.",
    "Tomorrow we will visit the old stone bridge.",
    "I left a blue notebook beside the lamp.",
    "The garden looks different after the rain.",
    "Someone has moved the chairs into the hall.",
    "Would you like another cup of warm tea?",
    "Listen carefully and tell me what you hear.",
    "The train arrived earlier than we expected.",
    "There is a small shop around the corner.",
    "My friend found the missing silver key.",
    "We should take a short walk before dinner.",
    "The children were counting clouds in the sky.",
    "Nobody remembered the name of that street.",
    "I think this is the beginning of something good.",
    "Put the clean glasses on the kitchen shelf.",
    "The quiet room was full of afternoon sunlight.",
    "She read the letter slowly for a second time.",
    "You can find the answer on the next page.",
    "Our small adventure was nearly at an end.",
    "He smiled when the familiar music started.",
    "We decided to meet again the following week.",
)


def _timestamp(seconds):
    milliseconds = round(seconds * 1000)
    hours, remainder = divmod(milliseconds, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    seconds, fraction = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{fraction:03d}"


def generate_fixture(destination):
    """Synthesize only our own neutral sentences directly into WAV files."""
    if not shutil.which("say"):
        raise RuntimeError("Generate this fixture on macOS, then copy it to the test server.")
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise ValueError("Fixture destination must be empty; existing data is never overwritten.")
    pcm = array.array("h", [0] * (5 * SAMPLE_RATE))
    randomizer = random.Random(726419)
    cues = []
    with tempfile.TemporaryDirectory(prefix="subtitle-voice-fixture-") as temporary:
        for index, phrase in enumerate(PHRASES):
            path = Path(temporary) / f"phrase-{index}.wav"
            subprocess.run(["say", "-v", "Samantha", "-r", "155", "-o", str(path),
                            "--file-format=WAVE", "--data-format=LEI16@16000", phrase],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           timeout=30)
            with wave.open(str(path), "rb") as handle:
                if (handle.getnchannels(), handle.getsampwidth(), handle.getframerate()) != (1, 2, SAMPLE_RATE):
                    raise ValueError("Unexpected synthesized audio format")
                spoken = array.array("h", handle.readframes(handle.getnframes()))
            if sys.byteorder != "little":
                spoken.byteswap()
            active = [position for position, value in enumerate(spoken) if abs(value) > 160]
            if not active:
                raise ValueError("Speech synthesizer produced no audible fixture")
            # Strip synthesizer padding, retaining 30ms around the spoken phrase.
            spoken = spoken[max(0, active[0] - 480):min(len(spoken), active[-1] + 481)]
            start = len(pcm) / SAMPLE_RATE
            pcm.extend(spoken)
            end = len(pcm) / SAMPLE_RATE
            cues.append(f"{101 + index}\r\n{_timestamp(start + 3)} --> "
                        f"{_timestamp(end + 3)}\r\n<i>{phrase}</i>\r\n")
            pcm.extend([0] * round(SAMPLE_RATE * randomizer.uniform(0.9, 4.4)))
    pcm.extend([0] * (6 * SAMPLE_RATE))
    if sys.byteorder != "little":
        pcm.byteswap()
    with wave.open(str(destination / "synthetic-speech.wav"), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(pcm.tobytes())
    (destination / "synthetic.en.srt").write_bytes("\r\n".join(cues).encode("utf-8"))
    return {"cue_count": len(cues), "duration_seconds": len(pcm) / SAMPLE_RATE,
            "introduced_delay_seconds": 3.0}


class _FixtureResponse(io.BytesIO):
    def __init__(self, data, range_header):
        start, end = 0, len(data) - 1
        self.status = 200
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
            if not match or not any(match.groups()):
                raise ValueError("Invalid fixture byte range")
            first, last = match.groups()
            if first:
                start = int(first)
                end = min(end, int(last)) if last else end
            else:
                start = max(0, len(data) - int(last))
            self.status = 206
        if start >= len(data) or end < start:
            self.status = 416
            selected = b""
        else:
            selected = data[start:end + 1]
        self.headers = {"Content-Length": str(len(selected)), "Accept-Ranges": "bytes"}
        if self.status == 206:
            self.headers["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
        elif self.status == 416:
            self.headers["Content-Range"] = f"bytes */{len(data)}"
        super().__init__(selected)

    def getheader(self, name):
        return self.headers.get(name)


@unittest.skipUnless(os.environ.get("RUN_FFSUBSYNC_AUDIO_INTEGRATION") == "1",
                     "Optional actual audio test: set RUN_FFSUBSYNC_AUDIO_INTEGRATION=1")
class ActualAudioPipelineTests(unittest.TestCase):
    def test_real_audio_proxy_sync_and_translation_output(self):
        fixture_dir = Path(os.environ["FFSUBSYNC_AUDIO_FIXTURE_DIR"])
        self.assertIsNotNone(shutil.which("ffmpeg"), "ffmpeg must be on PATH")
        self.assertIsNotNone(shutil.which("ffprobe"), "ffprobe must be on PATH")
        audio = (fixture_dir / "synthetic-speech.wav").read_bytes()
        original = (fixture_dir / "synthetic.en.srt").read_bytes()
        with tempfile.TemporaryDirectory(prefix="subtitle-audio-pipeline-") as temporary:
            root = Path(temporary)
            source = root / "synthetic.en.srt"
            source.write_bytes(original)
            job_id = "job_" + "c" * 32
            digest = hashlib.sha256(original).hexdigest()
            store = ReferenceStore(["fixture.example"])
            store.register({"job_id": job_id, "source": source.name, "source_sha256": digest,
                            "url": "https://fixture.example/synthetic.wav?token=synthetic-only",
                            "headers": {"Authorization": "Bearer synthetic-only"}})
            requests = []
            def opener(reference, allowed_hosts, method, range_header):
                # This injected opener is the only substitute at the media layer;
                # all FFmpeg traffic still crosses the real loopback HTTP proxy.
                self.assertEqual(reference.job_id, job_id)
                self.assertEqual(allowed_hosts, ("fixture.example",))
                self.assertIn(method, ("GET", "HEAD"))
                requests.append((method, range_header))
                return _FixtureResponse(audio, range_header), io.BytesIO()
            translations = []
            def translate(texts):
                translations.extend(texts)
                return [f"Testvertaling {index + 1}." for index, _ in enumerate(texts)]
            scanner = app.ConfirmedJobScanner(
                root, translate,
                synchronizer=SubtitleSynchronizer(timeout=120, python_binary=sys.executable),
                reference_store=store,
                reference_proxy_factory=lambda reference: MediaProxy(reference, ["fixture.example"], opener=opener),
            )
            request = {"version": 2, "job_id": job_id, "source": source.name,
                       "sync": {"required": True, "source_sha256": digest}}
            (root / (source.name + app.REQUEST_SUFFIX)).write_text(json.dumps(request))
            scanner.scan_once()
            scanner.scan_once()
            status = json.loads((root / (source.name + app.STATUS_SUFFIX)).read_text())
            self.assertEqual(status["state"], "complete", status)
            self.assertEqual(status["sync"]["state"], "complete")
            self.assertAlmostEqual(status["sync"]["offset_seconds"], -3.0, delta=0.25)
            self.assertAlmostEqual(status["sync"]["scale_factor"], 1.0, delta=0.002)
            self.assertEqual(source.read_bytes(), original)
            output = (root / "synthetic.nl.srt").read_text()
            output_cues = app.parse_srt(output)
            self.assertEqual(len(output_cues.translatable_texts()), len(PHRASES))
            self.assertEqual(len(translations), len(PHRASES))
            self.assertIn("Testvertaling 1.", output)
            self.assertTrue(requests, "FFmpeg must fetch audio through the actual proxy")
            self.assertNotIn("synthetic-only", json.dumps(status))
            self.assertNotIn("synthetic-only", (root / (source.name + app.REQUEST_SUFFIX)).read_text())
            print(json.dumps({"audio_test_offset_seconds": status["sync"]["offset_seconds"],
                              "scale_factor": status["sync"]["scale_factor"],
                              "proxy_request_count": len(requests), "cue_count": len(translations)}))


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--generate":
        print(json.dumps(generate_fixture(sys.argv[2])))
    else:
        unittest.main()
