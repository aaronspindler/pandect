#!/bin/sh
set -eu

project_root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_root"

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
    echo "Docker with the Compose plugin is required for setup." >&2
    exit 1
fi

pandect_host_uid=$(id -u)
pandect_host_gid=$(id -g)
if [ "$pandect_host_uid" -eq 0 ]; then
    echo "Run setup as an unprivileged user so containers do not run as root." >&2
    exit 1
fi

if [ ! -e .env ]; then
    env_tmp=$(mktemp "${TMPDIR:-/tmp}/pandect-env.XXXXXX")
    trap 'rm -f "$env_tmp"' EXIT HUP INT TERM
    sed \
        -e "s/^PANDECT_UID=.*/PANDECT_UID=$pandect_host_uid/" \
        -e "s/^PANDECT_GID=.*/PANDECT_GID=$pandect_host_gid/" \
        .env.example >"$env_tmp"
    chmod 600 "$env_tmp"
    mv "$env_tmp" .env
    trap - EXIT HUP INT TERM
    echo "Created .env for the current host user."
elif [ ! -f .env ]; then
    echo ".env exists but is not a regular file." >&2
    exit 1
fi

pandect_data_dir=${PANDECT_DATA_DIR:-}
if [ -z "$pandect_data_dir" ]; then
    pandect_data_dir=$(sed -n 's/^PANDECT_DATA_DIR=//p' .env | tail -n 1)
fi
pandect_data_dir=${pandect_data_dir:-./data}
case "$pandect_data_dir" in
    \"*\") pandect_data_dir=${pandect_data_dir#\"}; pandect_data_dir=${pandect_data_dir%\"} ;;
    \'*\') pandect_data_dir=${pandect_data_dir#\'}; pandect_data_dir=${pandect_data_dir%\'} ;;
esac

mkdir -p "$pandect_data_dir" "$pandect_data_dir/models" "$pandect_data_dir/work"
chmod 700 "$pandect_data_dir"
chmod 700 "$pandect_data_dir/models" "$pandect_data_dir/work"

docker compose build
docker compose run --rm --no-deps web python manage.py migrate --noinput

secret_file=$pandect_data_dir/secret.key
pandect_secret_override=$(sed -n 's/^PANDECT_SECRET_KEY=//p' .env | tail -n 1)
if [ -z "$pandect_secret_override" ]; then
    if [ ! -s "$secret_file" ]; then
        echo "Django did not create the expected local secret at $secret_file." >&2
        exit 1
    fi
    chmod 600 "$secret_file"
    echo "Generated or retained $secret_file."
else
    echo "Using the explicit PANDECT_SECRET_KEY override from .env."
fi

echo "Setup complete. Start Pandect with: make up"
