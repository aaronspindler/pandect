#!/bin/sh
# Use only a fresh project and temporary configuration; never the developer's data.
set -eu
project="pandect-smoke-$(date +%s)-$$"
smoke_env=$(mktemp)
smoke_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
compose() {
    docker compose --file "$smoke_root/compose.yaml" --project-directory "$smoke_root" --project-name "$project" --env-file "$smoke_env" "$@"
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
unset POSTGRES_PASSWORD
unset COMPOSE_FILE COMPOSE_PROJECT_NAME COMPOSE_PROFILES COMPOSE_ENV_FILES
# Include future settings too; never carry operator endpoints or tokens into tests.
for variable in $(python3 -c 'import os; print(" ".join(key for key in os.environ if key.startswith("PANDECT_")))'); do
    unset "$variable"
done
# Verify every disposable volume is local to this unique synthetic project.
compose config --format json | python3 -c '
import json, sys
config = json.load(sys.stdin)
project = config["name"]
assert project.startswith("pandect-smoke-")
assert set(config["volumes"]) == {"postgres_data", "app_data"}
for key, volume in config["volumes"].items():
    assert not volume.get("external", False)
    assert volume["name"] == f"{project}_{key}"
print("Verified synthetic project and scoped disposable volumes:", project)
'
compose up --build --wait --wait-timeout 180
compose exec -T web python manage.py migrate --check
compose exec -T web python manage.py check_task_worker --timeout 30
compose stop worker
compose exec -T web python manage.py shell <<'PY'
from background.tasks import worker_probe
from django.tasks import TaskResultStatus

result = worker_probe.enqueue("compose-worker-restart")
result.refresh()
assert result.status == TaskResultStatus.READY
assert result.started_at is None
PY
compose exec -T web python manage.py shell <<'PY'
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from allauth.account.models import EmailAddress
from analysis.services import start_run
from cases.models import Case, CaseMembership
from evidence.models import AudioEvidence, CallEvidenceAttachment, DocumentEvidence, FaceTimeEvidence, ImageEvidence, LocationEvidence, PersonLocationEvidence, PhoneCallEvidence, TextEvidence, TextMessageEvidence, VehicleLocationEvidence, VehicleLocationImport
from evidence.vehicle_imports import parse_csv
from django.utils import timezone
from people.models import Person
from vehicles.models import Vehicle

owner = get_user_model().objects.create_user(
    username="smoke", email="smoke@example.test", password="synthetic-smoke-password"
)
EmailAddress.objects.create(user=owner, email=owner.email, verified=True, primary=True)
collaborator = get_user_model().objects.create_user(username="collaborator")
case = Case.objects.create(owner=owner)
CaseMembership.objects.create(case=case, user=collaborator)
person = Person.objects.create(case=case, first_name="Synthetic person")
vehicle = Vehicle.objects.create(case=case, make="Synthetic Motors", license_plate="SYN-001")
for model in (ImageEvidence, AudioEvidence, DocumentEvidence):
    model.objects.create(
        case=case,
        uploaded_by=owner,
        title="Synthetic original",
        source="Synthetic smoke source",
        file=ContentFile(b"Synthetic smoke original\x00\r\n", name="original.bin"),
    )
native_document = DocumentEvidence.objects.create(
    case=case, uploaded_by=owner, title="Synthetic analysis source",
    file=ContentFile(b"Source-anchored native extraction.\n", name="analysis-native.txt"),
)
run, created = start_run(owner, case.pk, "native_text", [
    {"kind": "document", "id": str(native_document.pk)},
])
assert created and run.status == "queued"
# Real local OCR must run after restart without AI credentials.
import io
from PIL import Image, ImageDraw, ImageFont
image = Image.new("RGB", (1000, 240), "white")
ImageDraw.Draw(image).text(
    (40, 60), "PANDECT LOCAL TEXT 12345",
    font=ImageFont.load_default(size=48), fill="black",
)
image_bytes = io.BytesIO()
image.save(image_bytes, format="PNG")
local_image = ImageEvidence.objects.create(
    case=case, uploaded_by=owner, title="Synthetic local OCR source",
    file=ContentFile(image_bytes.getvalue(), name="local-ocr.png"),
)
ocr_run, created = start_run(owner, case.pk, "ocr", [
    {"kind": "image", "id": str(local_image.pk)},
])
assert created and not ocr_run.external_processing
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
TextMessageEvidence.objects.create(
    case=case, uploaded_by=owner, title="Synthetic message", sender=person,
    recipient=person, body="Message café\r\n",
)
for model, parent in ((PhoneCallEvidence, "phone_call"), (FaceTimeEvidence, "facetime")):
    call = model.objects.create(
        case=case, uploaded_by=owner, title="Synthetic call", sender=person, recipient=person,
    )
    CallEvidenceAttachment.objects.create(
        case=case, uploaded_by=owner, **{parent: call}, transcript="Transcript café\r\n",
        file=ContentFile(b"Synthetic call audio\x00\r\n", name="call.wav"),
    )
csv_bytes = (b'Asset,Serial Number,Address,Speed,Heading,Trigger,Time Summary,Created Time,Received Time\r\n'
    b'Synthetic van,000001,"Synthetic street, Example City",0 km/h,NW,Parked,51m,"2026-09-16, 2:57:22 PM","2026-09-16, 2:57:25 PM"\r\n')
document = DocumentEvidence.objects.create(
    case=case, uploaded_by=owner, title="Synthetic CSV source",
    file=ContentFile(csv_bytes, name="synthetic.csv"),
)
batch = VehicleLocationImport.objects.create(
    case=case, uploaded_by=owner, document=document, timezone="UTC",
)
row = parse_csv(csv_bytes, "UTC")[0]
VehicleLocationEvidence.objects.create(
    case=case, uploaded_by=owner, title="Synthetic vehicle location",
    vehicle=vehicle, import_batch=batch, source_row=row.number,
    raw_row=row.raw, **row.values,
)
batch.completed_at = timezone.now()
batch.vehicle_mapping = {"0": str(vehicle.pk)}
batch.save()
PY
compose stop
compose up --wait --wait-timeout 180 db web
compose exec -T web python manage.py shell <<'PY'
from django.contrib.auth import get_user_model
import hashlib
from uuid import UUID
from cases.models import Case
from evidence.models import AudioEvidence, CallEvidenceAttachment, DocumentEvidence, FaceTimeEvidence, ImageEvidence, LocationEvidence, PersonLocationEvidence, PhoneCallEvidence, TextEvidence, TextMessageEvidence, VehicleLocationEvidence, VehicleLocationImport
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
    evidence = model.objects.get(case=case, original_filename="original.bin")
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
message = TextMessageEvidence.objects.get(case=case)
assert message.body == "Message café\r\n"
for model in (TextMessageEvidence, PhoneCallEvidence, FaceTimeEvidence):
    communication = model.objects.get(case=case)
    assert isinstance(communication.pk, UUID)
    assert communication.sender == person and communication.recipient == person
    assert communication.sha256 == hashlib.sha256(communication.original_bytes()).hexdigest()
for addition in CallEvidenceAttachment.objects.filter(case=case):
    assert isinstance(addition.pk, UUID)
    assert addition.transcript == "Transcript café\r\n"
    assert addition.file.read() == b"Synthetic call audio\x00\r\n"
    assert addition.audio_sha256 == hashlib.sha256(b"Synthetic call audio\x00\r\n").hexdigest()
    assert addition.sha256 == hashlib.sha256(addition.original_bytes()).hexdigest()
assert CallEvidenceAttachment.objects.filter(case=case).count() == 2
location = VehicleLocationEvidence.objects.get(case=case)
assert location.vehicle == vehicle
assert location.speed_kmh == 0
assert location.sha256 == hashlib.sha256(location.original_bytes()).hexdigest()
batch = VehicleLocationImport.objects.get(case=case)
assert batch.completed_at is not None
assert batch.vehicle_mapping == {"0": str(vehicle.pk)}
assert batch.document.sha256 == hashlib.sha256(batch.document.file.read()).hexdigest()
assert location.raw_row["Serial Number"] == "000001"
PY
compose exec -T web python manage.py shell <<'PY'
from django.tasks import TaskResultStatus
from django_tasks_db.models import DBTaskResult

result = DBTaskResult.objects.get(
    args_kwargs={"args": ["compose-worker-restart"], "kwargs": {}},
)
assert result.status == TaskResultStatus.READY
assert result.started_at is None
PY
compose up -d --wait --wait-timeout 180 worker
compose exec -T web python manage.py check_task_worker --timeout 30
compose exec -T web python manage.py shell <<'PY'
from django.tasks import TaskResultStatus
from django_tasks_db.models import DBTaskResult

result = DBTaskResult.objects.get(
    args_kwargs={"args": ["compose-worker-restart"], "kwargs": {}},
)
assert result.status == TaskResultStatus.SUCCESSFUL
assert result.return_value == "compose-worker-restart"
assert len(result.worker_ids) == 1

from analysis.models import AnalysisRun, ExtractedPassage
ocr_run = AnalysisRun.objects.get(operation="ocr")
assert ocr_run.status in {"complete", "partial"}, ocr_run.error_code
assert not ocr_run.external_processing
assert "PANDECT LOCAL TEXT 12345" in " ".join(
    ExtractedPassage.objects.filter(result=ocr_run.result).values_list("text", flat=True)
)
assert ocr_run.result.artifacts.exists()
from analysis.models import GatewayRequest
assert not GatewayRequest.objects.filter(run=ocr_run).exists()
run = AnalysisRun.objects.get(operation="native_text")
assert run.status == "complete", run.error_code
assert run.result.data["items"][0]["text"] == "Source-anchored native extraction.\n"
assert run.result.provenance["source_inputs"] == run.inputs
assert ExtractedPassage.objects.get(result=run.result).text == run.result.data["items"][0]["text"]
assert run.inputs[0]["sha256"] == run.case.evidence_documentevidence_items.get(
    original_filename="analysis-native.txt",
).sha256

# Verify native geometry dependencies in the bounded Linux parser, without HTTP.
import json
from analysis.parsers import run_parser
context = run_parser("geographic_context", json.dumps({"elements": [{
    "type": "node", "id": 1, "lat": 43.65, "lon": -79.38,
    "tags": {"amenity": "cafe", "name": "Synthetic map reference"},
}]}).encode(), options={
    "center": [43.65, -79.38], "points": [[43.65, -79.38]],
    "radius_m": 500, "boundary_m": 10,
})
assert context["matches"][0]["feature_id"] == "node/1"
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
