#!/bin/sh
# Sourced by operator entrypoints; never evaluate .env as shell code.

pandect_fail() { echo "$*" >&2; exit 1; }

pandect_cleanup() {
    if [ -n "${pandect_config:-}" ]; then rm -f "$pandect_config"; fi
    if [ -n "${pandect_validation_dir:-}" ]; then
        rmdir "$pandect_validation_dir" || echo "Private validation data remains at $pandect_validation_dir; inspect it before removing it." >&2
    fi
    if [ "${pandect_lock_owned:-false}" = true ]; then
        rm -f "$project_root/.pandect-maintenance.lock/pid"
        rmdir "$project_root/.pandect-maintenance.lock"
    fi
}

pandect_lock() {
    if ! mkdir "$project_root/.pandect-maintenance.lock" 2>/dev/null; then
        pandect_fail 'Another maintenance operation holds .pandect-maintenance.lock. Check its pid file before recovering an abandoned lock.'
    fi
    pandect_lock_owned=true
    printf '%s\n' "$$" > "$project_root/.pandect-maintenance.lock/pid"
    trap pandect_cleanup EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
}

pandect_require_docker() {
    command -v docker >/dev/null 2>&1 || pandect_fail 'Docker with Compose >= 2.24 is required.'
    pandect_version=$(docker compose version --short 2>/dev/null) || pandect_fail 'Docker Compose >= 2.24 is required.'
    printf '%s\n' "$pandect_version" | awk -F. '{gsub(/^v/, "", $1); exit !(($1 > 2) || ($1 == 2 && $2 >= 24))}' || pandect_fail 'Upgrade Docker Compose to version 2.24 or later.'
    docker info >/dev/null 2>&1 || pandect_fail 'Docker is not available. Start Docker and retry.'
    [ "$(id -u)" -ne 0 ] || pandect_fail 'Run maintenance as an unprivileged host user.'
    umask 077
}

pandect_load_configuration() {
    docker compose --profile tools build maintenance >&2
    pandect_maintenance_image=$(docker compose --profile tools config --images | sed -n '/-maintenance:local$/p')
    case "$pandect_maintenance_image" in
        ''|*'
'*) pandect_fail 'Unable to resolve the single maintenance image.' ;;
    esac
    pandect_config=$(mktemp "${TMPDIR:-/tmp}/pandect-config.XXXXXX")
    pandect_validation_dir=$(mktemp -d "${TMPDIR:-/tmp}/pandect-validation.XXXXXX")
    # Raw configuration can contain credentials; it only travels through stdin.
    # The tempfile contains the helper's explicit safe allowlist.
    docker compose --profile tools config --format json | docker run --rm -i \
        --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true \
        --entrypoint python "$pandect_maintenance_image" /app/scripts/maintenance.py config >"$pandect_config"
    pandect_data_dir=$(pandect_config_get data_path)
    pandect_project=$(pandect_config_get project)
    pandect_runtime_image=$(pandect_config_get image)
    pandect_uid=$(pandect_config_get uid)
    pandect_gid=$(pandect_config_get gid)
    pandect_secret_override=$(pandect_config_get secret_override)
    case "$pandect_uid:$pandect_gid" in *[!0-9:]*|0:*|*:0) pandect_fail 'Configure non-root numeric PANDECT_UID and PANDECT_GID.' ;; esac
}

pandect_config_get() {
    docker run --rm -i --network none --read-only --cap-drop ALL \
        --security-opt no-new-privileges:true --entrypoint python \
        "$pandect_maintenance_image" /app/scripts/maintenance.py get --key "$1" <"$pandect_config"
}

pandect_runtime_image_identity() {
    # This is only the image reference in the tooling checkout's configuration;
    # it does not identify the historical process that wrote any evidence.
    # A first legacy backup must work before that image has ever been built.
    docker image inspect --format '{{.Id}}' "$pandect_runtime_image" 2>/dev/null || printf unknown
}

