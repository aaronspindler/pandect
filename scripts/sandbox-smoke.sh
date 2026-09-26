#!/bin/sh
# Exercise a test-only hostile dispatcher inside the production confinement.
# Never mount operator data, credentials, the Docker socket, or a host root.
set -eu

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
    echo 'Usage: scripts/sandbox-smoke.sh BUILT_RUNTIME_IMAGE [--current-runner-source]' >&2
    exit 2
fi
image=$1
source_overlay=${2:-}
if [ -n "$source_overlay" ] && [ "$source_overlay" != --current-runner-source ]; then
    echo 'Unknown sandbox smoke option.' >&2
    exit 2
fi
repository=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
fixtures=$repository/tests/sandbox_fixtures
run_id=pandect-sandbox-smoke-$(date +%s)-$$
runner=$run_id-runner
socket_volume=$run_id-socket

cleanup() {
    result=$?
    trap - EXIT HUP INT TERM
    if [ "$result" -ne 0 ]; then
        docker logs "$runner" >&2 || true
    fi
    docker rm --force "$runner" >/dev/null 2>&1 || true
    docker volume rm "$socket_volume" >/dev/null 2>&1 || true
    exit "$result"
}
trap cleanup EXIT HUP INT TERM

run_container() {
    if [ -n "$source_overlay" ]; then
        # Developer-only iteration: mount just code, never the repository root,
        # .env, or data. The release gate must run without this source overlay.
        docker run --mount "type=bind,source=$repository/pandect/media_runner,target=/app/pandect/media_runner,readonly" "$@"
    else
        docker run "$@"
    fi
}
if [ -n "$source_overlay" ]; then
    echo 'DEVELOPMENT ONLY: current runner source overlay; not an image release gate.'
fi

docker volume create --label pandect.synthetic-sandbox-test=true "$socket_volume" >/dev/null
run_container --detach --name "$runner" --label pandect.synthetic-sandbox-test=true \
    --user 0:0 --network none --ipc private --read-only \
    --cap-drop ALL --cap-add CHOWN --cap-add SETUID --cap-add SETGID --cap-add KILL \
    --security-opt no-new-privileges:true \
    --memory 256m --memory-swap 256m --cpus 1 --pids-limit 32 \
    --ulimit core=0 --ulimit nofile=256:256 \
    --tmpfs /scratch:rw,nodev,nosuid,noexec,size=64m,nr_inodes=2048,mode=0700 \
    --tmpfs /tmp:rw,nodev,nosuid,noexec,size=1m,mode=0755 \
    --mount "type=volume,source=$socket_volume,target=/run/pandect-media" \
    --mount "type=bind,source=$fixtures/operations.py,target=/app/pandect/media_runner/operations.py,readonly" \
    --mount "type=bind,source=$fixtures/public-model.txt,target=/models/public-model.txt,readonly" \
    --env PANDECT_RUNNER_APP_UID=10001 --env PANDECT_RUNNER_APP_GID=10001 \
    --env PANDECT_RUNNER_SOCKET=/run/pandect-media/runner.sock \
    --entrypoint python "$image" -m pandect.media_runner >/dev/null

attempt=0
until docker exec "$runner" python -m pandect.media_runner --healthcheck >/dev/null 2>&1; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 30 ] || [ "$(docker inspect -f '{{.State.Running}}' "$runner")" != true ]; then
        echo 'The isolated test runner did not become ready.' >&2
        exit 1
    fi
    sleep 1
done

client() {
    identity=$1
    shift
    run_container --rm --network none --read-only --cap-drop ALL \
        --security-opt no-new-privileges:true --memory 128m --memory-swap 128m \
        --pids-limit 32 --user "$identity:10001" \
        --tmpfs /tmp:rw,nodev,nosuid,noexec,size=16m,mode=1777 \
        --mount "type=volume,source=$socket_volume,target=/run/pandect-media,readonly" \
        --mount "type=bind,source=$repository/tests/sandbox_smoke.py,target=/sandbox_smoke.py,readonly" \
        --env PANDECT_RUNNER_SOCKET=/run/pandect-media/runner.sock \
        --env PANDECT_CLOUDFLARE_API_TOKEN=synthetic-client-only-token \
        --env PYTHONPATH=/app --entrypoint python "$image" /sandbox_smoke.py "$@"
}
client 10002 --unauthorized-peer
client 10001
docker exec "$runner" python -m pandect.media_runner --healthcheck
echo 'Sandbox smoke passed with reduced CI resource limits; no production capacity claim.'
