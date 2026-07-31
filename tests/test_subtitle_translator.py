import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from subtitle_translator import (
    CodexTranslator,
    ConfirmedJobScanner,
    MAX_SUBTITLE_BYTES,
    make_formatting_plan,
    output_path_for,
    parse_srt,
    process_file,
    render_srt,
)


SAMPLE_SRT = """1
00:00:01,000 --> 00:00:03,000
Hello there.

2
00:00:04,000 --> 00:00:06,000
How are you?
I missed you.
"""


class SrtParsingTests(unittest.TestCase):
    def test_preserves_indices_timestamps_and_translates_only_dialogue(self):
        document = parse_srt(SAMPLE_SRT)

        self.assertEqual(
            document.translatable_texts(),
            ["Hello there.", "How are you?\nI missed you."],
        )

        rendered = render_srt(
            document,
            ["Hallo daar.", "Hoe gaat het?\nIk heb je gemist."],
        )
        self.assertEqual(
            rendered,
            """1
00:00:01,000 --> 00:00:03,000
Hallo daar.

2
00:00:04,000 --> 00:00:06,000
Hoe gaat het?
Ik heb je gemist.
""",
        )

    def test_accepts_bom_crlf_and_cues_without_sequence_number(self):
        content = (
            "\ufeff00:00:00,000 --> 00:00:01,500\r\n"
            "<i>Wait!</i>\r\n\r\n"
            "2\r\n00:00:02,000 --> 00:00:03,000\r\nRun.\r\n"
        )

        document = parse_srt(content)

        self.assertEqual(document.translatable_texts(), ["<i>Wait!</i>", "Run."])
        self.assertEqual(
            render_srt(document, ["<i>Wacht!</i>", "Rennen."]),
            """00:00:00,000 --> 00:00:01,500
<i>Wacht!</i>

2
00:00:02,000 --> 00:00:03,000
Rennen.
""",
        )

    def test_rejects_a_file_without_valid_srt_cues(self):
        with self.assertRaisesRegex(ValueError, "geen geldige SRT"):
            parse_srt("This is plain text, not a subtitle.")

    def test_render_rejects_translation_count_mismatch(self):
        document = parse_srt(SAMPLE_SRT)

        with self.assertRaisesRegex(ValueError, "aantal vertalingen"):
            render_srt(document, ["Alleen de eerste."])

    def test_formatting_plan_preserves_exact_tags_whitespace_and_line_breaks(self):
        plan = make_formatting_plan("<i> Wait! </i>\n{\\an8}Don't move.")

        self.assertEqual(plan.fragments, ("Wait!", "Don't move."))
        self.assertEqual(
            plan.restore(["Wacht!", "Niet bewegen."]),
            "<i> Wacht! </i>\n{\\an8}Niet bewegen.",
        )

    def test_formatting_plan_discards_model_invented_tags_and_newlines(self):
        plan = make_formatting_plan("<i>Wait!</i>")

        restored = plan.restore(['<i class="bad">Wacht!\nNu</i>'])

        self.assertEqual(restored, "<i>Wacht! Nu</i>")


