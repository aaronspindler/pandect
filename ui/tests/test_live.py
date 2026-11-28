"""Live fragments retain the page's authorization and full-page fallback."""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from cases.models import CaseMembership
from core.analysis.services import start_run
from core.analysis.worker import process_run
from core.models import AnalysisRun
from core.tests.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence
from geo.models import VehicleLocationImport
from text.models import TextEvidence


class LiveFragmentTests(EvidenceViewTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.source = TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic source",
            body="Contact source@example.test about the proposed meeting.",
        )
        self.run, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "entities",
            [{"kind": "text", "id": str(self.source.pk)}],
        )
        document = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic CSV",
            file=SimpleUploadedFile("source.csv", b"Synthetic CSV\n", "text/csv"),
        )
        self.batch = VehicleLocationImport.objects.create(
            case=self.case,
            document=document,
            uploaded_by=self.users["owner"],
            timezone="UTC",
            status="queued",
        )
        self.preview, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "imessage_import",
            [{"kind": "document", "id": str(document.pk)}],
            parameters={"phase": "preview", "timezone": "UTC"},
        )
        self.idle_trip_urls = (
            reverse("geo:case_map", args=[self.case.pk]),
            reverse("vehicle:detail", args=[self.case.pk, self.vehicle.pk]),
        )
        self.urls = (
            reverse("core:analysis:processing", args=[self.case.pk]),
            reverse("core:analysis:detail", args=[self.case.pk, self.run.pk]),
            self.url("detail", self.source),
            reverse("geo:vehicle_import_review", args=[self.case.pk, self.batch.pk]),
            reverse("phone:imessage_review", args=[self.case.pk, self.preview.pk]),
            *self.idle_trip_urls,
        )

    def test_roles_receive_only_live_sections_or_the_complete_page(self):
        for role in ("owner", "editor", "viewer"):
            self.login(role)
            for url in self.urls:
                with self.subTest(role=role, url=url):
                    fragment = self.client.get(url, HTTP_X_PANDECT_LIVE="1")
                    self.assertContains(fragment, "data-live-region")
                    self.assertNotContains(fragment, "<!doctype html>")
                    self.assertNotContains(fragment, 'id="evidence-original-heading"')
                    self.assertEqual(fragment["X-Pandect-Live"], "1")
                    self.assertIn("X-Pandect-Live", fragment["Vary"])
                    self.assertIn("no-store", fragment["Cache-Control"])
                    page = self.client.get(url)
                    self.assertContains(page, "<!doctype html>")
                    if url in self.idle_trip_urls:
                        self.assertNotContains(page, "Refresh status")
                    else:
                        self.assertContains(page, "Refresh status")
                    self.assertNotIn("X-Pandect-Live", page)

    def test_every_refresh_rechecks_current_access_and_case_relationships(self):
        for role in ("outsider", "viewer", "editor"):
            if role != "outsider":
                CaseMembership.objects.filter(
                    case=self.case, user=self.users[role]
                ).delete()
            self.login(role)
            for url in self.urls:
                with self.subTest(role=role, url=url):
                    response = self.client.get(url, HTTP_X_PANDECT_LIVE="1")
                    self.assertEqual(response.status_code, 404)
                    self.assertNotContains(
                        response, "data-live-region", status_code=404
                    )
        self.login("owner")
        for url in self.urls:
            with self.subTest(foreign=url):
                response = self.client.get(
                    url.replace(str(self.case.pk), str(self.other_case.pk)),
                    HTTP_X_PANDECT_LIVE="1",
                )
                self.assertEqual(response.status_code, 404)
        self.client.logout()
        for url in self.urls:
            self.assertEqual(
                self.client.get(url, HTTP_X_PANDECT_LIVE="1").status_code, 302
            )

    def test_completion_and_failure_publish_the_new_state_without_queuing_work(self):
        self.login("viewer")
        url = self.urls[1]
        queued = self.client.get(url, HTTP_X_PANDECT_LIVE="1")
        self.assertContains(queued, 'data-live-poll="true"')
        process_run(self.run.pk)
        complete = self.client.get(url, HTTP_X_PANDECT_LIVE="1")
        self.assertContains(complete, 'data-live-poll="false"')
        self.assertContains(complete, "Complete")
        self.assertNotContains(self.client.get(url), "Refresh status")
        AnalysisRun.objects.filter(pk=self.run.pk).update(
            status="failed", error_code="source_unavailable"
        )
        failed = self.client.get(url, HTTP_X_PANDECT_LIVE="1")
        self.assertContains(failed, 'data-live-poll="false"')
        self.assertContains(failed, "Processing could not finish")
        self.assertEqual(AnalysisRun.objects.count(), 2)

    def test_old_running_checks_show_indeterminate_progress_across_case_pages(self):
        self.login("viewer")
        AnalysisRun.objects.filter(pk__in=(self.run.pk, self.preview.pk)).update(
            status="running", progress=10, started_at=self.run.created_at
        )
        for url in (*self.urls[:3], self.urls[4]):
            with self.subTest(url=url):
                response = self.client.get(url, HTTP_X_PANDECT_LIVE="1")
                self.assertContains(response, "Running analysis")
                self.assertContains(response, "Started <time")
                self.assertNotContains(response, "10%")
                if url in (self.urls[1], self.urls[4]):
                    self.assertContains(
                        response,
                        '<progress class="work-progress" '
                        'aria-label="Running analysis"></progress>',
                        html=True,
                    )

    def test_processing_shows_measured_import_rows_before_the_total_is_known(self):
        self.login("viewer")
        self.batch.status = "running"
        self.batch.processed_rows = 12
        self.batch.save(update_fields=["status", "processed_rows"])
        self.assertContains(
            self.client.get(self.urls[0], HTTP_X_PANDECT_LIVE="1"), "12 rows processed"
        )
