"""Synthetic current-field reports: missingness, byte domains and source links."""

from datetime import UTC, datetime, timedelta, timezone

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import Http404
from django.test import TestCase

from analysis.reports import report
from evidence.models import (
    AudioEvidence,
    CallEvidenceAttachment,
    DocumentEvidence,
    FaceTimeEvidence,
    ImageEvidence,
    PhoneCallEvidence,
    VehicleLocationEvidence,
    VehicleLocationImport,
)
from evidence.tests.test_views import EvidenceViewTestMixin
from people.models import Person
from vehicles.models import Vehicle

INSTANT = datetime(2026, 10, 27, 12, tzinfo=UTC)


class ExistingFieldReportTests(EvidenceViewTestMixin, TestCase):
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

    def test_calls_distinguish_unknown_zero_types_times_and_source_records(self):
        self.call(duration=None, occurred_at=None)
        zero = self.call(duration=timedelta(), occurred_at=INSTANT)
        video = self.call(
            FaceTimeEvidence,
            duration=timedelta(seconds=90),
            occurred_at=INSTANT.astimezone(timezone(timedelta(hours=-4))),
            is_archived=True,
        )
        result = report(self.case, "calls", state="all")
        metrics = result["metrics"]
        self.assertEqual(metrics["Selected records"], 3)
        self.assertEqual(metrics["Phone calls"], 2)
        self.assertEqual(metrics["FaceTime calls"], 1)
        self.assertEqual(metrics["Unknown duration"], 1)
        self.assertEqual(metrics["Zero duration"], 1)
        self.assertEqual(metrics["Reported duration total (seconds, known only)"], 90)
        self.assertEqual(metrics["Reported duration median (seconds, known only)"], 45)
        self.assertEqual(metrics["Unknown event time in selection"], 1)
        self.assertIn("12:00–12:59: 2", metrics["Recorded UTC hour counts"])
        self.assertEqual(len(result["items"]), 3)
        self.assertTrue(all(item["url"] for item in result["items"]))
        self.assertEqual(
            next(x for x in result["items"] if str(zero.pk) in x["url"])["values"][
                "Reported duration (seconds)"
            ],
            0,
        )
        self.assertIn(str(video.pk), str(result["items"]))
        self.assertEqual(report(self.case, "calls")["metrics"]["Selected records"], 2)
        selected = report(self.case, "calls", state="all", start=INSTANT, end=INSTANT)
        self.assertEqual(selected["metrics"]["Selected records"], 2)
        self.assertEqual(
            selected["metrics"]["Missing event time before period filter"], 1
        )
        self.assertIn("excluded", " ".join(selected["warnings"]))

    def test_same_named_participants_are_not_collapsed_to_one_pair(self):
        other = Person.objects.create(case=self.case, first_name=self.person.first_name)
        self.call()
        self.call(recipient=other)
        result = report(self.case, "calls", participant_id=self.person.pk)
        pair_counts = result["metrics"]["Participant pair counts"]
        self.assertIn(str(other.pk), pair_counts)
        self.assertIn(str(self.person.pk), pair_counts)
        self.assertEqual(pair_counts.count(": 1"), 2)

    def test_exact_participant_pair_includes_both_directions_and_self_pairs(self):
        second = Person.objects.create(case=self.case, first_name="Second person")
        third = Person.objects.create(case=self.case, first_name="Third person")
        forward = self.call(recipient=second, occurred_at=INSTANT)
        reverse = self.call(FaceTimeEvidence, sender=second)
        excluded = self.call(recipient=third)
        self_call = self.call()
        result = report(
            self.case,
            "calls",
            participant_id=self.person.pk,
            other_participant_id=second.pk,
        )
        self.assertEqual(result["metrics"]["Matching records"], 2)
        self.assertEqual(result["metrics"]["Selected records"], 2)
        self.assertEqual(result["metrics"]["Phone calls"], 1)
        self.assertEqual(result["metrics"]["FaceTime calls"], 1)
        self.assertEqual(result["metrics"]["Other participant filter"], str(second.pk))
        sources = " ".join(item["url"] for item in result["items"])
        self.assertIn(str(forward.pk), sources)
        self.assertIn(str(reverse.pk), sources)
        self.assertNotIn(str(excluded.pk), sources)
        self.assertNotIn(str(self_call.pk), sources)
        one_person = report(self.case, "calls", participant_id=self.person.pk)
        self.assertEqual(one_person["metrics"]["Selected records"], 4)
        self_pair = report(
            self.case,
            "calls",
            participant_id=self.person.pk,
            other_participant_id=self.person.pk,
        )
        self.assertEqual(self_pair["metrics"]["Selected records"], 1)
        self.assertIn(str(self_call.pk), self_pair["items"][0]["url"])
        period = report(
            self.case,
            "calls",
            participant_id=self.person.pk,
            other_participant_id=second.pk,
            start=INSTANT,
        )
        self.assertEqual(period["metrics"]["Selected records"], 1)
        self.assertEqual(
            period["metrics"]["Missing event time before period filter"], 1
        )

    def test_pair_filter_requires_both_same_case_participants(self):
        foreign = Person.objects.create(case=self.other_case, first_name="Private")
        for identifier in (foreign.pk, "not-a-uuid"):
            with self.subTest(identifier=identifier), self.assertRaises(Http404):
                report(
                    self.case,
                    "calls",
                    participant_id=self.person.pk,
                    other_participant_id=identifier,
                )
        with self.assertRaises(ValidationError):
            report(self.case, "calls", other_participant_id=self.person.pk)
        with self.assertRaises(ValidationError):
            report(
                self.case,
                "vehicle-coverage",
                participant_id=self.person.pk,
                other_participant_id=self.person.pk,
            )

    def test_vehicle_findings_preserve_unknown_zero_order_gaps_and_provenance(self):
        batch = self.batch()
        raw = {"Heading": " nw ", "Trigger": "Ignition On", "Time Summary": "51m"}
        first = self.observation(
            import_batch=batch,
            source_row=2,
            raw_row=raw,
            observed_at=INSTANT,
            received_at=INSTANT + timedelta(seconds=5),
            speed_kmh=0,
        )
        earlier = self.observation(
            import_batch=batch,
            source_row=3,
            raw_row={"Heading": "s"},
            observed_at=INSTANT - timedelta(hours=1),
            received_at=INSTANT - timedelta(hours=1, seconds=1),
            speed_kmh=10,
        )
        self.observation(
            import_batch=batch,
            source_row=4,
            raw_row={"Heading": ""},
            observed_at=earlier.observed_at,
        )
        self.observation(import_batch=batch, source_row=5)
        self.observation(
            import_batch=self.batch(),
            source_row=2,
            raw_row=raw,
            observed_at=INSTANT,
            received_at=INSTANT + timedelta(seconds=5),
            is_archived=True,
        )
        originals = list(VehicleLocationEvidence.objects.values_list("pk", "sha256"))
        result = report(
            self.case, "vehicle-coverage", state="all", vehicle_id=self.vehicle.pk
        )
        metrics = result["metrics"]
        self.assertEqual(metrics["Selected records"], 5)
        self.assertEqual(metrics["Missing observation time in selection"], 1)
        self.assertEqual(metrics["Zero reported speeds"], 1)
        self.assertEqual(metrics["Unknown reported speeds"], 3)
        self.assertEqual(metrics["Out-of-order CSV rows"], 1)
        self.assertEqual(metrics["Intervals of at least 30 minutes"], 1)
        self.assertEqual(metrics["Equal-time pairs"], 2)
        self.assertEqual(metrics["Duplicate raw-row groups"], 1)
        self.assertEqual(metrics["Negative receipt delays"], 1)
        self.assertEqual(metrics["Unknown receipt delays"], 2)
        self.assertEqual(metrics["Reported speed median (km/h)"], 5)
        self.assertIn("NW: 2", metrics["Reported heading counts"])
        source = next(x for x in result["items"] if str(first.pk) in x["url"])
        self.assertEqual(source["values"]["Raw heading"], " nw ")
        self.assertEqual(source["values"]["Raw time summary"], "51m")
        self.assertEqual(source["values"]["CSV row"], 2)
        self.assertTrue(
            any(str(batch.document_id) in x["url"] for x in source["related"])
        )
        out_of_order = next(x for x in result["items"] if str(earlier.pk) in x["url"])
        self.assertIn("precedes", out_of_order["text"])
        self.assertTrue(any(str(first.pk) in x["url"] for x in out_of_order["related"]))
        self.assertEqual(
            list(VehicleLocationEvidence.objects.values_list("pk", "sha256")), originals
        )
        self.assertIn("not inactivity", " ".join(result["warnings"]))

    def test_vehicle_empty_and_missing_event_times_do_not_become_zero(self):
        result = report(self.case, "vehicle-speed")
        self.assertEqual(result["items"], [])
        self.assertEqual(result["metrics"]["Reported speed minimum (km/h)"], "Unknown")
        self.observation()
        self.observation(observed_at=INSTANT)
        result = report(self.case, "vehicle-speed", start=INSTANT)
        self.assertEqual(result["metrics"]["Selected records"], 1)
        self.assertEqual(
            result["metrics"]["Missing observation time before period filter"], 1
        )
        self.assertEqual(
            result["items"][0]["values"]["Receipt minus event (seconds)"], "Unknown"
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

    def test_reports_bound_selections_and_do_not_claim_whole_case_completeness(self):
        self.call()
        self.call()
        result = report(self.case, "calls", limit=1)
        self.assertEqual(result["metrics"]["Matching records"], 2)
        self.assertEqual(result["metrics"]["Selected records"], 1)
        self.assertTrue(result["metrics"]["Truncated"])
        self.assertIn("Only the first 1 of 2", result["warnings"][0])

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
            {"state": "unknown"},
            {"limit": 0},
            {"limit": 501},
            {"limit": True},
            {"start": INSTANT.replace(tzinfo=None)},
            {"start": INSTANT, "end": INSTANT - timedelta(seconds=1)},
            {"vehicle_id": self.vehicle.pk},
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                report(self.case, **({"name": "calls"} | values))
        with self.assertRaises(ValidationError):
            report(self.case, "duplicates", participant_id=self.person.pk)
