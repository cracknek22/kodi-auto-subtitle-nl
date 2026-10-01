import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch


VIEW = Path(__file__).resolve().parents[1] / "kodi-addon/service.autosubtranslate.nl/resources/lib/progress_view.py"


class Control:
    def __init__(self, x, y, width, height, value, **kwargs):
        self.x, self.y, self.width, self.height = x, y, width, height
        self.value, self.options, self.visible = value, kwargs, True

    def setLabel(self, value):
        self.value = value

    def setWidth(self, width):
        self.width = width

    def setVisible(self, visible):
        self.visible = visible


class Window:
    def __init__(self):
        self.controls = []
        self.fail_at = None
        self.fail_remove = False

    def getWidth(self): return 1920
    def getHeight(self): return 1080

    def addControl(self, control):
        if len(self.controls) == self.fail_at:
            raise RuntimeError("add failed")
        self.controls.append(control)

    def removeControl(self, control):
        if self.fail_remove:
            raise RuntimeError("remove failed")
        self.controls.remove(control)


class Background:
    def __init__(self): self.calls = []
    def create(self, heading, message): self.calls.append(("create", heading, message))
    def update(self, percent, heading, message): self.calls.append(("update", percent, heading, message))
    def close(self): self.calls.append(("close",))


class ProgressViewTests(unittest.TestCase):
    def setUp(self):
        self.window, self.background, self.window_ids = Window(), Background(), []
        def window_factory(window_id):
            self.window_ids.append(window_id)
            return self.window
        gui = types.SimpleNamespace(Window=window_factory, ControlImage=Control,
                                    ControlLabel=Control, DialogProgressBG=lambda: self.background)
        spec = importlib.util.spec_from_file_location("subtitle_progress_view_test", VIEW)
        self.module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"xbmcgui": gui}):
            spec.loader.exec_module(self.module)
        self.view = self.module.ProgressOverlay("/addon")

    def test_uses_existing_fullscreen_controls_without_a_focus_window(self):
        self.view.create("Ondertitels", "Synchroniseren…")
        self.assertEqual(self.window_ids, [12005])
        self.assertEqual(len(self.window.controls), 5)
        self.assertEqual(self.background.calls[0], ("create", "Ondertitels", "Synchroniseren…"))
        self.assertFalse(self.window.controls[-1].visible)
        for control in self.window.controls:
            self.assertGreaterEqual(control.x, 0)
            self.assertLessEqual(control.x + control.width, 1920)

    def test_exact_percentage_updates_both_views_and_resets_unknown_fill(self):
        self.view.create("Ondertitels", "Wachten…")
        self.view.update(65, "Ondertitels", "Vertalen: 65%")
        panel, heading, message, track, fill = self.window.controls
        self.assertEqual(fill.width, int(track.width * .65))
        self.assertTrue(fill.visible)
        self.assertEqual(message.value, "Vertalen: 65%")
        self.assertEqual(self.background.calls[-1], ("update", 65, "Ondertitels", "Vertalen: 65%"))
        self.view.update(0, "Ondertitels", "Synchroniseren…")
        self.assertFalse(fill.visible)

    def test_close_removes_only_owned_controls_and_is_idempotent(self):
        unrelated = object()
        self.window.controls.append(unrelated)
        self.view.create("Ondertitels", "Wachten…")
        self.view.close()
        self.view.close()
        self.assertEqual(self.window.controls, [unrelated])
        self.assertEqual(self.background.calls.count(("close",)), 1)

    def test_partial_create_failure_cleans_up_added_controls_and_background(self):
        self.window.fail_at = 2
        with self.assertRaises(RuntimeError):
            self.view.create("Ondertitels", "Wachten…")
        self.assertEqual(self.window.controls, [])
        self.assertEqual(self.background.calls[-1], ("close",))

    def test_cleanup_hides_controls_even_if_kodi_removal_fails(self):
        self.view.create("Ondertitels", "Wachten…")
        self.window.fail_remove = True
        self.view.close()
        self.assertTrue(all(not control.visible for control in self.window.controls))
        self.assertEqual(self.background.calls[-1], ("close",))

    def test_rejects_invalid_percent_before_changing_ui(self):
        self.view.create("Ondertitels", "Wachten…")
        for value in (True, -1, 101, "65", 65.0, float("nan")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.view.update(value, "Ondertitels", "Ongeldig")
        self.assertEqual(len(self.background.calls), 1)


if __name__ == "__main__":
    unittest.main()
