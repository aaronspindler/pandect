#!/bin/sh
# Use only a fresh project and temporary configuration; never the developer's data.
set -eu
if [ "$#" -gt 1 ]; then
    printf 'Usage: %s [local-image]\n' "$0" >&2
    exit 2
fi
prebuilt_image=
if [ "$#" -eq 1 ]; then
    # Resolve a local reference once; never pull or rebuild a replacement image.
    prebuilt_image=$(docker image inspect --format '{{.Id}}' -- "$1")
    if ! printf '%s' "$prebuilt_image" | python3 -c 'import re, sys; sys.exit(not re.fullmatch(r"sha256:[a-f0-9]{64}", sys.stdin.read()))'; then
        printf 'The local image did not resolve to an immutable image ID.\n' >&2
        exit 1
    fi
fi
project="pandect-smoke-$(date +%s)-$$"
smoke_env=$(mktemp)
smoke_override=$(mktemp)
smoke_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
compose() {
    docker compose --file "$smoke_root/compose.yaml" --file "$smoke_override" --project-directory "$smoke_root" --project-name "$project" --env-file "$smoke_env" "$@"
}
cleanup() {
    result=$?
    if [ "$result" -ne 0 ]; then
        compose logs --no-color --tail 100 || true
    fi
    compose down --volumes --remove-orphans || true
    rm -f "$smoke_env" "$smoke_override"
}
trap cleanup EXIT
trap 'exit 1' INT TERM
# Exercise the production worker command from the same image as the web service.
cat > "$smoke_override" <<'YAML'
services:
  worker:
    command: ["sh", "/app/scripts/docker-entrypoint-worker.sh"]
YAML
if [ -n "$prebuilt_image" ]; then
    cat >> "$smoke_override" <<YAML
    image: $prebuilt_image
    pull_policy: never
  web:
    image: $prebuilt_image
    pull_policy: never
YAML
fi
cat > "$smoke_env" <<'ENV'
DJANGO_DEBUG=true
DJANGO_SECRET_KEY=synthetic-compose-smoke-secret-not-for-deployment
POSTGRES_DB=pandect_smoke
POSTGRES_USER=pandect_smoke
POSTGRES_PASSWORD=synthetic-smoke-password
POSTGRES_PORT=0
PANDECT_WEB_PORT=0
ENV
# Prevent inherited development/CI environment values from overriding isolation.
for variable in $(python3 -c 'import os; print(" ".join(key for key in os.environ if key.startswith(("DJANGO_", "POSTGRES_"))))'); do
    unset "$variable"
done
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
if [ -n "$prebuilt_image" ]; then
    compose up --no-build --wait --wait-timeout 180
else
    compose up --build --wait --wait-timeout 180
fi
web_image=$(docker inspect --format '{{.Image}}' "$(compose ps -q web)")
worker_image=$(docker inspect --format '{{.Image}}' "$(compose ps -q worker)")
test "$web_image" = "$worker_image"
if [ -n "$prebuilt_image" ]; then
    test "$web_image" = "$prebuilt_image"
fi
compose exec -T web python manage.py migrate --check
compose exec -T web python manage.py check_task_worker --timeout 30
compose exec -T web python manage.py shell <<'PY'
import base64
import io

from PIL import Image
from pypdf import PdfWriter
from audio.testing import m4a_bytes
from core.parsers import run_parser

writer = PdfWriter()
writer.add_blank_page(width=200, height=200)
content = io.BytesIO()
writer.write(content)
preview = run_parser("document_preview", content.getvalue(), options={"page": 1, "render": True})
assert preview["status"] == "complete" and preview["page_count"] == 1
with Image.open(io.BytesIO(base64.b64decode(preview["image"]))) as image:
    assert image.format == "JPEG" and image.size == (400, 400)
print("Bounded PDF rendering in the production container: OK")
recording = m4a_bytes(channels=2, rate=44100)
audio = run_parser("audio", recording)
assert audio["status"] == "complete" and audio["format"] == "M4A"
assert audio["channels"] == 2 and audio["sample_rate"] == 44100
assert run_parser("audio_preview", recording)["content_type"] == "audio/mp4"
chunk = run_parser("audio_chunk", recording, options={"start_frame": 0, "frame_count": 44100})
assert base64.b64decode(chunk["audio"])[:4] == b"RIFF"
print("Bounded AAC M4A decoding and WAV chunks in the production container: OK")
PY
compose stop worker
compose exec -T web python manage.py shell <<'PY'
from core.tasks import worker_probe
from django.tasks import TaskResultStatus

