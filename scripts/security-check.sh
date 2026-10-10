#!/bin/sh
# Explicit network check; ordinary make check remains networkless after builds.
set -eu
umask 077

cd "$(dirname "$0")/.."
repo=$(pwd -P)
audit_dir=$(mktemp -d "${TMPDIR:-/tmp}/pandect-audit.XXXXXXXX")
audit_name=$(basename "$audit_dir" | tr '[:upper:]' '[:lower:]' | tr . -)
tools_image="$audit_name-tools:local"
runtime_image="$audit_name-runtime:local"
maintenance_image="$audit_name-maintenance:local"
secrets_image="$audit_name-secrets:local"
trivy_image="$audit_name-trivy:local"
cleanup() {
    docker image rm "$tools_image" "$runtime_image" "$maintenance_image" "$secrets_image" "$trivy_image" >/dev/null 2>&1 || :
    # Only this invocation's private mktemp directory is ever removed.
    test -n "$audit_dir" && test -d "$audit_dir" && rm -rf -- "$audit_dir"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

docker build --target tools --tag "$tools_image" .
docker build --target runtime --tag "$runtime_image" .
docker build --target maintenance --tag "$maintenance_image" .
docker build --target secret-scanner --tag "$secrets_image" .
docker build --target vulnerability-scanner --tag "$trivy_image" .

# Clone reachable history without reusing object links or copying .git/config.
# Overlay current nonignored source, including not-yet-committed implementation.
# Neither ignored investigation data nor environment files enter these mounts.
git clone --quiet --mirror --no-hardlinks "$repo" "$audit_dir/history"
mkdir "$audit_dir/source"
git ls-files -z --cached --others --exclude-standard | xargs -0 sh -c '
    for path do
        if [ -f "$path" ] || [ -L "$path" ]; then
            printf "%s\0" "$path"
        fi
    done
' sh > "$audit_dir/source-files"
tar --null -T "$audit_dir/source-files" -cf "$audit_dir/source.tar"
tar -xf "$audit_dir/source.tar" -C "$audit_dir/source"
docker run --rm --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges --user "$(id -u):$(id -g)" \
    --mount "type=bind,src=$audit_dir/history,dst=/history,readonly" \
    --entrypoint git "$secrets_image" -c safe.directory=/history \
    -C /history rev-list --all --count > "$audit_dir/commit-count"
IFS= read -r commit_count < "$audit_dir/commit-count"
case "$commit_count" in
    ''|0|*[!0-9]*) echo 'History scan requires readable nonempty Git history.' >&2; exit 1 ;;
esac
# Gitleaks can return zero after Git fails. Treat any error-level log as a
# scanner failure, and scope Git's ownership exception to this private mirror.
if docker run --rm --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges --user "$(id -u):$(id -g)" \
    --env GIT_CONFIG_COUNT=1 --env GIT_CONFIG_KEY_0=safe.directory \
    --env GIT_CONFIG_VALUE_0=/history \
    --mount "type=bind,src=$audit_dir/history,dst=/history,readonly" \
    "$secrets_image" git /history --log-opts=--all --redact --no-banner \
    --no-color --log-level error 2> "$audit_dir/history-errors"; then
    if test -s "$audit_dir/history-errors"; then
        sed -n '1,80p' "$audit_dir/history-errors" >&2
        echo 'History scanner emitted errors; refusing to pass.' >&2
        exit 1
    fi
else
    sed -n '1,80p' "$audit_dir/history-errors" >&2
    exit 1
fi
printf 'Redacted secret scan passed for %s reachable commits.\n' "$commit_count"
docker run --rm --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges --user "$(id -u):$(id -g)" \
    --mount "type=bind,src=$audit_dir/source,dst=/source,readonly" \
    "$secrets_image" dir /source --redact --no-banner --no-color

mkdir "$audit_dir/audit"
cp pyproject.toml uv.lock security-exceptions.json "$audit_dir/audit/"
cp scripts/security_policy.py "$audit_dir/audit/"
docker run --rm --read-only --cap-drop ALL --security-opt no-new-privileges \
    --user "$(id -u):$(id -g)" --tmpfs /tmp:rw,noexec,nosuid,size=128m \
    --env UV_CACHE_DIR=/tmp/uv-cache --workdir /audit \
    --mount "type=bind,src=$audit_dir/audit,dst=/audit,readonly" \
    "$tools_image" python security_policy.py uv --exceptions security-exceptions.json

# Feed image archives, not the Docker socket, to the network-enabled scanner.
mkdir "$audit_dir/report" "$audit_dir/cache" "$audit_dir/scratch"
for image_kind in runtime maintenance; do
    case "$image_kind" in
        runtime) scan_image=$runtime_image ;;
        maintenance) scan_image=$maintenance_image ;;
    esac
    # Docker save replaces files atomically. Reusing a previously mounted path
    # can leave Docker Desktop's file sharing attached to its removed inode.
    image_archive="$audit_dir/audit/$image_kind.tar"
    docker image save --output "$image_archive" "$scan_image"
    docker run --rm --read-only --cap-drop ALL --security-opt no-new-privileges \
        --user "$(id -u):$(id -g)" --tmpfs /tmp:rw,noexec,nosuid,size=64m \
        --env TMPDIR=/scratch \
        --mount "type=bind,src=$image_archive,dst=/image.tar,readonly" \
        --mount "type=bind,src=$audit_dir/report,dst=/report" \
        --mount "type=bind,src=$audit_dir/cache,dst=/cache" \
        --mount "type=bind,src=$audit_dir/scratch,dst=/scratch" \
        "$trivy_image" image --input /image.tar --cache-dir /cache --no-progress \
        --scanners vuln --list-all-pkgs --format json \
        --output "/report/$image_kind.json" --timeout 15m
    docker run --rm --network none --read-only --cap-drop ALL \
        --security-opt no-new-privileges --user "$(id -u):$(id -g)" \
        --mount "type=bind,src=$audit_dir/audit,dst=/audit,readonly" \
        --mount "type=bind,src=$audit_dir/report,dst=/report,readonly" \
        "$tools_image" python /audit/security_policy.py trivy \
        --exceptions /audit/security-exceptions.json --image-kind "$image_kind" \
        --report "/report/$image_kind.json"
    rm -- "$image_archive"
done
