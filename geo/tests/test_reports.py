"""Deterministic vehicle-location coverage, timing, and telemetry reports."""

from datetime import UTC, datetime, timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from core.analysis.reports import report
from core.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence
from geo.models import VehicleLocationEvidence, VehicleLocationImport

INSTANT = datetime(2026, 10, 27, 12, tzinfo=UTC)


class VehicleReportTests(EvidenceViewTestMixin, TestCase):
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
        self.assertIn("not inactivity", " ".join(result["limitations"]))

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
