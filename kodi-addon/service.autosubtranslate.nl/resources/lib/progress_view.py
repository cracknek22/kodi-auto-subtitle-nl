"""Non-interactive progress in menus and over Kodi's existing video window.

Estuary hides DialogProgressBG in fullscreen video. A Python WindowXMLDialog
would steal input even with show(), so use labels/images on window 12005.
Never activate, close, resize, or change the focus of that existing window.
"""

import os

import xbmcgui


class ProgressOverlay:
    def __init__(self, addon_path):
        self._texture = os.path.join(addon_path, "resources", "media", "progress-white.png")
        self._background = None
        self._window = None
        self._controls = []

    def create(self, heading, message):
        self.close()
        try:
            self._background = xbmcgui.DialogProgressBG()
            self._background.create(heading, message)
            self._window = xbmcgui.Window(12005)
            width, height = self._window.getWidth(), self._window.getHeight()
            if width <= 0 or height <= 0:
                raise RuntimeError("Video window dimensions unavailable")
            scale = min(width / 1920.0, height / 1080.0)
            panel_width = max(240, int(550 * scale))
            panel_height = max(76, int(126 * scale))
            margin = max(12, int(36 * scale))
            padding = max(10, int(18 * scale))
            x, y = width - panel_width - margin, margin
            line_height = max(22, int(34 * scale))
            bar_height = max(4, int(8 * scale))
            bar_y = y + panel_height - padding - bar_height
            bar_width = panel_width - 2 * padding
            controls = [
                xbmcgui.ControlImage(x, y, panel_width, panel_height, self._texture,
                                     colorDiffuse="E61C2430"),
                xbmcgui.ControlLabel(x + padding, y + padding, bar_width, line_height,
                                     heading, font="font12", textColor="FFFFFFFF"),
                xbmcgui.ControlLabel(x + padding, y + padding + line_height,
                                     bar_width, line_height, message,
                                     font="font12", textColor="FFCAD5E2"),
                xbmcgui.ControlImage(x + padding, bar_y, bar_width, bar_height,
                                     self._texture, colorDiffuse="FF475569"),
                xbmcgui.ControlImage(x + padding, bar_y, 1, bar_height,
                                     self._texture, colorDiffuse="FF55D6BE"),
            ]
            for control in controls:
                self._window.addControl(control)
                self._controls.append(control)
            self._heading, self._message = controls[1:3]
            self._fill, self._bar_width = controls[4], bar_width
            self._fill.setVisible(False)
        except Exception:
            self.close()
            raise

    def update(self, percent, heading, message):
        if type(percent) is not int or not 0 <= percent <= 100:
            raise ValueError("Invalid progress percentage")
        self._background.update(percent, heading, message)
        self._heading.setLabel(heading)
        self._message.setLabel(message)
        self._fill.setWidth(max(1, int(self._bar_width * percent / 100)))
        self._fill.setVisible(percent > 0)

    def close(self):
        # Python controls attached to an existing Kodi window outlive the script
        # unless removed. Hide first so even a removal error leaves no stale UI.
        for control in reversed(self._controls):
            try:
                control.setVisible(False)
            except Exception:
                pass
            try:
                self._window.removeControl(control)
            except Exception:
                pass
        self._controls = []
        self._window = None
        background, self._background = self._background, None
        if background is not None:
            try:
                background.close()
            except Exception:
                pass
