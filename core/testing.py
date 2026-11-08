"""Synthetic case and evidence fixtures shared by domain integration tests."""

from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from audio.models import AudioEvidence
from cases.models import Case, CaseMembership
from core.evidence.types import COMMUNICATION_KINDS, LOCATION_KINDS
from document.models import DocumentEvidence
from geo.models import LocationEvidence, PersonLocationEvidence, VehicleLocationEvidence
from image.models import ImageEvidence
from people.models import Person
from phone.models import FaceTimeEvidence, PhoneCallEvidence, TextMessageEvidence
from text.models import TextEvidence
from vehicle.models import Vehicle

FILE_MODELS = (ImageEvidence, AudioEvidence, DocumentEvidence)


LOCATION_MODELS = (LocationEvidence, PersonLocationEvidence, VehicleLocationEvidence)


COMMUNICATION_MODELS = (TextMessageEvidence, PhoneCallEvidence, FaceTimeEvidence)


ALL_MODELS = (*FILE_MODELS, TextEvidence, *LOCATION_MODELS, *COMMUNICATION_MODELS)


ORIGINAL = b"Synthetic original material\x00\r\n"


class EvidenceTestMixin:
    @classmethod
    def setUpTestData(cls):
        cls.owner = get_user_model().objects.create_user(username="owner")
        cls.editor = get_user_model().objects.create_user(username="editor")
        cls.viewer = get_user_model().objects.create_user(username="viewer")
        cls.outsider = get_user_model().objects.create_user(username="outsider")
        cls.case = Case.objects.create(owner=cls.owner)
        cls.other_case = Case.objects.create(owner=cls.outsider)
        cls.person = Person.objects.create(case=cls.case, first_name="Synthetic person")
        cls.vehicle = Vehicle.objects.create(case=cls.case, model="Synthetic van")
        CaseMembership.objects.create(
            case=cls.case, user=cls.editor, role=CaseMembership.Role.EDITOR
        )
        CaseMembership.objects.create(case=cls.case, user=cls.viewer)

    def setUp(self):
        directory = TemporaryDirectory()
        self.media_root = Path(directory.name)
        self.addCleanup(directory.cleanup)
        settings = self.settings(MEDIA_ROOT=self.media_root)
        settings.enable()
        self.addCleanup(settings.disable)

    def new_evidence(self, model=TextEvidence, **overrides):
        values = {
            "case": self.case,
            "uploaded_by": self.editor,
            "title": "Synthetic material",
        }
        if model is TextEvidence:
            values["body"] = "Original café\r\nincluding spacing.  "
        elif model in COMMUNICATION_MODELS:
            values |= {"sender": self.person, "recipient": self.person}
            if model is TextMessageEvidence:
                values["body"] = "  Original message.\r\n"
        elif model in LOCATION_MODELS:
            values["address"] = "  Synthetic harbour office\r\n"
            if model is PersonLocationEvidence:
                values["person"] = self.person
            elif model is VehicleLocationEvidence:
                values["vehicle"] = self.vehicle
        else:
            values["file"] = SimpleUploadedFile(
                "original.bin", ORIGINAL, content_type="application/octet-stream"
            )
        return model(**(values | overrides))

    def create_evidence(self, model=TextEvidence, **overrides):
        evidence = self.new_evidence(model, **overrides)
        evidence.save()
        return evidence


class EvidenceViewTestMixin:
    @classmethod
    def setUpTestData(cls):
        cls.users = {}
        for role in ("owner", "editor", "viewer", "outsider"):
            user = get_user_model().objects.create_user(
                username=role,
                email=f"{role}@example.test",
                password="synthetic-password",
            )
            EmailAddress.objects.create(
                user=user, email=user.email, verified=True, primary=True
            )
            cls.users[role] = user
        cls.case = Case.objects.create(owner=cls.users["owner"], name="Harbour records")
        cls.other_case = Case.objects.create(
            owner=cls.users["outsider"], name="Other case"
        )
        cls.person = Person.objects.create(case=cls.case, first_name="Synthetic person")
        cls.vehicle = Vehicle.objects.create(case=cls.case, model="Synthetic van")
        CaseMembership.objects.create(
            case=cls.case, user=cls.users["editor"], role="editor"
        )
        CaseMembership.objects.create(
            case=cls.case, user=cls.users["viewer"], role="viewer"
        )

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.media_root = Path(directory.name)
        settings = self.settings(MEDIA_ROOT=directory.name)
        settings.enable()
        self.addCleanup(settings.disable)

    def url(self, name, item=None, kind="text", case=None):
        kwargs = {"case_id": (case or self.case).pk}
        if item:
            kwargs |= {"kind": kind, "evidence_id": item.pk}
        namespace = (
            "phone"
            if name in ("add_attachment", "attachment_download")
            else "core:evidence"
        )
        return reverse(f"{namespace}:{name}", kwargs=kwargs)

    def login(self, role):
        self.client.force_login(self.users[role])

    def text(self, **values):
        return TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Interview notes",
            body="Original café\r\n  preserved.  ",
            **values,
        )

    def payload(self, kind="text", **overrides):
        data = {
            "kind": kind,
            "submission_id": str(uuid4()),
            "title": "New source",
            "description": "Investigator description",
            "source": "Synthetic witness",
            "source_description": "Synthetic source",
        }
        if kind == "text":
            data["body"] = "  Original café\r\nunchanged.  "
        elif kind in COMMUNICATION_KINDS:
            data |= {"sender": str(self.person.pk), "recipient": str(self.person.pk)}
            if kind == "text-message":
                data["body"] = "  One original message.\r\n"
        elif kind in LOCATION_KINDS:
            data["address"] = "  Synthetic harbour office\r\n"
            if kind == "person-location":
                data["person"] = str(self.person.pk)
            elif kind == "vehicle-location":
                data["vehicle"] = str(self.vehicle.pk)
        else:
            data["file"] = SimpleUploadedFile(
                "original.bin",
                f"Original {kind}\x00\r\n".encode(),
                content_type="application/octet-stream",
            )
        return data | overrides