class FileWorkflowTests(unittest.TestCase):
    def test_output_name_replaces_english_language_suffix(self):
        self.assertEqual(
            output_path_for(Path("/subs/Movie.en.srt")),
            Path("/subs/Movie.nl.srt"),
        )
        self.assertEqual(
            output_path_for(Path("/subs/Movie.srt")),
            Path("/subs/Movie.nl.srt"),
        )
        self.assertIsNone(output_path_for(Path("/subs/Movie.nl.srt")))
        self.assertIsNone(output_path_for(Path("/subs/Movie.nl-NL.srt")))

    def test_process_file_writes_utf8_output_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "Movie.en.srt"
            source.write_text(SAMPLE_SRT, encoding="utf-8")

            output = process_file(
                source,
                lambda texts: ["Hallo daar.", "Hoe gaat het?\nIk miste je."],
            )

            self.assertEqual(output, Path(tmp) / "Movie.nl.srt")
            self.assertIn("Hallo daar.", output.read_text(encoding="utf-8"))
            self.assertEqual(output.stat().st_mode & 0o777, 0o644)
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])

    def test_failed_translation_never_leaves_partial_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "Movie.en.srt"
            source.write_text(SAMPLE_SRT, encoding="utf-8")

            def fail(_texts):
                raise RuntimeError("backend offline")

            with self.assertRaisesRegex(RuntimeError, "backend offline"):
                process_file(source, fail)

            self.assertFalse((Path(tmp) / "Movie.nl.srt").exists())
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])

    def test_preplaced_predictable_temp_symlink_is_never_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "Movie.en.srt"
            source.write_text(SAMPLE_SRT, encoding="utf-8")
            victim = root / "unrelated.txt"
            victim.write_text("leave me alone", encoding="utf-8")
            predictable_temp = root / "Movie.nl.srt.tmp"
            predictable_temp.symlink_to(victim)

            process_file(
                source,
                lambda _texts: ["Hallo daar.", "Hoe gaat het?\nIk miste je."],
            )

            self.assertEqual(victim.read_text(encoding="utf-8"), "leave me alone")
            self.assertTrue(predictable_temp.is_symlink())

    def test_oversized_translation_is_rejected_before_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "Movie.en.srt"
            source.write_text(SAMPLE_SRT, encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "groter dan 2 MB"):
                process_file(
                    source,
                    lambda _texts: [
                        "x" * (MAX_SUBTITLE_BYTES + 1),
                        "Tweede regel.",
                    ],
                )

            self.assertFalse((Path(tmp) / "Movie.nl.srt").exists())

