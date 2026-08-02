#!/bin/sh
set -eu

STACK_DIR="${SMB_PASSWORD_STACK_DIR:-/home/radxa/smb-stack}"
ENV_FILE="$STACK_DIR/.env"
COMPOSE_SERVICE="${SMB_PASSWORD_COMPOSE_SERVICE:-samba}"

detect_compose() {
    if docker compose version >/dev/null 2>&1; then
        COMPOSE_KIND="plugin"
        return 0
    fi
    if command -v docker-compose >/dev/null 2>&1 \
        && docker-compose version >/dev/null 2>&1; then
        COMPOSE_KIND="standalone"
        return 0
    fi
    echo "Docker Compose is niet beschikbaar." >&2
    return 1
}

run_compose() {
    if [ "$COMPOSE_KIND" = "plugin" ]; then
        docker compose "$@"
    else
        docker-compose "$@"
    fi
}

detect_compose

validate_compose() {
    if [ ! -d "$STACK_DIR" ] || [ ! -f "$ENV_FILE" ]; then
        echo "SMB-configuratie niet gevonden." >&2
        return 1
    fi
    if ! (cd "$STACK_DIR" && run_compose config --quiet); then
        echo "De SMB Compose-configuratie is ongeldig." >&2
        return 1
    fi

    service_found=0
    services="$(cd "$STACK_DIR" && run_compose config --services)"
    for service in $services; do
        if [ "$service" = "$COMPOSE_SERVICE" ]; then
            service_found=1
            break
        fi
    done
    if [ "$service_found" -ne 1 ]; then
        echo "Compose-service $COMPOSE_SERVICE is niet gevonden." >&2
        return 1
    fi
}

validate_compose

if [ "${1:-}" = "--check" ]; then
    if [ "$COMPOSE_KIND" = "plugin" ]; then
        echo "Controle geslaagd met docker compose."
    else
        echo "Controle geslaagd met docker-compose."
    fi
    exit 0
fi
if [ "$#" -ne 0 ]; then
    echo "Onbekende optie. Gebruik zonder opties om het wachtwoord te wijzigen." >&2
    exit 1
fi

if [ ! -t 0 ]; then
    echo "Open eerst een interactieve SSH-sessie en start dit script daar." >&2
    exit 1
fi
if [ ! -f "$ENV_FILE" ]; then
    echo "SMB-configuratie niet gevonden." >&2
    exit 1
fi

old_password="$(sed -n 's/^SMB_PASSWORD=//p' "$ENV_FILE" | head -n 1)"
if [ -z "$old_password" ]; then
    echo "Huidig SMB-wachtwoord ontbreekt in de configuratie." >&2
    exit 1
fi

tty_state="$(stty -g)"
password=""
confirmation=""
restore_tty() {
    stty "$tty_state" 2>/dev/null || true
}
trap restore_tty EXIT HUP INT TERM

printf "Nieuw wachtwoord voor smbuser: "
stty -echo
IFS= read -r password
stty "$tty_state"
printf "\nHerhaal het nieuwe wachtwoord: "
stty -echo
IFS= read -r confirmation
stty "$tty_state"
printf "\n"

if [ "$password" != "$confirmation" ]; then
    echo "De twee wachtwoorden zijn niet gelijk." >&2
    exit 1
fi
if [ "$password" = "$old_password" ]; then
    echo "Kies een ander wachtwoord dan het huidige." >&2
    exit 1
fi
if [ "${#password}" -lt 16 ] || [ "${#password}" -gt 64 ]; then
    echo "Gebruik 16 tot en met 64 tekens." >&2
    exit 1
fi
if ! printf '%s' "$password" | grep -Eq '^[A-Za-z0-9_@.-]+$'; then
    echo "Gebruik alleen letters, cijfers en _ @ . -" >&2
    exit 1
fi

umask 077
timestamp="$(date +%Y%m%d-%H%M%S)"
backup="$ENV_FILE.backup-$timestamp"
temporary="$(mktemp "$ENV_FILE.new.XXXXXX")"
auth_file="$(mktemp /tmp/smb-auth.XXXXXX)"
old_auth_file="$(mktemp /tmp/smb-old-auth.XXXXXX)"

cleanup() {
    rm -f "$temporary" "$auth_file" "$old_auth_file"
    restore_tty
}
trap cleanup EXIT HUP INT TERM

cp -p "$ENV_FILE" "$backup"
chmod 600 "$backup"

found_password=0
while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
        SMB_PASSWORD=*)
            printf 'SMB_PASSWORD=%s\n' "$password" >> "$temporary"
            found_password=1
            ;;
        *)
            printf '%s\n' "$line" >> "$temporary"
            ;;
    esac
done < "$ENV_FILE"
if [ "$found_password" -ne 1 ]; then
    printf 'SMB_PASSWORD=%s\n' "$password" >> "$temporary"
fi
chmod 600 "$temporary"
mv "$temporary" "$ENV_FILE"

rollback() {
    reason="$1"
    cp -p "$backup" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    if (cd "$STACK_DIR" && run_compose up -d --force-recreate "$COMPOSE_SERVICE" >/dev/null); then
        echo "$reason Het oude wachtwoord is hersteld." >&2
    else
        echo "$reason De oude configuratie is hersteld, maar Samba kon niet automatisch worden herstart." >&2
    fi
    exit 1
}

if ! (cd "$STACK_DIR" && run_compose up -d --force-recreate "$COMPOSE_SERVICE" >/dev/null); then
    rollback "Samba kon niet met de nieuwe configuratie worden gestart."
fi

healthy=0
attempt=0
while [ "$attempt" -lt 30 ]; do
    health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' smb-server 2>/dev/null || true)"
    if [ "$health" = "healthy" ] || [ "$health" = "running" ]; then
        healthy=1
        break
    fi
    attempt=$((attempt + 1))
    sleep 1
done
if [ "$healthy" -ne 1 ]; then
    rollback "Samba werd niet op tijd gezond met de nieuwe configuratie."
fi

printf 'username = smbuser\npassword = %s\n' "$password" > "$auth_file"
printf 'username = smbuser\npassword = %s\n' "$old_password" > "$old_auth_file"
chmod 600 "$auth_file" "$old_auth_file"

test_smb_auth() {
    docker exec -i smb-server \
        smbclient -A /dev/stdin //127.0.0.1/share -m SMB3 -c ls \
        < "$1" >/dev/null 2>&1
}

if ! test_smb_auth "$auth_file"; then
    rollback "Het nieuwe wachtwoord werd door Samba niet geaccepteerd."
fi
if test_smb_auth "$old_auth_file"; then
    rollback "Het oude wachtwoord werd na de wijziging nog geaccepteerd."
fi

password=""
confirmation=""
old_password=""
echo "SMB-wachtwoord gewijzigd en getest. Werk nu Kodi en andere SMB-apparaten bij."
