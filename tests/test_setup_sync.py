import importlib.util
import json
import os
import sys
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("setup_sync", ROOT / "deploy/setup-sync.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class SetupSyncTests(unittest.TestCase):
    def test_private_credentials_are_reused_and_token_never_returned(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "sync"
            args = SimpleNamespace(bind="127.0.0.1", port=8766, media_host=["*.real-debrid.com"],
                                   config_dir=config, unit_dir=None)
            public = setup.configure(args)
            token = (config / "api-token").read_text().strip()
            self.assertNotIn(token, json.dumps(public))
            self.assertEqual(os.stat(config / "api-token").st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(config / "server.key").st_mode & 0o777, 0o600)
            again = setup.configure(args)
            self.assertEqual(public, again)
            self.assertEqual((config / "api-token").read_text().strip(), token)

    def test_wildcard_listen_address_and_environment_injection_rejected(self):
        with self.assertRaises(ValueError):
            setup.configure(SimpleNamespace(bind="0.0.0.0"))
        with self.assertRaises(ValueError):
            setup.environment("PATH", "good\nExecStart=bad")

    def test_dropin_preserves_models_and_existing_service(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binaries = root / "bin"
            binaries.mkdir()
            for name in ("ffmpeg", "ffprobe"):
                (binaries / name).touch()
            (binaries / "python").symlink_to(sys.executable)
            unit_dir = root / "service.d"
            args = SimpleNamespace(bind="192.168.2.60", port=8766, media_host=["*.real-debrid.com"],
                                   config_dir=root / "config", unit_dir=unit_dir,
                                   python=binaries / "python", ffmpeg_dir=binaries)
            setup.configure(args)
            dropin = (unit_dir / "sync.conf").read_text()
            self.assertIn("MemoryMax=1G", dropin)
            self.assertNotIn("CODEX_MODEL", dropin)
            self.assertNotIn("ExecStart", dropin)
            self.assertIn("SYNC_TOKEN_FILE", dropin)
            self.assertIn(str(binaries / "python"), dropin)
            self.assertNotIn((args.config_dir / "api-token").read_text().strip(), dropin)
