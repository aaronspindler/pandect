#!/bin/sh
set -eu

project_root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd -P)
cd "$project_root"
. ./scripts/maintenance-common.sh
pandect_require_docker
pandect_lock

if [ -L .env ] || { [ -e .env ] && [ ! -f .env ]; }; then
    pandect_fail '.env must be a regular file, not a symbolic link.'
fi
if [ -e .env ]; then
    # POSIX numeric ls fields work on both macOS and Linux. Do not append to a
    # foreign-owned, shared, or hard-linked configuration file.
    ls -ldn .env | awk -v owner="$(id -u)" \
        'BEGIN { ok=0 } $2 == 1 && $3 == owner && substr($1, 1, 10) == "-rw-------" { ok=1 } END { exit !ok }' || \
        pandect_fail '.env must be owned by the current operator, mode 0600, and not hard-linked. Correct it explicitly before setup.'
fi
if [ ! -e .env ]; then
    # noclobber avoids replacing configuration created by another setup.
    (set -C; sed -e "s/^PANDECT_UID=.*/PANDECT_UID=$(id -u)/" \
        -e "s/^PANDECT_GID=.*/PANDECT_GID=$(id -g)/" .env.example > .env)
    echo 'Created private .env for the current host user.'
fi
if ! LC_ALL=C grep -Eq '^[[:space:]]*(export[[:space:]]+)?COMPOSE_PROJECT_NAME[[:space:]]*=' .env; then
    pandect_identity=${COMPOSE_PROJECT_NAME:-}
    if [ -z "$pandect_identity" ]; then
        pandect_identity=pandect-$(printf '%s' "$project_root" | cksum | awk '{print $1 "-" $2}')
    fi
    case "$pandect_identity" in ''|*[!a-z0-9_-]*) pandect_fail 'COMPOSE_PROJECT_NAME must use lowercase letters, numbers, underscore, and dash.' ;; esac
    printf '\nCOMPOSE_PROJECT_NAME=%s\n' "$pandect_identity" >> .env
fi

pandect_load_configuration
pandect_validate_data_path
pandect_validate_existing_data
pandect_require_stopped
# No data path is created or chmodded before all checks above succeed.
mkdir -p "$pandect_data_dir"
pandect_validate_private_directory "$pandect_data_dir"
set --
if [ "$pandect_secret_override" = true ]; then set -- --secret-override; fi
docker run --rm --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges:true --user "$pandect_uid:$pandect_gid" \
    --env TMPDIR=/validation \
    --mount "type=bind,src=$pandect_validation_dir,dst=/validation" \
    --mount "type=bind,src=$pandect_data_dir,dst=/data" --entrypoint python \
    "$pandect_maintenance_image" /app/scripts/maintenance.py initialize "$@"

docker compose build web worker media-runner
docker compose run --rm --no-deps web python manage.py migrate --noinput
docker compose --profile models run --rm --no-deps model-provisioner \
    python manage.py prepare_model_permissions
echo 'Setup complete. Existing evidence and credentials were preserved. Start with: make up'
