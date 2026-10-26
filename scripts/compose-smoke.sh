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
from django.core.files.base import ContentFile
from cases.models import Case, CaseMembership
from evidence.models import AudioEvidence, DocumentEvidence, ImageEvidence, LocationEvidence, PersonLocationEvidence, TextEvidence
from people.models import Person
from vehicles.models import Vehicle

owner = get_user_model().objects.create_user(
    username="smoke", password="synthetic-smoke-password"
)
collaborator = get_user_model().objects.create_user(username="collaborator")
case = Case.objects.create(owner=owner)
CaseMembership.objects.create(case=case, user=collaborator)
person = Person.objects.create(case=case, first_name="Synthetic person")
Vehicle.objects.create(case=case, make="Synthetic Motors", license_plate="SYN-001")
for model in (ImageEvidence, AudioEvidence, DocumentEvidence):
    model.objects.create(
        case=case,
        uploaded_by=owner,
        title="Synthetic original",
        source="Synthetic smoke source",
        file=ContentFile(b"Synthetic smoke original\x00\r\n", name="original.bin"),
    )
TextEvidence.objects.create(
    case=case, uploaded_by=owner, title="Synthetic text", body="Original café\r\n",
    source="Synthetic smoke source",
)
LocationEvidence.objects.create(
    case=case, uploaded_by=owner, title="Synthetic location",
    source="Synthetic smoke source", address="Synthetic harbour office",
)
PersonLocationEvidence.objects.create(
    case=case, uploaded_by=owner, title="Synthetic person location",
    source="Synthetic smoke source", person=person, latitude="0", longitude="-79.123456",
)
PY
compose stop
compose up --wait --wait-timeout 180
compose exec -T web python manage.py shell <<'PY'
from django.contrib.auth import get_user_model
import hashlib
from uuid import UUID
from cases.models import Case
from evidence.models import AudioEvidence, DocumentEvidence, ImageEvidence, LocationEvidence, PersonLocationEvidence, TextEvidence
from people.models import Person
from vehicles.models import Vehicle

owner = get_user_model().objects.get(username="smoke")
assert isinstance(owner.pk, UUID)
assert owner.check_password("synthetic-smoke-password")
case = Case.objects.get(owner=owner)
assert isinstance(case.pk, UUID)
assert case.created_at is not None
membership = case.memberships.get()
assert isinstance(membership.pk, UUID)
assert isinstance(membership.user_id, UUID)
assert membership.user.username == "collaborator"
assert membership.role == "viewer"
assert case.additional_users.get() == membership.user
person = Person.objects.get(case=case)
assert isinstance(person.pk, UUID)
assert person.first_name == "Synthetic person"
vehicle = case.vehicles.get()
assert isinstance(vehicle.pk, UUID)
assert vehicle.make == "Synthetic Motors"
assert vehicle.license_plate == "SYN-001"
for model in (ImageEvidence, AudioEvidence, DocumentEvidence):
    evidence = model.objects.get(case=case)
    assert isinstance(evidence.pk, UUID)
    assert evidence.uploaded_by == owner
    assert evidence.source == "Synthetic smoke source"
    assert evidence.original_filename == "original.bin"
    with evidence.file.open("rb") as original:
        content = original.read()
    assert content == b"Synthetic smoke original\x00\r\n"
    assert evidence.byte_size == len(content)
    assert evidence.sha256 == hashlib.sha256(content).hexdigest()
text = TextEvidence.objects.get(case=case)
assert isinstance(text.pk, UUID)
assert text.body == "Original café\r\n"
assert text.sha256 == hashlib.sha256(text.body.encode("utf-8")).hexdigest()
assert text.source == "Synthetic smoke source"
for model in (LocationEvidence, PersonLocationEvidence):
    location = model.objects.get(case=case)
    assert isinstance(location.pk, UUID)
    assert location.source == "Synthetic smoke source"
    assert location.sha256 == hashlib.sha256(location.original_bytes()).hexdigest()
assert LocationEvidence.objects.get(case=case).address == "Synthetic harbour office"
assert PersonLocationEvidence.objects.get(case=case).person == person
PY
compose exec -T web python -c \
    'import json, urllib.request; assert json.load(urllib.request.urlopen("http://127.0.0.1:8000/health/")) == {"status": "ok"}'
compose exec -T web python <<'PY'
import os
import subprocess
import time
from urllib.error import URLError
from urllib.request import urlopen

# Exercise the actual non-debug WSGI/static path as the unprivileged image user.
process = subprocess.Popen(
    ["gunicorn", "config.wsgi:application", "--bind", "127.0.0.1:8001",
     "--logger-class", "config.privacy.PrivateAccessLogger", "--log-level", "warning"],
    env=os.environ | {"DJANGO_DEBUG": "false"},
)
try:
    for attempt in range(50):
        try:
            with urlopen("http://127.0.0.1:8001/health/", timeout=2) as response:
                assert response.status == 200
            break
        except URLError:
            if process.poll() is not None:
                raise RuntimeError("The production WSGI server exited.")
            time.sleep(0.1)
    else:
        raise RuntimeError("The production WSGI server did not become ready.")
    for path, expected in (
        ("/static/pandect/app.css", b"evidence-workspace"),
        ("/static/pandect/app.js", b"data-evidence-upload"),
        ("/static/pandect/icons/file-text.svg", b"<svg"),
        ("/accounts/login/", b"Welcome to Pandect"),
    ):
        with urlopen("http://127.0.0.1:8001" + path, timeout=5) as response:
            assert response.status == 200, path
            assert expected in response.read(), path
finally:
    process.terminate()
    process.wait(timeout=10)
PY
