import os
import pty
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "deploy" / "change-smb-password.sh"
SECRET_APPLIER = ROOT / "deploy" / "apply-smb-password-secret.sh"
SAMBA_ENTRYPOINT = ROOT / "deploy" / "samba-uid-entrypoint.sh"
PUNCTUATED_PASSWORD = "Aa1!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~Z9"


class ComposeDetectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.stack = self.base / "smb-stack"
        self.bin = self.base / "bin"
        self.stack.mkdir()
        self.bin.mkdir()
        self.write_command("sleep", "exit 0")
        (self.stack / ".env").write_text(
            "SMB_USER=smbuser\nPUID=1000\nPGID=1000\n",
            encoding="utf-8",
        )
        self.secret = self.stack / "secrets" / "smb_password"
        self.secret.parent.mkdir(mode=0o700)
        self.secret.write_text("unit-test-secret", encoding="utf-8")
        self.secret.chmod(0o600)

    def write_command(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def run_check(self) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["PATH"] = f"{self.bin}:/usr/bin:/bin"
        environment["SMB_PASSWORD_STACK_DIR"] = str(self.stack)
        environment["SMB_PASSWORD_WAIT_ATTEMPTS"] = "3"
        return subprocess.run(
            ["sh", str(HELPER), "--check"],
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )

    def run_interactive(self, password: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["PATH"] = f"{self.bin}:/usr/bin:/bin"
        environment["SMB_PASSWORD_STACK_DIR"] = str(self.stack)
        master, slave = pty.openpty()
        try:
            process = subprocess.Popen(
                ["sh", str(HELPER)],
                stdin=slave,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
            )
            os.close(slave)
            slave = -1
            os.write(master, f"{password}\n{password}\n".encode())
            try:
                stdout, stderr = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
                raise
            return subprocess.CompletedProcess(
                process.args,
                process.returncode,
                stdout,
                stderr,
            )
        finally:
            os.close(master)
            if slave != -1:
                os.close(slave)

    def write_working_compose(self) -> None:
        self.write_command(
            "docker-compose",
            """
if [ "$1" = "version" ]; then exit 0; fi
if [ "$1 $2" = "config --quiet" ]; then exit 0; fi
if [ "$1 $2" = "config --services" ]; then echo samba; exit 0; fi
if [ "$1" = "up" ]; then exit 0; fi
exit 94
""".strip(),
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

    def test_complete_change_uses_stdin_auth_and_rejects_old_password(self):
        self.write_working_compose()
        self.write_command(
            "docker",
            """
if [ "$1 $2" = "compose version" ]; then exit 1; fi
if [ "$1" = "inspect" ]; then echo starting; exit 0; fi
if [ "$1 $2 $3 $4 $5 $6" = "exec -i smb-server smbclient -A /dev/stdin" ]; then
    input="$(cat)"
    case "$input" in
        *"password = unit-test-secret"*) exit 1 ;;
        *) exit 0 ;;
    esac
fi
exit 95
""".strip(),
        )

        result = self.run_interactive(PUNCTUATED_PASSWORD)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("gewijzigd en getest", result.stdout)
        self.assertEqual(self.secret.read_text(encoding="utf-8"), PUNCTUATED_PASSWORD)
        self.assertNotIn("SMB_PASSWORD", (self.stack / ".env").read_text(encoding="utf-8"))
        self.assertNotIn(PUNCTUATED_PASSWORD, result.stdout + result.stderr)

    def test_rejects_spaces_and_control_characters_before_recreating(self):
        self.write_command(
            "docker",
            """
if [ "$1 $2" = "compose version" ]; then exit 1; fi
if [ "$1" = "inspect" ]; then echo healthy; exit 0; fi
if [ "$1" = "exec" ]; then cat >/dev/null; exit 1; fi
exit 97
""".strip(),
        )
        self.write_working_compose()

        result = self.run_interactive("Bad Password12345")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("spaties", result.stderr)
        self.assertEqual(self.secret.read_text(encoding="utf-8"), "unit-test-secret")
        self.assertNotIn("Bad Password12345", result.stdout + result.stderr)

    def test_reports_when_samba_rejects_the_new_password(self):
        self.write_working_compose()
        self.write_command(
            "docker",
            """
if [ "$1 $2" = "compose version" ]; then exit 1; fi
if [ "$1" = "inspect" ]; then echo healthy; exit 0; fi
if [ "$1 $2 $3 $4 $5 $6" = "exec -i smb-server smbclient -A /dev/stdin" ]; then cat >/dev/null; exit 1; fi
exit 96
""".strip(),
        )

        result = self.run_interactive("AnotherPassword1")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("nieuwe wachtwoord", result.stderr)
        self.assertIn("niet geaccepteerd", result.stderr)
        self.assertEqual(self.secret.read_text(encoding="utf-8"), "unit-test-secret")


class SecretApplierTests(unittest.TestCase):
    def test_passes_punctuation_only_through_stdin(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            secret = base / "smb_password"
            recorded_stdin = base / "stdin"
            recorded_args = base / "args"
            fake_smbpasswd = base / "smbpasswd"
            secret.write_text(PUNCTUATED_PASSWORD, encoding="utf-8")
            secret.chmod(0o600)
            fake_smbpasswd.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$RECORDED_ARGS\"\n"
                "cat > \"$RECORDED_STDIN\"\n",
                encoding="utf-8",
            )
            fake_smbpasswd.chmod(0o700)
            environment = os.environ.copy()
            environment.update(
                {
                    "SMB_PASSWORD_FILE": str(secret),
                    "SMB_PASSWD_BIN": str(fake_smbpasswd),
                    "SMB_USER": "smbuser",
                    "RECORDED_ARGS": str(recorded_args),
                    "RECORDED_STDIN": str(recorded_stdin),
                }
            )

            result = subprocess.run(
                ["sh", str(SECRET_APPLIER)],
                capture_output=True,
                text=True,
                env=environment,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(recorded_args.read_text(encoding="utf-8"), "-s\n-a\nsmbuser\n")
            self.assertEqual(
                recorded_stdin.read_text(encoding="utf-8"),
                f"{PUNCTUATED_PASSWORD}\n{PUNCTUATED_PASSWORD}\n",
            )
            self.assertNotIn(PUNCTUATED_PASSWORD, result.stdout + result.stderr)

    def test_entrypoint_applies_secret_before_starting_samba(self):
        content = SAMBA_ENTRYPOINT.read_text(encoding="utf-8")

        apply_index = content.index(
            "/usr/local/bin/apply-smb-password-secret.sh"
        )
        samba_index = content.index('exec /usr/bin/samba.sh "$@"')
        self.assertLess(apply_index, samba_index)
        self.assertNotIn("SMB_PASSWORD", content)


if __name__ == "__main__":
    unittest.main()