result = worker_probe.enqueue("compose-worker-restart")
result.refresh()
assert result.status == TaskResultStatus.READY
assert result.started_at is None
PY
compose exec -T web python manage.py shell <<'PY'
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from allauth.account.models import EmailAddress
from core.models import AnalysisRun
from core.analysis.services import start_run
from cases.models import Case, CaseMembership
from audio.models import AudioEvidence
from phone.models import CallEvidenceAttachment, FaceTimeEvidence, PhoneCallEvidence, TextMessageEvidence
from document.models import DocumentEvidence
from image.models import ImageEvidence
from geo.models import LocationEvidence, PersonLocationEvidence, VehicleLocationEvidence, VehicleLocationImport
from text.models import TextEvidence
from geo.vehicle_import_jobs import enqueue_preparation
from people.models import Person
from vehicle.models import Vehicle

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
client = Client()
client.force_login(owner)
response = client.post(
    reverse("core:evidence:create", args=[case.pk]),
    {
        "kind": "document", "title": "Synthetic analysis source", "submission_id": str(uuid4()),
        "file": SimpleUploadedFile(
            "analysis-native.txt", b"Source-anchored native extraction.\n", content_type="text/plain",
        ),
    },
    HTTP_ACCEPT="application/json", HTTP_HOST="localhost",
)
assert response.status_code == 201, response.content
run = AnalysisRun.objects.get(case=case, operation="native_text")
assert run.status == "queued" and run.inputs[0]["evidence_id"] == response.json()["id"]
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
from image.testing import plate_scene
plate_image = ImageEvidence.objects.create(
    case=case, uploaded_by=owner, title="Synthetic plate search source",
    file=ContentFile(plate_scene(), name="plate-scene.jpeg"),
)
plate_run, created = start_run(owner, case.pk, "plate_ocr", [
    {"kind": "image", "id": str(plate_image.pk)},
])
assert created and not plate_run.external_processing
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
    case=case, uploaded_by=owner, document=document, timezone="UTC", selected_vehicle=vehicle,
)
batch = enqueue_preparation(owner, case.pk, batch.pk)
assert batch.status == "queued"
assert not VehicleLocationEvidence.objects.exists()
PY
compose stop
compose up --no-build --wait --wait-timeout 180 db web
compose exec -T web python manage.py shell <<'PY'
from django.contrib.auth import get_user_model
import hashlib
from uuid import UUID
from cases.models import Case
from audio.models import AudioEvidence
from phone.models import CallEvidenceAttachment, FaceTimeEvidence, PhoneCallEvidence, TextMessageEvidence
from document.models import DocumentEvidence
from image.models import ImageEvidence
from geo.models import LocationEvidence, PersonLocationEvidence, VehicleLocationEvidence, VehicleLocationImport
from text.models import TextEvidence
from people.models import Person
from vehicle.models import Vehicle

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
batch = VehicleLocationImport.objects.get(case=case)
assert batch.status == "queued" and batch.phase == "prepare"
assert batch.completed_at is None
assert batch.selected_vehicle == vehicle
assert not batch.rows.exists()
assert not VehicleLocationEvidence.objects.filter(case=case).exists()
assert batch.document.sha256 == hashlib.sha256(batch.document.file.read()).hexdigest()
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
compose up --no-build -d --wait --wait-timeout 180 worker
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

from core.models import AnalysisRun, ExtractedPassage
ocr_run = AnalysisRun.objects.get(operation="ocr")
assert ocr_run.status in {"complete", "partial"}, ocr_run.error_code
assert not ocr_run.external_processing
assert "PANDECT LOCAL TEXT 12345" in " ".join(
    ExtractedPassage.objects.filter(result=ocr_run.result).values_list("text", flat=True)
)
assert ocr_run.result.artifacts.exists()
from core.models import GatewayRequest
assert not GatewayRequest.objects.filter(run=ocr_run).exists()
plate_run = AnalysisRun.objects.get(operation="plate_ocr")
assert plate_run.status == "partial", plate_run.error_code
assert not plate_run.external_processing
assert not GatewayRequest.objects.filter(run=plate_run).exists()
assert "TEST 482" in " ".join(
    ExtractedPassage.objects.filter(result=plate_run.result).values_list("text", flat=True)
)
for crop in plate_run.result.artifacts.exclude(metadata={}):
    finding = plate_run.result.data["items"][crop.metadata["finding_index"]]
    assert crop.sha256 == finding["values"]["Recognition JPEG SHA-256"]
