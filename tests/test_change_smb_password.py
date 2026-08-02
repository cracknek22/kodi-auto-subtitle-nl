import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "deploy" / "change-smb-password.sh"


class ComposeDetectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.stack = self.base / "smb-stack"
        self.bin = self.base / "bin"
        self.stack.mkdir()
        self.bin.mkdir()
        (self.stack / ".env").write_text(
            "SMB_USER=smbuser\nSMB_PASSWORD=unit-test-secret\n",
            encoding="utf-8",
        )

    def write_command(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def run_check(self) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["PATH"] = f"{self.bin}:/usr/bin:/bin"
        environment["SMB_PASSWORD_STACK_DIR"] = str(self.stack)
        return subprocess.run(
            ["sh", str(HELPER), "--check"],
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )

    def test_uses_docker_compose_plugin_when_available(self):
        self.write_command(
            "docker",
            """
if [ "$1 $2" = "compose version" ]; then exit 0; fi
if [ "$1 $2 $3" = "compose config --quiet" ]; then exit 0; fi
if [ "$1 $2 $3" = "compose config --services" ]; then echo samba; exit 0; fi
exit 91
""".strip(),
        )

        result = self.run_check()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("docker compose", result.stdout)
        self.assertNotIn("unit-test-secret", result.stdout + result.stderr)

    def test_falls_back_to_standalone_docker_compose(self):
        self.write_command("docker", "exit 1")
        self.write_command(
            "docker-compose",
            """
if [ "$1" = "version" ]; then exit 0; fi
if [ "$1 $2" = "config --quiet" ]; then exit 0; fi
if [ "$1 $2" = "config --services" ]; then echo samba; exit 0; fi
exit 92
""".strip(),
        )

        result = self.run_check()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("docker-compose", result.stdout)
        self.assertNotIn("unit-test-secret", result.stdout + result.stderr)

    def test_fails_safely_when_no_compose_variant_exists(self):
        self.write_command("docker", "exit 1")

        result = self.run_check()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Docker Compose", result.stderr)
        self.assertNotIn("unit-test-secret", result.stdout + result.stderr)

    def test_fails_safely_when_samba_service_is_missing(self):
        self.write_command(
            "docker",
            """
if [ "$1 $2" = "compose version" ]; then exit 0; fi
if [ "$1 $2 $3" = "compose config --quiet" ]; then exit 0; fi
if [ "$1 $2 $3" = "compose config --services" ]; then echo filebrowser; exit 0; fi
exit 93
""".strip(),
        )

        result = self.run_check()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("samba", result.stderr)
        self.assertNotIn("unit-test-secret", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