# Resolve lexically while checking every component on the host, before Docker
# receives a writable mount. No mount of / or the operator's home is needed.
pandect_canonical_path() {
    case "$1" in *','*|*':'*|*'
'*) pandect_fail 'Paths must not contain commas, colons, or newlines.' ;; esac
    case "$1" in /*) pandect_candidate=$1 ;; *) pandect_candidate=$project_root/$1 ;; esac
    pandect_result=/
    pandect_old_ifs=$IFS
    IFS=/
    set -f
    set -- $pandect_candidate
    IFS=$pandect_old_ifs
    set +f
    for pandect_component do
        case "$pandect_component" in
            ''|.) continue ;;
            ..) pandect_result=$(dirname "$pandect_result") ;;
            *)
                pandect_result=${pandect_result%/}/$pandect_component
                [ ! -L "$pandect_result" ] || pandect_fail 'Paths must not contain symbolic links; use the physical path.'
                ;;
        esac
    done
    printf '%s\n' "$pandect_result"
}

pandect_validate_data_path() {
    pandect_data_dir=$(pandect_canonical_path "$pandect_data_dir")
    pandect_host_home=$(CDPATH='' cd -- "$HOME" && pwd -P)
    case "$pandect_data_dir" in /|"$pandect_host_home"|"$project_root") pandect_fail 'Choose a dedicated Pandect data directory.' ;; esac
    case "$project_root/" in "$pandect_data_dir/"*) pandect_fail 'Data must not contain the checkout.' ;; esac
    case "$pandect_host_home/" in "$pandect_data_dir/"*) pandect_fail 'Data must not contain the home directory.' ;; esac
    case "$pandect_data_dir" in "$project_root/data") ;; "$project_root/"*) pandect_fail 'Only ./data is supported inside the checkout.' ;; esac
    [ ! -e "$pandect_data_dir" ] || [ -d "$pandect_data_dir" ] || pandect_fail 'The data path is not a directory.'
}

pandect_validate_existing_data() {
    if [ -d "$pandect_data_dir" ]; then
        pandect_validate_private_directory "$pandect_data_dir"
        set --
        if [ "$pandect_secret_override" = true ]; then set -- --secret-override; fi
        docker run --rm --network none --read-only --cap-drop ALL \
            --security-opt no-new-privileges:true --user "$pandect_uid:$pandect_gid" \
            --env TMPDIR=/validation \
            --mount "type=bind,src=$pandect_validation_dir,dst=/validation" \
            --mount "type=bind,src=$pandect_data_dir,dst=/data,readonly" \
            --entrypoint python "$pandect_maintenance_image" /app/scripts/maintenance.py validate "$@"
    fi
}

pandect_validate_private_directory() {
    [ -d "$1" ] && [ ! -L "$1" ] || pandect_fail 'Expected a real private directory.'
    ls -ldn "$1" | awk -v owner="$(id -u)" \
        'BEGIN { ok=0 } $3 == owner && substr($1, 1, 10) == "drwx------" { ok=1 } END { exit !ok }' || \
        pandect_fail 'The dedicated directory must be owned by this host operator and mode 0700; correct it explicitly and retry.'
}

pandect_require_stopped() {
    [ -z "$(docker ps --quiet --filter "label=com.docker.compose.project=$pandect_project")" ] || pandect_fail 'Stop all project services and one-off containers before maintenance: make down'
    # Catch a legacy project name or another checkout sharing the same data.
    for pandect_container in $(docker ps --quiet); do
        pandect_mounts=$(docker inspect --format '{{range .Mounts}}{{if .RW}}{{println .Source}}{{end}}{{end}}' "$pandect_container") || \
            pandect_fail 'Unable to verify running-container mounts; retry maintenance after stopping them.'
        if printf '%s\n' "$pandect_mounts" | pandect_conflicting_mount; then
            pandect_fail 'A running container still mounts this data. Stop it before maintenance.'
        fi
    done
}

pandect_conflicting_mount() {
    awk -v source="$pandect_data_dir" '$0 == "/" || $0 == source || index($0, source "/") == 1 || (length($0) && index(source, $0 "/") == 1) { found=1 } END { exit !found }'
}
