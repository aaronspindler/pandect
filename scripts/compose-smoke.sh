#!/bin/sh
# No user's data/config is used: everything lives in this invocation's tmpdir.
set -eu
umask 077

cd "$(dirname "$0")/.."
repo=$(pwd -P)
smoke_dir=$(mktemp -d "${TMPDIR:-/tmp}/pandect-smoke.XXXXXXXX")
smoke_dir=$(CDPATH='' cd "$smoke_dir" && pwd -P)
# An operator's custom Compose files/env-file list must never enter this stack.
unset COMPOSE_FILE COMPOSE_PROFILES COMPOSE_ENV_FILES COMPOSE_PATH_SEPARATOR
unset COMPOSE_IGNORE_ORPHANS COMPOSE_REMOVE_ORPHANS
COMPOSE_PROJECT_NAME=$(basename "$smoke_dir" | tr '[:upper:]' '[:lower:]' | tr . -)
PANDECT_DATA_DIR="$smoke_dir/data"
PANDECT_PORT=0
PANDECT_DEBUG=false
PANDECT_ALLOWED_HOSTS=localhost,127.0.0.1,[::1]
PANDECT_TIME_ZONE=UTC
PANDECT_SECRET_KEY=
PANDECT_CLOUDFLARE_AI_ENABLED=false
PANDECT_CLOUDFLARE_API_TOKEN=
export COMPOSE_PROJECT_NAME PANDECT_DATA_DIR PANDECT_PORT PANDECT_DEBUG
export PANDECT_ALLOWED_HOSTS PANDECT_TIME_ZONE
export PANDECT_SECRET_KEY PANDECT_CLOUDFLARE_AI_ENABLED PANDECT_CLOUDFLARE_API_TOKEN

cleanup() {
    if test -f "$smoke_dir/source/compose.yaml"; then
        (cd "$smoke_dir/source" && docker compose down --volumes --remove-orphans) || :
    fi
    # Untag only this invocation's images; never prune shared images/build cache.
    docker image rm "${COMPOSE_PROJECT_NAME}-runtime:local" \
        "${COMPOSE_PROJECT_NAME}-maintenance:local" \
        "${COMPOSE_PROJECT_NAME}-tools:local" >/dev/null 2>&1 || :
    # This directory contains only the smoke test's synthetic state/source copy.
    test -n "$smoke_dir" && test -d "$smoke_dir" && rm -rf -- "$smoke_dir"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

git clone --quiet --no-hardlinks --no-checkout "$repo" "$smoke_dir/source"
git ls-files -z --cached --others --exclude-standard | xargs -0 sh -c '
    for path do
        if [ -f "$path" ] || [ -L "$path" ]; then
            printf "%s\0" "$path"
        fi
    done
' sh > "$smoke_dir/source-files"
tar --null -T "$smoke_dir/source-files" -cf "$smoke_dir/source.tar"
tar -xf "$smoke_dir/source.tar" -C "$smoke_dir/source"
cd "$smoke_dir/source"

make setup
./scripts/sandbox-smoke.sh "${COMPOSE_PROJECT_NAME}-runtime:local"
# Bounded wait covers all health checks, including the actual runner supervisor.
docker compose up --detach --wait --wait-timeout 120
docker compose exec -T web python scripts/compose_smoke.py health
make task-smoke
docker compose exec -T web python -m pandect.media_runner.smoke
docker compose exec -T web python scripts/compose_smoke.py optional-models
docker compose --profile models run --rm --no-deps model-provisioner \
    python scripts/compose_smoke.py seed
docker compose down --remove-orphans
docker compose run --rm --no-deps web python scripts/compose_smoke.py snapshot
make backup BACKUP_DIR="$smoke_dir/backups"
set -- "$smoke_dir"/backups/pandect-backup-*
test "$#" -eq 1 && test -d "$1" || {
    echo 'Smoke backup did not create exactly one backup bundle.' >&2
    exit 1
}
backup=$1
PANDECT_DATA_DIR="$smoke_dir/restored"
export PANDECT_DATA_DIR
make restore BACKUP="$backup"
docker compose run --rm --no-deps web python scripts/compose_smoke.py verify
docker compose up --detach --wait --wait-timeout 120
docker compose exec -T web python scripts/compose_smoke.py health
make task-smoke
echo 'Clean setup, runner, health, task, and backup/restore smoke checks passed.'
