#!/bin/sh
set -eu

if ! grep -q "^${SMB_USER}:x:${PGID}:" /etc/group; then
    addgroup --gid "${PGID}" "${SMB_USER}"
fi

current_uid="$(id -u "${SMB_USER}")"
current_gid="$(id -g "${SMB_USER}")"
sed -i "s/^${SMB_USER}:x:${current_uid}:${current_gid}:/${SMB_USER}:x:${PUID}:${PGID}:/" /etc/passwd

/usr/local/bin/apply-smb-password-secret.sh
exec /usr/bin/samba.sh "$@"
