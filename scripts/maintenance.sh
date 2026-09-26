#!/bin/sh
set -eu

project_root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd -P)
cd "$project_root"
. ./scripts/maintenance-common.sh
pandect_require_docker
pandect_lock
case "${1:-}" in backup|restore) pandect_action=$1 ;; *) pandect_fail 'Usage: maintenance.sh backup BACKUP_DIR | restore BACKUP_DIRECTORY' ;; esac
[ -n "${2:-}" ] || pandect_fail 'Supply a dedicated backup directory.'
pandect_backup=$(pandect_canonical_path "$2")
pandect_load_configuration
pandect_validate_data_path
case "$pandect_backup/" in "$project_root/"*|"$pandect_data_dir/"*) pandect_fail 'Backups must be outside the checkout and source data.' ;; esac
case "$pandect_data_dir/" in "$pandect_backup/"*) pandect_fail 'Backup location must not contain the data directory.' ;; esac
[ "$pandect_backup" != / ] && [ "$pandect_backup" != "$pandect_host_home" ] || pandect_fail 'Choose a dedicated backup directory.'
pandect_require_stopped
if [ "$pandect_action" = backup ]; then
    pandect_validate_existing_data
    [ -d "$pandect_data_dir" ] || pandect_fail 'No data directory exists to back up.'
    mkdir -p "$pandect_backup"
    pandect_validate_private_directory "$pandect_backup"
    pandect_tooling_revision=$(git rev-parse --verify HEAD 2>/dev/null || printf unknown)
    pandect_revision=$pandect_tooling_revision
    if [ -n "${BACKUP_REVISION:-}" ]; then
        pandect_revision=$(git rev-parse --verify --end-of-options "${BACKUP_REVISION}^{commit}" 2>/dev/null) || \
            pandect_fail 'BACKUP_REVISION must resolve to an available local Git commit.'
    fi
    # Recovery must not require rebuilding or starting the application. This
    # identifies the configured image only when it is already available.
    pandect_image_id=$(pandect_runtime_image_identity)
    set --
    if [ -n "$(git status --porcelain --untracked-files=normal 2>/dev/null || printf unknown)" ]; then set -- --dirty; fi
    pandect_backup_name=$(docker run --rm -i --network none --read-only --cap-drop ALL \
        --security-opt no-new-privileges:true --user "$pandect_uid:$pandect_gid" \
        --env TMPDIR=/validation \
        --mount "type=bind,src=$pandect_validation_dir,dst=/validation" \
        --mount "type=bind,src=$pandect_data_dir,dst=/data,readonly" \
        --mount "type=bind,src=$pandect_backup,dst=/backup" --entrypoint python \
        "$pandect_maintenance_image" /app/scripts/maintenance.py backup \
        --revision "$pandect_revision" --tooling-revision "$pandect_tooling_revision" \
        --image-id "$pandect_image_id" "$@" <"$pandect_config")
    printf 'Verified private backup: %s/%s\n' "$pandect_backup" "$pandect_backup_name"
    echo 'The archive is not encrypted. Store it on encrypted storage.'
else
    [ -d "$pandect_backup" ] || pandect_fail 'Backup directory does not exist.'
    if [ -d "$pandect_data_dir" ] && [ -n "$(find "$pandect_data_dir" -mindepth 1 -maxdepth 1 -print -quit)" ]; then
        pandect_fail 'Restore requires an empty data directory; existing data will never be overwritten.'
    fi
    pandect_parent=$(dirname "$pandect_data_dir")
    [ -d "$pandect_parent" ] || pandect_fail 'Create the dedicated restore parent directory first.'
    pandect_staging=$(mktemp -d "$pandect_parent/.pandect-restore.XXXXXX")
    pandect_validate_private_directory "$pandect_staging"
    if [ -d "$pandect_data_dir" ]; then pandect_validate_private_directory "$pandect_data_dir"; fi
    # Retain staging after failure for operator inspection; never guess a
    # recursive cleanup target. It is private and outside the destination.
    if ! docker run --rm --network none --read-only --cap-drop ALL \
        --security-opt no-new-privileges:true --user "$pandect_uid:$pandect_gid" \
        --env TMPDIR=/validation \
        --mount "type=bind,src=$pandect_validation_dir,dst=/validation" \
        --mount "type=bind,src=$pandect_staging,dst=/data" \
        --mount "type=bind,src=$pandect_backup,dst=/backup,readonly" --entrypoint python \
        "$pandect_maintenance_image" /app/scripts/maintenance.py restore; then
        rmdir "$pandect_staging" 2>/dev/null || true
        pandect_fail 'Restore verification failed. Existing data was not changed.'
    fi
    pandect_require_stopped
    pandect_validate_data_path
    pandect_validate_private_directory "$pandect_staging"
    if [ -d "$pandect_data_dir" ]; then pandect_validate_private_directory "$pandect_data_dir"; fi
    [ "$pandect_parent" != / ] || pandect_fail "Restore parent cannot be filesystem root; verified staging retained at $pandect_staging"
    # This separate invocation only performs a fixed directory rename. It has
    # no archive mount, configuration input, credentials, or decoding code.
    # POSIX rename atomically refuses a nonempty destination directory.
    docker run --rm --network none --read-only --cap-drop ALL \
        --security-opt no-new-privileges:true --user "$pandect_uid:$pandect_gid" \
        --mount "type=bind,src=$pandect_parent,dst=/parent" --entrypoint python \
        "$pandect_maintenance_image" /app/scripts/maintenance.py publish \
        --source-name "$(basename "$pandect_staging")" \
        --target-name "$(basename "$pandect_data_dir")"
    echo 'Restore published atomically without applying migrations.'
fi
