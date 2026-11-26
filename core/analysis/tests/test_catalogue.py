"""Complete findings remain scoped, versioned, bounded, and source-backed."""

import importlib
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.apps import apps
from django.db import connection
from django.db.models.query import QuerySet
from django.http import QueryDict
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from cases.models import CaseMembership
from core.analysis.catalogue import CatalogueForm, catalogue_page, project_result
from core.analysis.operations import get_operation
from core.analysis.projection import equivalence_key, identifier_key
from core.analysis.services import start_run
from core.analysis.source_selection import snapshot_sources
from core.analysis.worker import process_run
from core.models import (
    AnalysisFinding,
    AnalysisFindingSource,
    AnalysisResult,
    AnalysisReview,
    AnalysisRun,
)
from core.testing import EvidenceViewTestMixin
from geo.models import LocationEvidence
from people.models import Person
from phone.models import TextMessageEvidence
from text.models import TextEvidence


class CatalogueTests(EvidenceViewTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.case.external_ai_enabled = True
        self.case.save(update_fields=["external_ai_enabled"])
        self.note = self.text()
        self.url = reverse("core:analysis:browse", kwargs={"case_id": self.case.pk})

    def publish(
        self,
        *,
        sources=None,
        items=None,
        operation="entities",
        parameters=None,
        status="complete",
        project=True,
    ):
        inputs = (
            sources
            if sources is not None
            else snapshot_sources(self.case, [{"kind": "text", "id": self.note.pk}])
        )
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.users["owner"],
            operation=operation,
            method_version=get_operation(operation).version,
            inputs=inputs,
            parameters=parameters or {},
            status=status,
        )
        if items is None:
            items = [
                {
                    "label": "An observation",
                    "text": "Literal needle 100% _ <script>",
                    "source_ids": [inputs[0]["id"]],
                    "values": {"detail": "Source value"},
                }
            ]
        result = AnalysisResult.objects.create(
            case=self.case, run=run, data={"items": items}
        )
        if project:
            project_result(result)
        return result

    def stub_finding_search(self, *results):
        from types import SimpleNamespace

        def matched(case, actor, query, **kwargs):
            passages = []
            for result in results:
                for finding in result.findings.all():
                    if query.casefold() in finding.search_text.casefold():
                        locator = {
                            "pk": str(finding.pk),
                            "surface": "finding",
                            "dependencies": [],
                        }
                        passages.append(
                            (
                                SimpleNamespace(locator=locator, url="/synthetic"),
                                0,
                                query,
                            )
                        )
            return passages

        self.enterContext(patch("core.case_search.hosted.hits", side_effect=matched))

    def page(self, **params):
        return catalogue_page(
            self.case, QueryDict(urlencode(params)), actor=self.users["owner"]
        )

    def test_method_choices_include_distinct_retained_methods_from_this_case(self):
        for case, operation in (
            (self.case, "retired_method"),
            (self.case, "retired_method"),
            (self.other_case, "foreign_retired_method"),
        ):
            AnalysisRun.objects.create(
                case=case,
                requested_by=case.owner,
                operation=operation,
                method_version="synthetic.v1",
            )
        form = CatalogueForm(QueryDict("method=retired_method"), case=self.case)
        self.assertTrue(form.is_valid(), form.errors)
        methods = [key for key, _ in form.fields["method"].choices]
        self.assertEqual(methods.count("retired_method"), 1)
        self.assertNotIn("foreign_retired_method", methods)
        self.assertIn("entities", methods)

    def test_findings_remain_readable_without_selection_actions(self):
        first = snapshot_sources(self.case, [{"kind": "text", "id": self.note.pk}])
        other = self.text()
        second = snapshot_sources(self.case, [{"kind": "text", "id": other.pk}])
        self.publish(
            sources=first + second,
            items=[
                {
                    "label": "Email",
                    "text": "person@example.test",
                    "values": {"raw": "person@example.test"},
                    "source_ids": [first[0]["id"], second[0]["id"]],
                }
            ],
        )
        group_url = reverse("core:analysis:connections", args=[self.case.pk])
        for role in ("owner", "editor", "viewer"):
            self.login(role)
            groups = self.client.get(group_url)
            occurrence_url = groups.context["groups"][0]["url"]
            for url in (self.url, occurrence_url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, "Save to selection")

    def test_reader_shows_only_current_findings_citing_this_record(self):
        other = self.text()
        inputs = snapshot_sources(
            self.case,
            [{"kind": "text", "id": self.note.pk}, {"kind": "text", "id": other.pk}],
        )
        first, second = (source["id"] for source in inputs)
        older = self.publish(
            sources=inputs, items=[{"label": "Earlier reading", "source_ids": [first]}]
        )
        self.publish(
            sources=inputs,
            items=[
                {"label": "Shared reading", "source_ids": [first, second]},
                {"label": "Neighbor only", "source_ids": [second]},
            ],
        )
        reader = reverse(
            "core:evidence:detail", args=[self.case.pk, "text", self.note.pk]
        )
        self.login("viewer")
        response = self.client.get(reader)
        self.assertContains(response, "Findings (1)")
        rows = list(response.context["findings_page"])
        self.assertEqual([row["label"] for row in rows], ["Shared reading"])
        self.assertEqual(
            [s["url"] for s in rows[0]["other_sources"]],
            [reverse("core:evidence:detail", args=[self.case.pk, "text", other.pk])],
        )
        self.assertNotContains(response, "Neighbor only")
        self.assertNotContains(response, "Earlier reading")
        old_run = next(
            run
            for run in response.context["analysis_page_obj"]
            if run.pk == older.run_id
        )
        history = self.client.get(old_run.history_url)
        self.assertContains(history, "Earlier reading")
        self.assertEqual(
            history.context["back_to_analysis"],
            reader + "?results_page=1#evidence-history-heading",
        )

    def test_all_runs_and_items_are_paginated_without_highlight_limits(self):
        for index in range(45):
            self.publish(parameters={"sample": index})
        first = self.page()
        self.assertEqual(first["page_obj"].paginator.count, 45)
        self.assertEqual(len(first["rows"]), 25)
        second = self.page(page=2)
        self.assertEqual(len(second["rows"]), 20)
        self.assertFalse(
            {row["id"] for row in first["rows"]} & {row["id"] for row in second["rows"]}
        )
        self.assertIn("page=2", first["next_url"])

    def test_return_to_findings_restores_case_query_and_ignores_other_targets(self):
        results = [self.publish(parameters={"sample": n}) for n in range(2)]
        self.stub_finding_search(*results)
        self.login("viewer")
        selection = {"q": "needle", "kind": "text", "history": "all", "page": "2"}
        with patch("core.analysis.catalogue.PER_PAGE", 1):
            catalogue = self.client.get(self.url, selection)
        row = catalogue.context["rows"][0]
        self.assertEqual(row["headline"], "Literal needle 100% _ <script>")
        detail = self.client.get(row["url"])
        self.assertEqual(detail.context["back_label"], "Back to findings")
        self.assertEqual(
            detail.context["back_to_analysis"], f"{self.url}?{urlencode(selection)}"
        )
        self.assertNotContains(detail, "Refresh status")
        self.assertContains(detail, "Analysis run details")
        run = AnalysisResult.objects.get(pk=row["result_id"]).run
        tampered = self.client.get(
            reverse("core:analysis:detail", args=[self.case.pk, run.pk]),
            {"return_findings": "next=https%3A%2F%2Fexample.test&q=needle"},
        )
        self.assertEqual(tampered.context["back_to_analysis"], f"{self.url}?q=needle")

    def test_empty_success_suppresses_older_items_but_failure_does_not(self):
        older = self.publish()
        failed = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.users["owner"],
            operation=older.run.operation,
            method_version=older.run.method_version,
            inputs=older.run.inputs,
            status="failed",
        )
        self.assertEqual(failed.equivalence_key, older.run.equivalence_key)
        self.assertEqual(self.page()["page_obj"].paginator.count, 1)
        self.publish(items=[], status="empty")
        self.assertEqual(self.page()["page_obj"].paginator.count, 0)
        self.assertEqual(self.page(history="all")["page_obj"].paginator.count, 1)

    def test_equivalence_preserves_settings_order_and_retained_versions(self):
        result = self.publish(
            parameters={"automatic_batch": "first", "automatic_followup": True}
        )
        run = result.run
        run.parameters = {"automatic_batch": "different"}
        self.assertEqual(equivalence_key(run), result.run.equivalence_key)
        run.parameters = {"_execution": {"model": "different"}}
        self.assertNotEqual(equivalence_key(run), result.run.equivalence_key)
        run.parameters = {}
        other = self.text()
        run.inputs += snapshot_sources(self.case, [{"kind": "text", "id": other.pk}])
        before = equivalence_key(run)
        run.inputs.reverse()
        self.assertNotEqual(before, equivalence_key(run))
        before = equivalence_key(run)
        run.inputs[0]["result_id"] = str(uuid4())
        self.assertNotEqual(before, equivalence_key(run))

    def test_latest_is_selected_before_search_filters(self):
        older = self.publish()
        self.stub_finding_search(older)
        self.publish(
            items=[
                {
                    "label": "Latest",
                    "text": "Different",
                    "source_ids": [older.run.inputs[0]["id"]],
                }
            ]
        )
        self.assertEqual(self.page(q="needle")["page_obj"].paginator.count, 0)
        self.assertEqual(
            self.page(q="needle", history="all")["page_obj"].paginator.count, 1
        )

    def test_retired_review_filter_does_not_affect_results(self):
        self.publish()
        page = self.page(review="accepted")
        self.assertEqual(page["page_obj"].paginator.count, 1)
        self.assertNotIn("review", page["form"].fields)
        self.assertNotIn("review", page["rows"][0])

    def test_literal_search_excludes_archived_sources_and_escapes_output(self):
        self.stub_finding_search(self.publish())
        self.login("viewer")
        response = self.client.get(self.url, {"q": "100% _ <script>"})
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, "<script>")
        self.assertEqual(
            self.page(q="detail: Source value")["page_obj"].paginator.count, 1
        )
        self.assertEqual(self.page(q="100XYZ")["page_obj"].paginator.count, 0)

        TextEvidence.objects.filter(pk=self.note.pk).update(
            is_archived=True,
            archive_changed_at=timezone.now(),
            archive_changed_by=self.users["owner"],
        )
        response = self.client.get(self.url, {"q": "100% _ <script>"})
        self.assertContains(response, "No matching findings")
        self.assertEqual(
            self.page(q="detail: Source value")["page_obj"].paginator.count, 0
        )

    def test_person_filter_uses_only_each_findings_citations(self):
        other = Person.objects.create(case=self.case, first_name="Other person")
        message = TextMessageEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Self message",
            body="Hello",
            sender=self.person,
            recipient=self.person,
        )
        unrelated = TextMessageEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Other message",
            body="No association",
            sender=other,
            recipient=other,
        )
        inputs = snapshot_sources(
            self.case,
            [
                {"kind": "text-message", "id": message.pk},
                {"kind": "text-message", "id": unrelated.pk},
            ],
        )
        self.publish(
            sources=inputs,
            items=[
                {"label": "Person finding", "source_ids": [inputs[0]["id"]]},
                {"label": "Other finding", "source_ids": [inputs[1]["id"]]},
            ],
        )
        page = self.page(person=self.person.pk)
        self.assertEqual([row["label"] for row in page["rows"]], ["Person finding"])
        self.assertEqual(
            self.page(person=self.person.pk, evidence=unrelated.pk)[
                "page_obj"
            ].paginator.count,
            0,
        )
        self.assertEqual(
            self.page(person=self.person.pk, kind="document")[
                "page_obj"
            ].paginator.count,
            0,
        )

    def test_changed_source_has_stable_unavailable_slot_without_content(self):
        self.publish()
        TextEvidence.objects.filter(pk=self.note.pk).update(
            body="Changed without checksum update"
        )
        page = self.page()
        self.assertEqual(page["page_obj"].paginator.count, 1)
        self.assertTrue(page["rows"][0]["unavailable"])
        self.assertEqual(page["rows"][0]["excerpt"], "")
        self.assertEqual(page["rows"][0]["sources"], [])

    def test_roles_revocation_and_cross_case_filter(self):
        self.publish()
        foreign = Person.objects.create(
            case=self.other_case, first_name="Private person"
        )
        for role in ("owner", "editor", "viewer"):
            self.login(role)
            self.assertEqual(self.client.get(self.url).status_code, 200)
        invalid = self.client.get(self.url, {"person": foreign.pk})
        self.assertEqual(invalid.context["page_obj"].paginator.count, 0)
        self.assertNotContains(invalid, "Private person")
        CaseMembership.objects.filter(
            case=self.case, user=self.users["viewer"]
        ).delete()
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.login("outsider")
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_projection_publication_failure_rolls_back_result(self):
        run, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "entities",
            [{"kind": "text", "id": self.note.pk}],
        )
        with patch(
            "core.analysis.catalogue.project_result",
            side_effect=RuntimeError("synthetic"),
        ):
            self.assertEqual(process_run(run.pk), "processing_failed")
        self.assertFalse(AnalysisResult.objects.filter(run=run).exists())
        self.assertFalse(AnalysisFinding.objects.exists())

    def test_backfill_uses_json_and_resumes_without_touching_results(self):
        result = self.publish(project=False)
        AnalysisRun.objects.filter(pk=result.run_id).update(equivalence_key="")
        migration = importlib.import_module(
            "core.migrations.0004_backfill_finding_catalogue"
        )
        schema_editor = SimpleNamespace(connection=connection)
        original = result.data
        with patch(
            "django.core.files.storage.FileSystemStorage.open",
            side_effect=AssertionError("Original must not be read"),
        ):
            migration.backfill(apps, schema_editor)
            migration.backfill(apps, schema_editor)
        self.assertEqual(AnalysisFinding.objects.count(), 1)
        self.assertEqual(AnalysisFindingSource.objects.count(), 1)
        result.refresh_from_db()
        self.assertEqual(result.data, original)
        self.assertEqual(result.run.equivalence_key, equivalence_key(result.run))

    def test_interrupted_backfill_rolls_back_partial_run_and_resumes(self):
        results = [self.publish(project=False), self.publish(project=False)]
        first, second = sorted(results, key=lambda result: result.run_id)
        AnalysisRun.objects.filter(pk__in=[result.run_id for result in results]).update(
            equivalence_key=""
        )
        migration = importlib.import_module(
            "core.migrations.0004_backfill_finding_catalogue"
        )
        schema_editor = SimpleNamespace(connection=connection)
        bulk_create = QuerySet.bulk_create

        def interrupted(query, objects, *args, **kwargs):
            if query.model is AnalysisFindingSource and any(
                item.finding.result_id == second.pk for item in objects
            ):
                raise RuntimeError("Synthetic interrupted backfill")
            return bulk_create(query, objects, *args, **kwargs)

        with patch.object(
            QuerySet, "bulk_create", autospec=True, side_effect=interrupted
        ):
            with self.assertRaisesMessage(RuntimeError, "interrupted backfill"):
                migration.backfill(apps, schema_editor)
        self.assertEqual(AnalysisFinding.objects.filter(result=first).count(), 1)
        self.assertFalse(AnalysisFinding.objects.filter(result=second).exists())
        self.assertTrue(AnalysisRun.objects.get(pk=first.run_id).equivalence_key)
        self.assertEqual(AnalysisRun.objects.get(pk=second.run_id).equivalence_key, "")
        migration.backfill(apps, schema_editor)
        self.assertEqual(AnalysisFinding.objects.count(), 2)
        self.assertEqual(AnalysisFindingSource.objects.count(), 2)

    def test_catalogue_defers_result_payload_and_original_body(self):
        self.publish()
        with CaptureQueriesContext(connection) as queries:
            page = self.page()
        self.assertFalse(page["rows"][0]["unavailable"])
        sql = "\n".join(query["sql"] for query in queries)
        self.assertNotIn('"core_analysisresult"."data"', sql)
        self.assertNotIn('"core_analysisresult"."provenance"', sql)
        self.assertNotIn(', "text_textevidence"."body",', sql)
        self.assertLess(len(queries), 15)

    def test_conservative_typed_identifier_keys(self):
        def key(label, raw):
            return identifier_key("entities", {"label": label, "values": {"raw": raw}})

        self.assertEqual(key("Email", "A@EXAMPLE.TEST"), ("email", "A@example.test"))
        self.assertNotEqual(
            key("Email", "A@example.test"), key("Email", "a@example.test")
        )
        self.assertNotEqual(
            key("Phone candidate", "+1 555 123 4567"),
            key("Phone candidate", "1 555 123 4567"),
        )
        self.assertEqual(
            key("URL", "https://EXAMPLE.TEST/Path?q=A"),
            ("url", "https://example.test/Path?q=A"),
        )
        self.assertNotEqual(
            key("URL", "https://example.test/"), key("Domain candidate", "example.test")
        )
        self.assertEqual(key("Date candidate", "2026-11-15"), ("", ""))
        self.assertEqual(
            identifier_key("plate_ocr", {"text": "ab 12-cd", "values": {}}),
            ("plate", "AB12CD"),
        )
        self.assertEqual(
            identifier_key(
                "plate_ocr", {"text": "ab 12-cd", "values": {"Weak plate region": True}}
            ),
            ("", ""),
        )

    def test_search_includes_complete_values_but_returns_bounded_excerpts(self):
        inputs = snapshot_sources(self.case, [{"kind": "text", "id": self.note.pk}])
        result = self.publish(
            items=[
                {
                    "label": "Long retained measurement",
                    "source_ids": [inputs[0]["id"]],
                    "values": {"detail": "prefix " * 2000 + "Unique tail value"},
                }
            ]
        )
        self.stub_finding_search(result)
        page = self.page(q="Unique tail value")
        self.assertEqual(page["page_obj"].paginator.count, 1)
        self.assertIn("Unique tail value", page["rows"][0]["excerpt"])
        self.assertLessEqual(len(page["rows"][0]["excerpt"]), 300)
        self.assertNotIn("Unique tail value", page["rows"][0]["preview"])
        self.assertLessEqual(len(page["rows"][0]["preview"]), 300)

    def test_historical_geocode_review_is_retained_with_changed_review_warning(self):
        location = LocationEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Reported address",
            address="Synthetic street",
        )
        original = snapshot_sources(
            self.case, [{"kind": "location", "id": location.pk}]
        )
        prior = self.publish(
            sources=original,
            operation="geocode_locations",
            items=[
                {
                    "label": "Location estimate",
                    "source_ids": [original[0]["id"]],
                    "values": {"latitude": 45, "longitude": -79},
                }
            ],
        )
        AnalysisReview.objects.create(
            case=self.case,
            result=prior,
            reviewed_by=self.users["owner"],
            decision="accepted",
            item_index=0,
        )
        selected = snapshot_sources(
            self.case,
            [
                {
                    "kind": "location",
                    "id": location.pk,
                    "result_id": prior.pk,
                    "geocode_candidate": 0,
                }
            ],
        )
        self.publish(sources=selected, operation="location_quality")
        AnalysisReview.objects.create(
            case=self.case,
            result=prior,
            reviewed_by=self.users["owner"],
            decision="rejected",
            item_index=0,
        )
        row = self.page(method="location_quality")["rows"][0]
        self.assertFalse(row["unavailable"])
        self.assertIn("is no longer selected", row["warning"])

    def test_corrupt_cross_case_source_is_withheld(self):
        result = self.publish()
        foreign = TextEvidence.objects.create(
            case=self.other_case,
            uploaded_by=self.users["outsider"],
            title="Private foreign record",
            body="Private foreign body",
        )
        inputs = result.run.inputs
        inputs[0] |= {"evidence_id": str(foreign.pk), "sha256": foreign.sha256}
        AnalysisRun.objects.filter(pk=result.run_id).update(inputs=inputs)
        row = self.page()["rows"][0]
        self.assertTrue(row["unavailable"])
        self.assertEqual(row["sources"], [])


def urlencode(params):
    query = QueryDict(mutable=True)
    query.update(params)
    return query.urlencode()
