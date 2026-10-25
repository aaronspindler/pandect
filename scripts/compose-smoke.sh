#!/bin/sh
# Use only a fresh project and temporary configuration; never the developer's data.
set -eu
project="pandect-smoke-$(date +%s)-$$"
smoke_env=$(mktemp)
compose() {
    docker compose --project-name "$project" --env-file "$smoke_env" "$@"
}
cleanup() {
    result=$?
    if [ "$result" -ne 0 ]; then
        compose logs --no-color --tail 100 || true
    fi
    compose down --volumes --remove-orphans || true
    rm -f "$smoke_env"
}
trap cleanup EXIT
trap 'exit 1' INT TERM
cat > "$smoke_env" <<'ENV'
DJANGO_DEBUG=true
DJANGO_SECRET_KEY=synthetic-compose-smoke-secret-not-for-deployment
POSTGRES_DB=pandect_smoke
POSTGRES_USER=pandect_smoke
POSTGRES_PASSWORD=synthetic-smoke-password
PANDECT_DB_PORT=0
PANDECT_WEB_PORT=0
ENV
# Prevent inherited development/CI environment values from overriding isolation.
unset DJANGO_DEBUG DJANGO_SECRET_KEY DJANGO_ALLOWED_HOSTS POSTGRES_DB POSTGRES_USER
unset POSTGRES_PASSWORD PANDECT_DB_PORT PANDECT_WEB_PORT
compose up --build --wait --wait-timeout 180
compose exec -T web python manage.py migrate --check
compose exec -T web python manage.py shell <<'PY'
from django.contrib.auth import get_user_model
from cases.models import Case, CaseMembership

owner = get_user_model().objects.create_user(
    username="smoke", password="synthetic-smoke-password"
)
collaborator = get_user_model().objects.create_user(username="collaborator")
case = Case.objects.create(owner=owner)
CaseMembership.objects.create(case=case, user=collaborator)
PY
compose stop
compose up --wait --wait-timeout 180
compose exec -T web python manage.py shell <<'PY'
from django.contrib.auth import get_user_model
from cases.models import Case

owner = get_user_model().objects.get(username="smoke")
assert owner.check_password("synthetic-smoke-password")
case = Case.objects.get(owner=owner)
assert case.created_at is not None
membership = case.memberships.get()
assert membership.user.username == "collaborator"
assert membership.role == "viewer"
assert case.additional_users.get() == membership.user
PY
compose exec -T web python -c \
    'import json, urllib.request; assert json.load(urllib.request.urlopen("http://127.0.0.1:8000/health/")) == {"status": "ok"}'
