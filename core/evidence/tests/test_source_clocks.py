from datetime import UTC, datetime, timedelta

from django.core.files.base import ContentFile
from django.test import TestCase

from core.analysis.services import process_run, start_run
from core.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence
from geo.models import VehicleLocationEvidence, VehicleLocationImport
from geo.testing import csv_content, parse_csv
from phone.models import CommunicationContext, PhoneCallEvidence, TextMessageEvidence


class SourceClockTests(EvidenceViewTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.login("viewer")
        self.common = {"case": self.case, "uploaded_by": self.case.owner}
        self.parties = {"sender": self.person, "recipient": self.person}
        self.instant = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)

    def document(self, data):
        return DocumentEvidence.objects.create(
            **self.common,
            title="Clock source",
            file=ContentFile(data, name="export.csv"),
        )

    def test_competing_offset_and_raw_export_clock_around_fold(self):
        raw = "2026-11-01T01:30:00-04:00"
        document = self.document(raw.encode())
        message = TextMessageEvidence.objects.create(
            **self.common,
            **self.parties,
            title="First 01:30",
            body="Clock claim",
            occurred_at=self.instant,
            collected_at=self.instant + timedelta(days=1),
        )
        context = CommunicationContext.objects.create(
            case=self.case,
            message=message,
            source_document=document,
            source_sha256=document.sha256,
            source_row=2,
            source_start=0,
            source_end=len(raw),
            parser_version="imessage-csv-v1",
            timezone="America/Winnipeg",
            raw_sender="Sender",
            raw_timestamp=raw,
            source_context={"raw_fields": {"Read Date": "2026-11-01T01:00:00-05:00"}},
        )
        self.users["viewer"].timezone = "America/Toronto"
        self.users["viewer"].save()
        second = TextMessageEvidence.objects.create(
            **self.common,
            **self.parties,
            title="Second 01:30",
            body="Second",
            occurred_at=self.instant + timedelta(hours=1),
        )
        response = self.client.get(self.url("chronology"), {"category": "all"})
        rows = response.context["rows"]
        self.assertEqual([r["id"] for r in rows], [message.pk, second.pk, message.pk])
        for phrase in (
            "01:30:00 EDT (-0400)",
            "01:30:00 EST (-0500)",
            'datetime="2026-11-01T01:30:00-04:00"',
            'datetime="2026-11-01T01:30:00-05:00"',
            raw,
            "America/Winnipeg",
            "Supplied offset differs",
            "Read Date as supplied",
            "2026-11-01T01:00:00-05:00",
            "Retained export · Row 2",
            "imessage-csv-v1",
        ):
            self.assertContains(response, phrase)
        self.assertNotIn("export_url", rows[2])
        self.assertContains(self.client.get(rows[0]["export_url"]), document.title)
        CommunicationContext.objects.filter(pk=context.pk).update(
            source_sha256="1" * 64
        )
        response = self.client.get(self.url("chronology"))
        self.assertNotContains(response, "Message Date as supplied")
        self.assertNotContains(response, "America/Winnipeg")

    def test_date_only_message_preserves_its_source_precision(self):
        raw = "2026-11-01"
        message = TextMessageEvidence.objects.create(
            **self.common,
            **self.parties,
            title="Only a day",
            body="Text",
            occurred_at=datetime(2026, 11, 1, tzinfo=UTC),
        )
        document = self.document(raw.encode())
        CommunicationContext.objects.create(
            case=self.case,
            message=message,
            source_document=document,
            source_sha256=document.sha256,
            source_row=2,
            source_start=0,
            source_end=len(raw),
            parser_version="imessage-csv-v1",
            timezone="UTC",
            raw_sender="Sender",
            raw_timestamp=raw,
        )
        for query in ({}, {"view": "day", "day": raw}):
            with self.subTest(query=query):
                response = self.client.get(self.url("chronology"), query)
                self.assertNotContains(response, "Only a day")
                self.assertEqual(response.context["rows"], [])
                self.assertNotContains(response, "00:00:00 UTC")

        other = TextMessageEvidence.objects.create(
            **self.common,
            **self.parties,
            title="Timed message",
            body="Next",
            occurred_at=message.occurred_at + timedelta(minutes=2),
        )
        run, _ = start_run(
            self.case.owner,
            self.case.pk,
            "sessions",
            [{"kind": "text-message", "id": str(item.pk)} for item in (message, other)],
        )
        process_run(run.pk)
        self.users["viewer"].timezone = "America/Toronto"
        self.users["viewer"].save(update_fields=["timezone"])
        response = self.client.get(
            self.url("chronology"), {"view": "day", "day": "2026-10-31"}
        )
        self.assertNotContains(response, "Inferred session")
        self.assertNotContains(response, "20:00:00")
        self.assertEqual([r["id"] for r in response.context["rows"]], [other.pk])
        unplaced = self.client.get(self.url("chronology"), {"view": "unplaced"})
        self.assertContains(unplaced, "Only a day")
        info = next(
            row for row in unplaced.context["originals"] if row["id"] == message.pk
        )
        self.assertEqual(info["source_date"].isoformat(), raw)
        self.assertEqual(info["source_zone"], "UTC")
        self.assertContains(unplaced, "01 Nov 2026")

    def test_calculated_call_ends_distinguish_zero_overlap_and_missing_duration(self):
        for title, minutes, duration in (
            ("Ends later", 0, timedelta(hours=1)),
            ("Inside another interval", 1, timedelta()),
            ("No duration", 2, None),
        ):
            PhoneCallEvidence.objects.create(
                **self.common,
                **self.parties,
                title=title,
                occurred_at=self.instant + timedelta(minutes=minutes),
                duration=duration,
            )
        for query in ({}, {"view": "day", "day": "2026-11-01"}):
            with self.subTest(query=query):
                response = self.client.get(self.url("chronology"), query)
                self.assertContains(response, "Calculated end", count=2)
                self.assertContains(response, "06:30:00 UTC")
                self.assertContains(response, "Duration 0:00:00", count=1)
                rows = response.context["rows"]
                self.assertEqual(rows[0]["end_at"], self.instant + timedelta(hours=1))
                self.assertEqual(rows[1]["end_at"], self.instant + timedelta(minutes=1))
                self.assertNotIn("end_at", rows[2])

    def test_vehicle_receipt_export_and_parser_stay_pinned_to_observation(self):
        data = csv_content(**{"Received Time": "2026-09-16, 2:57:20 PM"})
        zone = "America/Toronto"
        source = parse_csv(data, zone)[0]
        document = self.document(data)
        batch = VehicleLocationImport.objects.create(
            **self.common,
            document=document,
            timezone=zone,
        )
        VehicleLocationEvidence.objects.create(
            **self.common,
            **source.values,
            title="Vehicle observation",
            vehicle=self.vehicle,
            import_batch=batch,
            source_row=source.number,
            raw_row=source.raw,
        )
        response = self.client.get(self.url("chronology"))
        for phrase in (
            "Created Time as supplied",
            source.raw["Created Time"],
            "Received Time as supplied",
            source.raw["Received Time"],
            "Retained export · Row 2",
            batch.parser_version,
            zone,
            "receipt precedes the observation",
        ):
            self.assertContains(response, phrase)
        DocumentEvidence.objects.filter(pk=document.pk).update(sha256="1" * 64)
        response = self.client.get(self.url("chronology"))
        self.assertNotContains(response, source.raw["Created Time"])
        self.assertNotContains(response, "Retained export · Row 2")