class ConfirmedJobTests(unittest.TestCase):
    def _write_request(
        self,
        root,
        *,
        source="Movie.en.srt",
        job_id="job-12345678",
        extra=None,
    ):
        request = Path(root) / "Movie.en.srt.translate.request.json"
        payload = {
            "version": 1,
            "job_id": job_id,
            "source": source,
        }
        payload.update(extra or {})
        request.write_text(
            json.dumps(payload),
            encoding="utf-8",
        )
        return request

    def test_srt_without_confirmed_request_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "Movie.en.srt"
            source.write_text(SAMPLE_SRT, encoding="utf-8")
            calls = []
            scanner = ConfirmedJobScanner(Path(tmp), lambda texts: calls.append(texts))

            scanner.scan_once()
            scanner.scan_once()

            self.assertEqual(calls, [])
            self.assertFalse((Path(tmp) / "Movie.nl.srt").exists())

    def test_valid_request_is_processed_once_and_writes_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "Movie.en.srt"
            source.write_text(SAMPLE_SRT, encoding="utf-8")
            request = self._write_request(tmp)
            calls = []

            def translate(texts):
                calls.append(list(texts))
                return ["Hallo daar.", "Hoe gaat het?\nIk heb je gemist."]

            scanner = ConfirmedJobScanner(Path(tmp), translate)

            self.assertEqual(scanner.scan_once(), [])
            self.assertEqual(scanner.scan_once(), [Path(tmp) / "Movie.nl.srt"])
            self.assertEqual(scanner.scan_once(), [])
            self.assertEqual(len(calls), 1)

            status = json.loads(
                request.with_name("Movie.en.srt.translate.status.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(status["state"], "complete")
            self.assertEqual(status["job_id"], "job-12345678")
            self.assertEqual(status["output"], "Movie.nl.srt")

    def test_completed_status_is_not_overwritten_when_source_disappears(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "Movie.en.srt"
            source.write_text(SAMPLE_SRT, encoding="utf-8")
            request = self._write_request(root)
            scanner = ConfirmedJobScanner(
                root,
                lambda _texts: ["Hallo daar.", "Hoe gaat het?\nIk miste je."],
            )

            scanner.scan_once()
            scanner.scan_once()
            source.unlink()
            scanner.scan_once()

            status = json.loads(
                request.with_name("Movie.en.srt.translate.status.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(status["state"], "complete")
            self.assertEqual(status["job_id"], "job-12345678")
            self.assertEqual(status["output"], "Movie.nl.srt")

    def test_preplaced_status_temp_symlink_is_never_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "Movie.en.srt"
            source.write_text(SAMPLE_SRT, encoding="utf-8")
            self._write_request(root)
            victim = root / "unrelated.txt"
            victim.write_text("leave me alone", encoding="utf-8")
            predictable_temp = (
                root / "Movie.en.srt.translate.status.json.tmp"
            )
            predictable_temp.symlink_to(victim)
            scanner = ConfirmedJobScanner(
                root,
                lambda _texts: ["Hallo daar.", "Hoe gaat het?\nIk miste je."],
            )

            scanner.scan_once()
            scanner.scan_once()

            self.assertEqual(victim.read_text(encoding="utf-8"), "leave me alone")
            self.assertTrue(predictable_temp.is_symlink())

    def test_missing_source_failure_keeps_the_confirmed_job_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "Movie.en.srt"
            source.write_text(SAMPLE_SRT, encoding="utf-8")
            request = self._write_request(tmp)
            scanner = ConfirmedJobScanner(Path(tmp), lambda _texts: [])

            scanner.scan_once()
            source.unlink()
            scanner.scan_once()

            status = json.loads(
                request.with_name("Movie.en.srt.translate.status.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(status["state"], "failed")
            self.assertEqual(status["job_id"], "job-12345678")
            self.assertIn("ongeldige bron", status["message"])

    def test_path_traversal_request_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_request(tmp, source="../secret.srt")
            calls = []
            scanner = ConfirmedJobScanner(Path(tmp), lambda texts: calls.append(texts))

            scanner.scan_once()
            scanner.scan_once()

            status = json.loads(
                (Path(tmp) / "Movie.en.srt.translate.status.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(status["state"], "failed")
            self.assertIn("ongeldige bron", status["message"])
            self.assertEqual(calls, [])

    def test_request_cannot_store_a_video_url_or_unknown_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "Movie.en.srt"
            source.write_text(SAMPLE_SRT, encoding="utf-8")
            self._write_request(
                tmp,
                extra={"video": "https://stream.invalid/?token=secret"},
            )
            scanner = ConfirmedJobScanner(Path(tmp), lambda _texts: [])

            scanner.scan_once()
            scanner.scan_once()

            status = json.loads(
                (Path(tmp) / "Movie.en.srt.translate.status.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(status["state"], "failed")
            self.assertNotIn("secret", json.dumps(status))


class CodexTranslatorTests(unittest.TestCase):
    def test_uses_one_read_only_structured_codex_task_for_plain_fragments(self):
        captured = {}

        def runner(command, **kwargs):
            captured.update({"command": command, **kwargs})
            schema_path = Path(
                command[command.index("--output-schema") + 1]
            )
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            self.assertEqual(
                schema["properties"]["translations"]["minItems"],
                2,
            )
            self.assertEqual(
                schema["properties"]["translations"]["maxItems"],
                2,
            )
            plain_items = json.loads(
                kwargs["input"].split("INPUT_JSON:\n", 1)[1]
            )
            self.assertEqual(plain_items, ["Wait!", "Stay here."])
            self.assertIn("silent final fluency pass", kwargs["input"])
            self.assertIn("machine-translated", kwargs["input"])
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "translations": [
                            '<i class="bad">Wacht!</i>',
                            "Blijf hier.",
                        ]
                    },
                ),
                stderr="",
            )

        with tempfile.TemporaryDirectory() as tmp:
            translator = CodexTranslator(
                "/usr/local/bin/codex",
                "gpt-5.6-terra",
                reasoning_effort="low",
                runner=runner,
                work_dir=Path(tmp),
            )

            self.assertEqual(
                translator(["<i> Wait! </i>\n{\\an8}Stay here."]),
                ["<i> Wacht! </i>\n{\\an8}Blijf hier."],
            )

        command = captured["command"]
        self.assertIn("--strict-config", command)
        self.assertIn("--ephemeral", command)
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--ignore-rules", command)
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        self.assertEqual(command[command.index("--model") + 1], "gpt-5.6-terra")
        self.assertIn('model_reasoning_effort="low"', command)
        self.assertIn('approval_policy="never"', command)
        self.assertIn("features.shell_tool=false", command)
        self.assertIn('web_search="disabled"', command)
        self.assertNotIn("--output-last-message", command)
        self.assertEqual(command[-1], "-")
        self.assertNotIn("Wait!", " ".join(command))
        self.assertNotIn("<i>", captured["input"])
        self.assertNotIn("{\\an8}", captured["input"])

    def test_splits_a_batch_when_backend_returns_wrong_count(self):
        calls = []

        def runner(command, **kwargs):
            items = json.loads(
                kwargs["input"].split("INPUT_JSON:\n", 1)[1]
            )
            calls.append(items)
            if len(items) > 1:
                result = {"translations": ["te weinig"]}
            else:
                result = {"translations": [f"nl:{items[0]}"]}
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(result),
                stderr="",
            )

        with tempfile.TemporaryDirectory() as tmp:
            translator = CodexTranslator(
                "codex",
                "gpt-5.6-terra",
                batch_size=5000,
                runner=runner,
                work_dir=Path(tmp),
            )

            self.assertEqual(
                translator(["one", "two", "three"]),
                ["nl:one", "nl:two", "nl:three"],
            )
        self.assertGreater(len(calls), 1)

    def test_reports_auth_and_usage_errors_without_exposing_cli_output(self):
        def runner(_command, **_kwargs):
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="Usage limit reached. sensitive subtitle text",
            )

        with tempfile.TemporaryDirectory() as tmp:
            translator = CodexTranslator(
                "codex",
                "gpt-5.6-terra",
                runner=runner,
                work_dir=Path(tmp),
            )

            with self.assertRaisesRegex(RuntimeError, "gebruikslimiet"):
                translator(["private dialogue"])

    def test_uses_configured_fallback_when_primary_model_is_at_capacity(self):
        attempted_models = []

        def runner(command, **_kwargs):
            model = command[command.index("--model") + 1]
            attempted_models.append(model)
            if model == "gpt-5.6-luna":
                return SimpleNamespace(
                    returncode=1,
                    stdout="",
                    stderr="Selected model is at capacity.",
                )
            return SimpleNamespace(
                returncode=0,
                stdout='{"translations":["Hallo."]}',
                stderr="",
            )

        with tempfile.TemporaryDirectory() as tmp:
            translator = CodexTranslator(
                "codex",
                "gpt-5.6-luna",
                fallback_model="gpt-5.6-terra",
                runner=runner,
                work_dir=Path(tmp),
            )

            self.assertEqual(translator(["Hello."]), ["Hallo."])
            self.assertEqual(translator.last_model, "gpt-5.6-terra")

        self.assertEqual(
            attempted_models,
            ["gpt-5.6-luna", "gpt-5.6-terra"],
        )

    def test_rejects_an_empty_translation(self):
        def runner(_command, **_kwargs):
            return SimpleNamespace(
                returncode=0,
                stdout='{"translations":[""]}',
                stderr="",
            )

        with tempfile.TemporaryDirectory() as tmp:
            translator = CodexTranslator(
                "codex",
                "gpt-5.6-luna",
                fallback_model=None,
                runner=runner,
                work_dir=Path(tmp),
            )

            with self.assertRaisesRegex(RuntimeError, "lege vertaling"):
                translator(["Hello."])


if __name__ == "__main__":
    unittest.main()
