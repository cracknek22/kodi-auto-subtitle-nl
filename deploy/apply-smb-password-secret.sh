#!/bin/sh
set -eu

PASSWORD_FILE="${SMB_PASSWORD_FILE:-/run/secrets/smb_password}"
SMB_PASSWD_BIN="${SMB_PASSWD_BIN:-smbpasswd}"
SMB_USER="${SMB_USER:-smbuser}"

if [ ! -f "$PASSWORD_FILE" ] || [ ! -r "$PASSWORD_FILE" ]; then
    echo "Beveiligd SMB-wachtwoordbestand ontbreekt." >&2
    exit 1
fi
if ! printf '%s\n' "$SMB_USER" | LC_ALL=C grep -Eq '^[A-Za-z0-9._-]{1,32}$'; then
    echo "Ongeldige SMB-gebruikersnaam." >&2
    exit 1
fi

byte_count="$(wc -c < "$PASSWORD_FILE" | tr -d ' ')"
newline_count="$(LC_ALL=C tr -cd '\n' < "$PASSWORD_FILE" | wc -c | tr -d ' ')"
if [ "$byte_count" -lt 1 ] || [ "$byte_count" -gt 127 ] \
    || [ "$newline_count" -ne 0 ] \
    || ! LC_ALL=C grep -Eq '^[!-~]+$' "$PASSWORD_FILE"; then
    echo "Ongeldig SMB-wachtwoordbestand." >&2
    exit 1
fi

password="$(cat "$PASSWORD_FILE")"
printf '%s\n%s\n' "$password" "$password" \
    | "$SMB_PASSWD_BIN" -s -a "$SMB_USER" >/dev/null
password=""
