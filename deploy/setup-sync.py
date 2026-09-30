#!/usr/bin/env python3
"""Create private sync credentials and an optional systemd user drop-in.

Never prints the API token. Existing credentials are retained, so Kodi's pin
does not change on an upgrade. Does not install packages or restart services.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import secrets
import shutil
import ssl
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sync_reference import normalize_allowed_hosts


def write_private(path: Path, content: str):
    fd, temporary = tempfile.mkstemp(prefix=".sync-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def environment(name: str, value: str) -> str:
    if any(ord(char) < 32 for char in value):
        raise ValueError("Ongeldig configuratiepad")
    return "Environment=" + json.dumps(name + "=" + value.replace("%", "%%"))


def configure(args):
    address = ipaddress.ip_address(args.bind)
    if address.is_unspecified or address.is_multicast or not (address.is_private or address.is_loopback):
        raise ValueError("Kies een specifiek lokaal/VPN-adres, niet 0.0.0.0 of een publiek adres.")
    allowed_hosts = normalize_allowed_hosts(args.media_host)
    if not 1024 <= args.port <= 65535:
        raise ValueError("Kies een niet-bevoorrechte poort.")
    config = args.config_dir.resolve()
    config.mkdir(parents=True, exist_ok=True, mode=0o700)
    config.chmod(0o700)
    certificate, key, token = (config / name for name in ("server.crt", "server.key", "api-token"))
    for path in (certificate, key, token):
        if path.is_symlink():
            raise ValueError("Configuratie mag geen symbolische links bevatten.")
    if certificate.exists() != key.exists():
        raise ValueError("Onvolledig certificaat; herstel eerst de bestaande configuratie.")
    if not certificate.exists():
        with tempfile.TemporaryDirectory(prefix=".certificate-", dir=config) as temporary:
            temporary = Path(temporary)
            subprocess.run([
                "openssl", "req", "-x509", "-newkey", "rsa:3072", "-sha256",
                "-days", "365", "-nodes", "-subj", "/CN=subtitle-sync",
                "-addext", "subjectAltName=IP:" + str(address),
                "-keyout", str(temporary / "key"), "-out", str(temporary / "cert"),
            ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for generated, destination in (("key", key), ("cert", certificate)):
                (temporary / generated).chmod(0o600)
                os.replace(temporary / generated, destination)
    if not token.exists():
        write_private(token, secrets.token_urlsafe(32) + "\n")
    key.chmod(0o600)
    token.chmod(0o600)
    fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(certificate.read_text())).hexdigest()
    host = "[" + str(address) + "]" if address.version == 6 else str(address)
    public = {"server": f"https://{host}:{args.port}", "certificate_sha256": fingerprint,
              "token_file": str(token)}
    write_private(config / "connection.json", json.dumps(public, indent=2) + "\n")
    if args.unit_dir:
        # A venv executable is normally a symlink: retain that path so Python
        # finds its pyvenv.cfg instead of silently using the system environment.
        python = args.python.absolute()
        ffmpeg_dir = args.ffmpeg_dir.resolve()
        if not python.is_file() or not all((ffmpeg_dir / binary).is_file() for binary in ("ffmpeg", "ffprobe")):
            raise ValueError("Installeer eerst de aparte Python-omgeving, ffmpeg en ffprobe.")
        args.unit_dir.mkdir(parents=True, exist_ok=True)
        target = args.unit_dir / "sync.conf"
        if target.exists():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            shutil.copy2(target, config / ("sync.conf.before-" + stamp))
        values = {
            "SYNC_ENABLED": "true", "SYNC_BIND": str(address), "SYNC_PORT": str(args.port),
            "SYNC_ALLOWED_HOSTS": ",".join(allowed_hosts), "SYNC_CERT_FILE": str(certificate),
            "SYNC_KEY_FILE": str(key), "SYNC_TOKEN_FILE": str(token), "SYNC_PYTHON": str(python),
            "SYNC_TIMEOUT": "300", "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1",
            "PATH": str(ffmpeg_dir) + ":/usr/local/bin:/usr/bin:/bin",
        }
        write_private(target, "[Service]\nMemoryMax=1G\n" + "\n".join(environment(k, v) for k, v in values.items()) + "\n")
    return public


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--media-host", action="append", required=True)
    parser.add_argument("--config-dir", type=Path, default=Path.home() / ".config/subtitle-translator/sync")
    parser.add_argument("--unit-dir", type=Path)
    parser.add_argument("--python", type=Path, default=Path.home() / ".local/share/subtitle-translator/sync-venv/bin/python")
    parser.add_argument("--ffmpeg-dir", type=Path, default=Path("/usr/bin"))
    args = parser.parse_args()
    try:
        print(json.dumps(configure(args), indent=2))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print("Veilige koppeling niet ingesteld: " + type(exc).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
