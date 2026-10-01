import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest

from subtitle_translator import CodexTranslator, process_file
from codex_runtime import CODEX_ISOLATION_ARGS, run_codex


class TranslationResilienceTests(unittest.TestCase):
    def test_primary_timeout_uses_existing_fallback_without_changing_primary(self):
        attempts = []

        def runner(command, **kwargs):
            self.assertEqual(tuple(command[2:6]), CODEX_ISOLATION_ARGS)
            attempts.append((command[command.index("--model") + 1], kwargs))
            if len(attempts) == 1:
                raise subprocess.TimeoutExpired(command, kwargs["timeout"],
                                                output="PRIVATE", stderr="PRIVATE")
            return SimpleNamespace(returncode=0, stdout=json.dumps(
                {"translations": ["Kom maar mee."]}), stderr="")

        with tempfile.TemporaryDirectory() as temporary:
            translator = CodexTranslator("codex", "gpt-5.6-luna", runner=runner,
                                         work_dir=Path(temporary), timeout=180)
            self.assertEqual(translator(["Come with me."]), ["Kom maar mee."])
            self.assertEqual(translator.model, "gpt-5.6-luna")
            self.assertEqual(translator.last_model, "gpt-5.6-terra")
        self.assertEqual([model for model, _ in attempts],
                         ["gpt-5.6-luna", "gpt-5.6-terra"])
        self.assertTrue(all(kwargs["timeout"] == 180 for _, kwargs in attempts))
        self.assertEqual(attempts[0][1]["input"], attempts[1][1]["input"])

    def test_both_timeouts_fail_after_two_attempts_without_partial_translation(self):
        attempts = []

        def runner(command, **kwargs):
            attempts.append(command)
            raise subprocess.TimeoutExpired(command, kwargs["timeout"],
                                            output="PRIVATE", stderr="PRIVATE")

        with tempfile.TemporaryDirectory() as temporary:
            translator = CodexTranslator("codex", "gpt-5.6-luna", runner=runner,
                                         work_dir=Path(temporary))
            with self.assertRaisesRegex(RuntimeError, "duurde te lang") as caught:
                translator(["Come with me."])
            self.assertNotIn("PRIVATE", str(caught.exception))
            self.assertIsNone(translator.last_model)
        self.assertEqual(len(attempts), 2)

    def test_timeout_without_fallback_stops_after_one_attempt(self):
        attempts = []

        def runner(command, **kwargs):
            attempts.append(command)
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])

        with tempfile.TemporaryDirectory() as temporary:
            translator = CodexTranslator("codex", "gpt-5.6-luna", runner=runner,
                                         work_dir=Path(temporary), fallback_model=None)
            with self.assertRaisesRegex(RuntimeError, "duurde te lang"):
                translator(["Come with me."])
        self.assertEqual(len(attempts), 1)

    def test_defaults_bound_request_size_and_wait_without_changing_models(self):
        translator = CodexTranslator("codex", "gpt-5.6-luna")
        self.assertEqual(translator.batch_size, 80)
        self.assertEqual(translator.timeout, 180)
        self.assertEqual(translator.fallback_model, "gpt-5.6-terra")
        self.assertIs(translator.runner, run_codex)

    def test_later_batch_failure_never_publishes_partial_subtitles(self):
        original = ("1\n00:00:01,000 --> 00:00:02,000\nHello.\n\n"
                    "2\n00:00:03,000 --> 00:00:04,000\nGoodbye.\n")
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "test.en.srt"
            output = Path(temporary) / "test.nl.srt"
            source.write_text(original)
            output.write_text("existing complete output")
            calls = []

            def translate(texts):
                calls.append(texts)
                if len(calls) == 2:
                    raise RuntimeError("Codex-vertaling duurde te lang.")
                return ["Hallo."]

            with self.assertRaisesRegex(RuntimeError, "duurde te lang"):
                process_file(source, translate, batch_size=1)
            self.assertEqual(len(calls), 2)
            self.assertEqual(source.read_text(), original)
            self.assertEqual(output.read_text(), "existing complete output")


if __name__ == "__main__":
    unittest.main()