run = AnalysisRun.objects.get(operation="native_text")
assert run.status == "complete", run.error_code
assert run.result.data["items"][0]["text"] == "Source-anchored native extraction.\n"
assert run.result.provenance["source_inputs"] == run.inputs
assert ExtractedPassage.objects.get(result=run.result).text == run.result.data["items"][0]["text"]
assert run.inputs[0]["sha256"] == run.case.document_documentevidence_items.get(
    original_filename="analysis-native.txt",
).sha256

# Prepare and publish through a separate worker after preserving the queued
# source across a full stack restart. No in-process import processing is used.
import hashlib
import json
import time
from django.contrib.auth import get_user_model
from geo.models import VehicleLocationEvidence, VehicleLocationImport
from geo.vehicle_import_jobs import enqueue_completion, process_import

batch = VehicleLocationImport.objects.get()
deadline = time.monotonic() + 30
while batch.status in {"queued", "running"} and time.monotonic() < deadline:
    time.sleep(0.1)
    batch.refresh_from_db()
assert batch.status == "review", (batch.status, batch.error_code)
assert batch.row_count == 1 and batch.rows.count() == 1
assert not VehicleLocationEvidence.objects.exists()
owner = get_user_model().objects.get(username="smoke")
batch = enqueue_completion(
    owner, batch.case_id, batch.pk, {"0": str(batch.selected_vehicle_id)},
)
deadline = time.monotonic() + 30
while batch.status in {"queued", "running"} and time.monotonic() < deadline:
    time.sleep(0.1)
    batch.refresh_from_db()
assert batch.status == "complete", (batch.status, batch.error_code)
assert batch.completed_at is not None
assert batch.vehicle_mapping == {"0": str(batch.selected_vehicle_id)}
location = VehicleLocationEvidence.objects.get(import_batch=batch)
assert not AnalysisRun.objects.filter(case_id=batch.case_id, inputs__contains=[
    {"kind": "vehicle-location", "evidence_id": str(location.pk)},
]).exists()
assert location.vehicle_id == batch.selected_vehicle_id
assert location.speed_kmh == 0
assert location.raw_row["Serial Number"] == "000001"
assert location.sha256 == hashlib.sha256(location.original_bytes()).hexdigest()
provenance = json.loads(location.original_bytes())["import"]
assert provenance["document_sha256"] == batch.document.sha256
assert provenance["document_id"] == str(batch.document_id)
assert provenance["row"] == 2 and provenance["timezone"] == "UTC"
assert process_import(str(batch.pk), str(batch.job_token)) == "complete"
assert VehicleLocationEvidence.objects.filter(import_batch=batch).count() == 1

# Verify native geometry dependencies in the bounded Linux parser, without HTTP.
import json
from core.parsers import run_parser
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
    'import json, os, urllib.request; assert json.load(urllib.request.urlopen("http://127.0.0.1:8000/health/")) == {"status": "ok", "version": os.environ["APP_VERSION"], "commit": os.environ["APP_COMMIT_SHA"]}'
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
        except (URLError, TimeoutError):
            if process.poll() is not None:
                raise RuntimeError("The production WSGI server exited.")
            time.sleep(0.1)
    else:
        raise RuntimeError("The production WSGI server did not become ready.")
    for path, expected in (
        ("/static/ui/app.css", b".global-header"),
        ("/static/core/workspace.css", b".evidence-workspace"),
        ("/static/core/evidence.js", b"data-evidence-upload"),
        ("/static/ui/icons/file-text.svg", b"<svg"),
        ("/accounts/login/", b'name="login"'),
    ):
        with urlopen("http://127.0.0.1:8001" + path, timeout=5) as response:
            assert response.status == 200, path
            assert expected in response.read(), path
finally:
    process.terminate()
    process.wait(timeout=10)
PY
