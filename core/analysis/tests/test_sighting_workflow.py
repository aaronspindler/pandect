"""Sighting groups publish and review only exact, currently authorized inputs."""

from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase
from django.urls import reverse
from django.utils.html import escape

from cases.models import CaseMembership
from core.analysis.operations import get_operation
from core.analysis.services import (
    process_run,
    set_address_estimate,
    snapshot_sources,
    start_run,
)
from core.analysis.sightings import candidate_links
from core.evidence.deletion import plan_deletion
from core.models import AnalysisResult, AnalysisReview, AnalysisRun, ImportantItem
from core.testing import EvidenceViewTestMixin
from geo.models import LocationEvidence, PersonLocationEvidence


class SightingWorkflowTests(EvidenceViewTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.login("editor")

    def observation(self, title, *, latitude="43.65", associated=False, **kwargs):
        model = PersonLocationEvidence if associated else LocationEvidence
        values = {"person": self.person} if associated else {}
        return model.objects.create(
            case=kwargs.pop("case", self.case),
            uploaded_by=kwargs.pop("actor", self.users["owner"]),
            title=title,
            latitude=latitude,
            longitude="-79.38",
            **values,
            **kwargs,
        )

    def spatial_comparison(self, *records, estimate=None):
        choices = [{"kind": "location", "id": str(record.pk)} for record in records]
        if estimate:
            choices[0].update(result_id=str(estimate.pk), geocode_candidate=0)
        run, _ = start_run(
            self.users["editor"],
            self.case.pk,
            "spatial_groups",
            choices,
        )
        process_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "complete", run.error_code)
        return run

    def create_group(self, anchor, *, kind="location", **data):
        selected = f"{kind}:{anchor.pk}"
        links = candidate_links(self.case, selected)
        self.assertTrue(links)
        payload = {
            "submission_id": str(uuid4()),
            "anchor": selected,
            "links": [links[0]["token"]],
            **data,
        }
        url = reverse("core:analysis:sighting_create", args=[self.case.pk])
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, 302)
        run = AnalysisRun.objects.get(submission_id=payload["submission_id"])
        return run, payload

    def inspect_url(self, name, run):
        return reverse(f"core:analysis:{name}", args=[self.case.pk, run.pk])

    def review(self, run, decision="accepted", revision=""):
        return self.client.post(
            self.inspect_url("sighting_review", run),
            {"decision": decision, "revision": revision},
        )

    def test_duplicate_submit_and_review_revision_preserve_a_single_proposal(self):
        first = self.observation("First observation", associated=True)
        self.observation("Second observation", associated=True)
        contrary = self.observation("Contrary location")
        run, payload = self.create_group(
            first,
            kind="person-location",
            **{f"context_location:{contrary.pk}": "contrary"},
        )
        url = reverse("core:analysis:sighting_create", args=[self.case.pk])
        with patch("core.analysis.sighting_views.candidate_links") as discover:
            duplicate = self.client.post(url, payload)
        discover.assert_not_called()
        self.assertEqual(duplicate.status_code, 302)
        self.assertEqual(
            AnalysisRun.objects.filter(operation="sighting_group").count(), 1
        )
        before = plan_deletion(first, "person-location")
        with (
            patch(
                "core.case_search.indexing.profile", return_value={"available": True}
            ),
            patch("core.case_search.indexing.add_sources") as add_sources,
        ):
            process_run(run.pk)
        add_sources.assert_not_called()
        result = AnalysisResult.objects.get(run=run)
        self.assertFalse(result.provenance["external_processing"])
        self.assertEqual(result.provenance["source_inputs"], run.inputs)
        self.assertEqual(result.findings.count(), 1)
        self.assertEqual(plan_deletion(first, "person-location").analysis_count, 1)
        self.assertNotEqual(
            plan_deletion(first, "person-location").fingerprint, before.fingerprint
        )
        self.assertContains(
            self.client.get(self.inspect_url("detail", run), HTTP_X_PANDECT_LIVE="1"),
            "Open sighting group",
        )

        self.assertEqual(self.review(run).status_code, 302)
        latest = result.reviews.get()
        self.assertEqual(latest.decision, "accepted")
        self.assertIn(self.review(run, "rejected").status_code, (302, 409))
        self.assertEqual(result.reviews.count(), 1)
        self.assertEqual(self.review(run, "rejected", str(latest.pk)).status_code, 302)
        self.assertEqual(
            list(result.reviews.values_list("decision", flat=True)),
            ["rejected", "accepted"],
        )
        response = self.client.post(self.inspect_url("retry", run))
        self.assertEqual(response.status_code, 302)
        successor = AnalysisRun.objects.get(previous_run=run)
        self.assertEqual(successor.inputs, run.inputs)
        process_run(successor.pk)
        self.assertFalse(successor.result.reviews.exists())
        self.assertEqual(
            self.review(run, "unresolved", str(result.reviews.first().pk)).status_code,
            409,
        )

    def test_revocation_and_archival_block_publication(self):
        first = self.observation("First observation", associated=True)
        second = self.observation("Second observation", associated=True)
        run, _ = self.create_group(first, kind="person-location")
        self.client.post(self.url("archive", second, "person-location"))
        self.assertEqual(process_run(run.pk), "source_archived")
        self.assertFalse(AnalysisResult.objects.filter(run=run).exists())
        self.client.post(self.url("restore", second, "person-location"))
        run, _ = self.create_group(first, kind="person-location")
        CaseMembership.objects.filter(
            case=self.case, user=self.users["editor"]
        ).delete()
        self.assertEqual(process_run(run.pk), "access_removed")
        self.assertFalse(AnalysisResult.objects.filter(run=run).exists())

    def test_important_reopens_the_completed_sighting_group_with_return_context(self):
        first = self.observation("First observation", associated=True)
        second = self.observation("Second observation", associated=True)
        run, _ = self.create_group(first, kind="person-location")
        process_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, AnalysisRun.Status.COMPLETE, run.error_code)
        detail = self.inspect_url("sighting_detail", run)
        self.assertContains(self.client.get(detail), "Mark important")

        mark = reverse("core:important:mark_finding", args=[self.case.pk, run.pk, 0])
        self.assertRedirects(self.client.post(mark, {"action": "mark"}), detail)
        saved = ImportantItem.objects.get(finding_result__run=run, item_index=0)
        source = reverse(
            "core:analysis:sighting_detail",
            args=[self.case.pk, run.pk],
            query={"important_page": 1, "important_item": saved.pk},
        )
        important = reverse("core:important:list", args=[self.case.pk])
        self.assertContains(self.client.get(important), f'href="{escape(source)}"')
        self.login("viewer")
        response = self.client.get(source)
        self.assertContains(response, second.title)
        self.assertContains(response, f'href="{important}?page=1#important-{saved.pk}"')
        self.assertNotContains(response, "Remove from Important")
        self.assertFalse(AnalysisReview.objects.filter(result__run=run).exists())

    def test_important_displays_the_latest_sighting_review_outcome(self):
        first = self.observation("First observation", associated=True)
        second = self.observation("Second observation", associated=True)
        run, _ = self.create_group(first, kind="person-location")
        process_run(run.pk)
        result = AnalysisResult.objects.get(run=run)
        mark = reverse("core:important:mark_finding", args=[self.case.pk, run.pk, 0])
        self.assertRedirects(
            self.client.post(mark, {"action": "mark"}),
            self.inspect_url("sighting_detail", run),
        )
        saved = ImportantItem.objects.get(finding_result=result, item_index=0)
        urls = (
            reverse("core:important:list", args=[self.case.pk]),
            reverse("core:important:notes", args=[self.case.pk, saved.pk]),
        )
        for url in urls:
            self.assertContains(self.client.get(url), "Saved proposal · Unresolved")

        revision = ""
        for decision, label in (
            ("accepted", "Accepted for review"),
            ("rejected", "Rejected"),
        ):
            with self.subTest(decision=decision):
                self.assertEqual(self.review(run, decision, revision).status_code, 302)
                revision = str(result.reviews.first().pk)
                for url in urls:
                    self.assertContains(
                        self.client.get(url), f"Saved proposal · {label}"
                    )

        self.assertEqual(
            self.client.post(
                self.url("archive", second, "person-location")
            ).status_code,
            302,
        )
        for url in urls:
            response = self.client.get(url)
            self.assertContains(response, "Historical proposal")
            self.assertContains(response, "Rejected")
            self.assertContains(response, "Archived source")

    def test_publication_rechecks_revocation_after_computing_the_group(self):
        first = self.observation("First observation", associated=True)
        self.observation("Second observation", associated=True)
        run, _ = self.create_group(first, kind="person-location")
        operation = get_operation(run.operation)

        def revoke_before_publication(sources, parameters):
            data = operation.function(sources, parameters)
            CaseMembership.objects.filter(
                case=self.case, user=self.users["editor"]
            ).delete()
            return data

        with patch(
            "core.analysis.services.get_operation",
            return_value=replace(operation, function=revoke_before_publication),
        ):
            self.assertEqual(process_run(run.pk), "access_removed")
        self.assertFalse(AnalysisResult.objects.filter(run=run).exists())

    def test_superseded_comparison_is_history_and_retry_requires_current_inputs(self):
        first = self.observation("Market entrance")
        second = self.observation("Unrelated nearby entrance", latitude="43.6502")
        producer = self.spatial_comparison(first, second)
        initial, _ = self.create_group(first)
        process_run(initial.pk)
        self.assertEqual(
            initial.parameters["finding_refs"],
            [{"result_id": str(producer.result.pk), "item_index": 0}],
        )
        queued, _ = self.create_group(first)
        replacement = self.spatial_comparison(first, second)
        self.assertNotEqual(producer.pk, replacement.pk)
        self.assertEqual(process_run(queued.pk), "sighting_comparison_superseded")
        self.assertFalse(AnalysisResult.objects.filter(run=queued).exists())
        saved = self.client.get(self.inspect_url("sighting_detail", initial))
        self.assertContains(saved, second.title)
        self.assertNotContains(saved, "Accept for review")
        self.review(initial)
        self.assertFalse(AnalysisReview.objects.filter(result__run=initial).exists())
        retried = self.client.post(self.inspect_url("retry", initial))
        self.assertEqual(retried.status_code, 302)
        self.assertFalse(AnalysisRun.objects.filter(previous_run=initial).exists())

    def test_revoked_and_reselected_estimate_cannot_replace_pinned_review(self):
        first = LocationEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Reported street address",
            address="Synthetic market hall",
        )
        second = self.observation("Recorded nearby position")
        inputs = snapshot_sources(
            self.case, [{"kind": "location", "id": str(first.pk)}]
        )
        geocode = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.users["editor"],
            operation="geocode_locations",
            method_version="2",
            inputs=inputs,
            status="complete",
        )
        estimate = AnalysisResult.objects.create(
            case=self.case,
            run=geocode,
            data={
                "items": [
                    {
                        "label": "Candidate address estimate",
                        "source_ids": [inputs[0]["id"]],
                        "values": {"latitude": "43.6501", "longitude": "-79.38"},
                    },
                ]
            },
        )
        actor = self.users["editor"]
        set_address_estimate(actor, self.case.pk, geocode.pk, 0, use=True)
        self.spatial_comparison(first, second, estimate=estimate)
        run, _ = self.create_group(first)
        process_run(run.pk)
        self.assertTrue(AnalysisResult.objects.filter(run=run).exists())
        set_address_estimate(actor, self.case.pk, geocode.pk, 0, use=False)
        self.assertEqual(self.review(run).status_code, 409)
        set_address_estimate(actor, self.case.pk, geocode.pk, 0, use=True)
        self.assertEqual(self.review(run).status_code, 409)
        self.assertFalse(candidate_links(self.case, f"location:{first.pk}"))
        detail = self.client.get(self.inspect_url("sighting_detail", run))
        self.assertContains(detail, "Reviewed address estimate")
        self.assertNotContains(detail, "Accept for review")

    def test_current_roles_and_invalid_sources_protect_every_result_route(self):
        first = self.observation("Private first entrance")
        second = self.observation("Private second entrance", latitude="43.6502")
        self.spatial_comparison(first, second)
        run, payload = self.create_group(first)
        process_run(run.pk)
        detail = self.inspect_url("sighting_detail", run)
        download = self.inspect_url("download", run)
        self.login("viewer")
        self.assertContains(self.client.get(detail), second.title)
        self.assertIn("no-store", self.client.get(download).headers["Cache-Control"])
        self.assertEqual(self.review(run).status_code, 403)
        self.login("outsider")
        for url in (detail, download):
            self.assertEqual(self.client.get(url).status_code, 404)

        self.login("editor")
        foreign = self.observation(
            "Other case entrance", case=self.other_case, actor=self.users["outsider"]
        )
        invalid = payload | {
            "submission_id": str(uuid4()),
            "anchor": f"location:{foreign.pk}",
        }
        create_url = reverse("core:analysis:sighting_create", args=[self.case.pk])
        denied = self.client.post(create_url, invalid)
        self.assertIn(denied.status_code, (400, 404))
        self.assertNotContains(denied, foreign.title, status_code=denied.status_code)
        self.assertFalse(
            AnalysisRun.objects.filter(submission_id=invalid["submission_id"]).exists()
        )

        LocationEvidence.objects.filter(pk=second.pk).update(sha256="a" * 64)
        withheld = self.client.get(detail)
        self.assertEqual(withheld.status_code, 200)
        self.assertNotContains(withheld, first.title)
        self.assertNotContains(withheld, second.title)
        self.assertEqual(self.client.get(download).status_code, 404)
        catalogue = self.client.get(
            reverse("core:analysis:browse", args=[self.case.pk]),
            {"method": "sighting_group"},
        )
        self.assertNotContains(catalogue, second.title)
