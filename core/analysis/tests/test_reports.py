"""Synthetic current-field reports: missingness, byte domains and source links."""

from datetime import UTC, datetime, timedelta

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import Http404
from django.test import TestCase
from django.urls import reverse
from django.utils.html import strip_tags

from audio.models import AudioEvidence
from core.analysis.reports import report
from core.tests.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence
from geo.models import VehicleLocationEvidence, VehicleLocationImport
from image.models import ImageEvidence
from people.models import Person
from phone.models import CallEvidenceAttachment, FaceTimeEvidence, PhoneCallEvidence
from vehicle.models import Vehicle

INSTANT = datetime(2026, 10, 27, 12, tzinfo=UTC)


class ExistingFieldReportTests(EvidenceViewTestMixin, TestCase):
    def test_call_report_displays_names_for_filters_and_pair_counts(self):
        recipient = Person.objects.create(case=self.case, first_name="Recipient")
        self.call(recipient=recipient)
        self.login("viewer")
        response = self.client.get(
            reverse("core:analysis:report", args=[self.case.pk, "calls"]),
            {"participant_id": self.person.pk, "other_participant_id": recipient.pk},
        )
        self.assertEqual(response.status_code, 200)
        content = strip_tags(response.content.decode())
        self.assertIn(str(self.person), content)
        self.assertIn(str(recipient), content)
        self.assertLess(
            content.index("Calls by participant pair"), content.index("Report totals")
        )
        self.assertNotIn(str(self.person.pk), content)
        self.assertNotIn(str(recipient.pk), content)

    def call(self, model=PhoneCallEvidence, **values):
        if values.get("is_archived"):
            values |= {
                "archive_changed_at": INSTANT,
                "archive_changed_by": self.users["owner"],
            }
        return model.objects.create(
            **(
                {
                    "case": self.case,
                    "uploaded_by": self.users["owner"],
                    "title": "Synthetic call",
                    "sender": self.person,
                    "recipient": self.person,
                }
                | values
            )
        )

    def batch(self):
        document = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic CSV",
            file=SimpleUploadedFile("synthetic.csv", b"retained csv bytes"),
        )
        return VehicleLocationImport.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            document=document,
            timezone="UTC",
        )

    def observation(self, **values):
        if values.get("is_archived"):
            values |= {
                "archive_changed_at": INSTANT,
                "archive_changed_by": self.users["owner"],
            }
        return VehicleLocationEvidence.objects.create(
            **(
                {
                    "case": self.case,
                    "uploaded_by": self.users["owner"],
                    "title": "Synthetic observation",
                    "vehicle": self.vehicle,
                    "address": "10 Example Street",
                }
                | values
            )
        )

    def attachment(self, call, content=b"same bytes", **values):
        return CallEvidenceAttachment.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            **{
                "phone_call"
                if isinstance(call, PhoneCallEvidence)
                else "facetime": call
            },
            file=SimpleUploadedFile("call.wav", content),
            **values,
        )

    def test_file_duplicate_groups_use_byte_hashes_across_types_and_call_audio(self):
        for model, name in (
            (ImageEvidence, "first.png"),
            (AudioEvidence, "second.wav"),
        ):
            model.objects.create(
                case=self.case,
                uploaded_by=self.users["owner"],
                title=name,
                file=SimpleUploadedFile(name, b"same bytes"),
            )
        different = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Different bytes",
            file=SimpleUploadedFile("different.bin", b"different bytes"),
        )
        phone = self.call()
        phone_audio = self.attachment(phone, transcript="independent supplied text")
        video_audio = self.attachment(self.call(FaceTimeEvidence))
        # A metadata-record hash is deliberately equal: it must not be counted.
        PhoneCallEvidence.objects.filter(pk=phone.pk).update(
            sha256=phone_audio.audio_sha256
        )
        CallEvidenceAttachment.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            phone_call=phone,
            transcript="Transcript with no recording",
        )
        result = report(self.case, "duplicates")
        self.assertEqual(result["metrics"]["Matching records"], 5)
        self.assertEqual(result["metrics"]["Duplicate byte groups"], 1)
        self.assertEqual(result["metrics"]["Occurrences in duplicate groups"], 4)
        self.assertNotIn(str(different.pk), str(result["items"]))
        self.assertTrue(
            any(f"#attachment-{phone_audio.pk}" in x["url"] for x in result["items"])
        )
        self.assertTrue(
            any(f"#attachment-{video_audio.pk}" in x["url"] for x in result["items"])
        )
        self.assertTrue(
            all(
                x["values"]["SHA-256"] == phone_audio.audio_sha256
                for x in result["items"]
            )
        )
        self.assertNotEqual(phone_audio.sha256, phone_audio.audio_sha256)
        self.assertEqual(ImageEvidence.objects.count(), 1)
        self.assertEqual(CallEvidenceAttachment.objects.count(), 3)
        self.login("viewer")
        response = self.client.get(
            reverse("core:analysis:report", args=[self.case.pk, "duplicates"])
        )
        self.assertContains(response, "Group 1 · 4 identical files")
        self.assertContains(
            response,
            '<details class="disclosure analysis-report-checksum">',
        )
        self.assertContains(response, f"<code>{phone_audio.audio_sha256}</code>")

    def test_foreign_file_audio_and_import_provenance_never_enter_reports(self):
        original = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Authorized file",
            file=SimpleUploadedFile("authorized.bin", b"same bytes"),
        )
        foreign = DocumentEvidence.objects.create(
            case=self.other_case,
            uploaded_by=self.users["outsider"],
            title="Private other-case file",
            file=SimpleUploadedFile("private.bin", b"same bytes"),
        )
        attachment = self.attachment(self.call())
        CallEvidenceAttachment.objects.filter(pk=attachment.pk).update(
            case=self.other_case
        )
        duplicate_report = report(self.case, "duplicates")
        self.assertEqual(duplicate_report["metrics"]["Matching records"], 1)
        self.assertEqual(duplicate_report["metrics"]["Duplicate byte groups"], 0)
        batch = self.batch()
        imported = self.observation(import_batch=batch, source_row=2)
        VehicleLocationImport.objects.filter(pk=batch.pk).update(document=foreign)
        vehicle_report = report(self.case, "vehicle-coverage")
        self.assertEqual(vehicle_report["items"], [])
        self.assertNotIn(str(imported.pk), str(vehicle_report))
        original.refresh_from_db()
        self.assertEqual(original.file.read(), b"same bytes")

    def test_cross_case_and_inconsistent_related_records_are_excluded(self):
        other_person = Person.objects.create(case=self.other_case, first_name="Private")
        other_vehicle = Vehicle.objects.create(case=self.other_case, model="Private")
        self.call(
            case=self.other_case,
            uploaded_by=self.users["outsider"],
            sender=other_person,
            recipient=other_person,
        )
        forged_call = self.call()
        PhoneCallEvidence.objects.filter(pk=forged_call.pk).update(sender=other_person)
        forged_location = self.observation()
        VehicleLocationEvidence.objects.filter(pk=forged_location.pk).update(
            vehicle=other_vehicle
        )
        self.assertEqual(report(self.case, "calls")["items"], [])
        self.assertEqual(report(self.case, "vehicle-coverage")["items"], [])
        for values in (
            {"participant_id": other_person.pk},
            {"vehicle_id": other_vehicle.pk},
        ):
            with self.subTest(values=values), self.assertRaises(Http404):
                report(self.case, "calls", **values)
        for values in ({"participant_id": "not-a-uuid"}, {"vehicle_id": "not-a-uuid"}):
            with self.subTest(values=values), self.assertRaises(Http404):
                report(self.case, "calls", **values)

    def test_invalid_report_options_are_rejected(self):
        for values in (
            {"name": "unknown"},
            {"page_size": 0},
            {"page_size": 26},
            {"page_size": True},
            {"start": INSTANT.replace(tzinfo=None)},
            {"start": INSTANT, "end": INSTANT - timedelta(seconds=1)},
            {"vehicle_id": self.vehicle.pk},
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                report(self.case, **({"name": "calls"} | values))
        with self.assertRaises(ValidationError):
            report(self.case, "duplicates", participant_id=self.person.pk)

    def test_duplicate_groups_compare_files_beyond_the_old_selection_limit(self):
        documents = DocumentEvidence.objects.bulk_create(
            [
                DocumentEvidence(
                    case=self.case,
                    uploaded_by=self.users["owner"],
                    title=f"File {number}",
                    file=f"synthetic/{number}.bin",
                    original_filename=f"file-{number}.bin",
                    byte_size=1,
                    sha256=f"{number if number < 1002 else 0:064x}",
                )
                for number in range(1003)
            ]
        )
        result = report(self.case, "duplicates")
        self.assertEqual(result["metrics"]["Selected records"], 1003)
        self.assertEqual(result["metrics"]["Duplicate byte groups"], 1)
        self.assertEqual(result["metrics"]["Occurrences in duplicate groups"], 2)
        self.assertEqual(
            {item["values"]["Source ID"] for item in result["items"]},
            {str(documents[0].pk), str(documents[-1].pk)},
        )
        DocumentEvidence.objects.filter(case=self.case).update(sha256="a" * 64)
        last = report(self.case, "duplicates", page=41)
        self.assertEqual(len(last["items"]), 3)
        self.assertEqual(last["page_obj"].paginator.count, 1003)
        self.assertTrue(
            all(item["values"]["Group occurrences"] == 1003 for item in last["items"])
        )
