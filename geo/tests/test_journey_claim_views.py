from datetime import UTC, datetime
from urllib.parse import urlencode

from django.test import TestCase
from django.urls import reverse
from django.utils.html import escape

from core.analysis.errors import AnalysisError
from core.analysis.services import process_run, start_run
from core.models import AnalysisRun
from core.testing import EvidenceViewTestMixin
from geo.models import PersonLocationEvidence
from people.models import Person
from text.models import TextEvidence


class JourneyClaimViewTests(EvidenceViewTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.login("editor")
        self.create_url = reverse("core:analysis:create", args=(self.case.pk,))
        self.source = TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["editor"],
            title="Calendar extract",
            body="🐝 Meet at 11:00. First plan.\r\nMeet at 11:00. Second plan.",
        )
        self.observation = PersonLocationEvidence.objects.create(
            case=self.case,
            person=self.person,
            uploaded_by=self.users["owner"],
            title="Midday report",
            observed_at=datetime(2026, 11, 21, 11, tzinfo=UTC),
            address="10 Main Street, Hamilton",
        )
        self.picked = {
            "claim": f"text:{self.source.pk}",
            "movement": f"person-location:{self.observation.pk}",
        }

    def post(self, **values):
        return self.client.post(
            self.create_url, {"operation": "journey_claims", **values}
        )

    def prepare(self):
        response = self.post(action="prepare", **self.picked)
        self.assertContains(response, "Interpret a passage")
        return response.context["draft"]

    def interpret(self, draft, **overrides):
        return self.post(
            **{
                "action": "review",
                "draft": draft,
                "quote": "Meet at 11:00.",
                "status": "proposed",
                "subject": f"person:{self.person.pk}",
                "address": "10 Main Street, Hamilton",
                "place_resolved": "on",
                "time_basis": "explicit",
                "time_start": "2026-11-21 11:00+00:00",
                **overrides,
            }
        )

    def test_confirmed_exact_unicode_occurrence_and_idempotent_submission(self):
        review = self.interpret(self.prepare())
        self.assertContains(review, "Choose the occurrence in its source context.")
        self.assertContains(review, "Occurrence 2")
        at = self.source.body.rindex("Meet at 11:00.")
        reviewed = self.interpret(review.context["draft"], occurrence=str(at))
        self.assertContains(reviewed, "Queue comparison")
        self.assertFalse(
            AnalysisRun.objects.filter(operation="journey_claims").exists()
        )
        queued = self.post(action="queue", draft=reviewed.context["draft"])
        repeated = self.post(action="queue", draft=reviewed.context["draft"])
        self.assertEqual(queued.status_code, 302)
        self.assertEqual(queued.url, repeated.url)
        run = AnalysisRun.objects.get(operation="journey_claims")
        self.assertFalse(run.external_processing)
        self.assertEqual(run.parameters["claims"][0]["start"], at)
        process_run(run.pk)
        self.assertEqual(run.result.data["items"][0]["label"], "Supporting observation")

    def test_reader_origin_survives_restart_validation_and_result_navigation(self):
        filters = {
            "q": "Calendar & appointments",
            "type": "text",
            "state": "all",
            "page": 2,
        }
        chooser = {
            "source": self.picked["claim"],
            "return_collection": urlencode(filters),
        }
        method_url = reverse(
            "core:analysis:create",
            args=(self.case.pk,),
            query=chooser | {"operation": "journey_claims"},
        )
        reader_url = reverse(
            "core:evidence:detail",
            args=(self.case.pk, "text", self.source.pk),
            query=filters,
            fragment="evidence-reader",
        )
        initial = self.client.get(method_url)
        self.assertContains(initial, f'href="{escape(reader_url)}">Cancel</a>')
        self.assertEqual(initial.context["pickers"][0]["query"], "")
        prepared = self.client.post(
            method_url,
            {
                "operation": "journey_claims",
                "action": "prepare",
                "claim_query": "Calendar extract",
                "movement_query": "Midday report",
                **self.picked,
            },
        )
        initial_draft = prepared.context["draft"]
        invalid = self.interpret(initial_draft)
        self.assertContains(invalid, "Choose the occurrence in its source context.")
        self.assertContains(invalid, f'href="{escape(reader_url)}">Cancel</a>')
        restarted = self.post(action="choose_again", draft=invalid.context["draft"])
        claim_picker, movement_picker = restarted.context["pickers"]
        self.assertEqual(claim_picker["query"], "Calendar extract")
        self.assertEqual(movement_picker["query"], "Midday report")
        self.assertEqual(restarted.context["picker_url"], method_url)
        for value in self.picked.values():
            self.assertContains(restarted, f'value="{value}" checked')

        reviewed = self.interpret(initial_draft, quote="First plan.")
        self.assertContains(reviewed, f'href="{escape(reader_url)}">Cancel</a>')
        queued = self.post(action="queue", draft=reviewed.context["draft"])
        repeated = self.post(action="queue", draft=reviewed.context["draft"])
        self.assertEqual(queued.status_code, 302)
        self.assertEqual(queued.url, repeated.url)
        run = AnalysisRun.objects.get(operation="journey_claims")
        process_run(run.pk)
        result = self.client.get(queued.url)
        self.assertEqual(result.context["back_to_analysis"], reader_url)
        passage_url = next(
            url
            for label, url in result.context["items"][0]["comparison_links"]
            if label.startswith("Read passage")
        )
        passage = self.client.get(passage_url)
        self.assertContains(passage, "<mark>First plan.</mark>")
        returned = self.client.get(passage.context["back_url"])
        self.assertEqual(returned.context["back_to_analysis"], reader_url)

    def test_draft_without_saved_navigation_keeps_working(self):
        from django.core import signing

        salt = f"journey-claims.{self.case.pk}.{self.users['editor'].pk}"
        old_state = signing.loads(self.prepare(), salt=salt)
        for key in ("chooser_query", "result_query", "picker_state"):
            del old_state[key]
        draft = signing.dumps(old_state, salt=salt, compress=True)
        reviewed = self.interpret(draft, quote="First plan.")
        self.assertEqual(
            reviewed.context["cancel_url"],
            reverse("core:analysis:index", args=(self.case.pk,)),
        )
        queued = self.post(action="queue", draft=reviewed.context["draft"])
        run = AnalysisRun.objects.get(operation="journey_claims")
        self.assertRedirects(
            queued, reverse("core:analysis:detail", args=(self.case.pk, run.pk))
        )

    def test_missing_reader_origin_returns_to_filtered_evidence(self):
        origin = self.text()
        filters = {"q": "Older statement", "type": "text", "state": "all"}
        method_url = reverse(
            "core:analysis:create",
            args=(self.case.pk,),
            query={
                "operation": "journey_claims",
                "source": f"text:{origin.pk}",
                "return_collection": urlencode(filters),
            },
        )
        prepared = self.client.post(
            method_url,
            {"operation": "journey_claims", "action": "prepare", **self.picked},
        )
        origin.delete()
        review = self.interpret(prepared.context["draft"], quote="First plan.")
        self.assertContains(review, "Queue comparison")
        collection = reverse("core:evidence:list", args=(self.case.pk,), query=filters)
        self.assertEqual(review.context["cancel_url"], collection)
        restarted = self.post(action="choose_again", draft=review.context["draft"])
        self.assertEqual(restarted.context["cancel_url"], collection)
        prepared_again = self.client.post(
            restarted.context["picker_url"],
            {"operation": "journey_claims", "action": "prepare", **self.picked},
        )
        self.assertEqual(prepared_again.context["cancel_url"], collection)
        queued = self.post(action="queue", draft=review.context["draft"])
        self.assertEqual(
            self.client.get(queued.url).context["back_to_analysis"], collection
        )

    def test_no_js_retains_earlier_interpretation_and_links_cancellation(self):
        first = self.interpret(self.prepare(), quote="First plan.")
        added = self.post(action="add", draft=first.context["draft"])
        second = self.interpret(
            added.context["draft"],
            quote="Second plan.",
            status="cancelled",
            applies_to="0",
        )
        self.assertContains(second, "Applies to")
        self.post(action="queue", draft=second.context["draft"])
        run = AnalysisRun.objects.get(operation="journey_claims")
        proposal, cancellation = run.parameters["claims"]
        self.assertEqual(cancellation["applies_to"]["start"], proposal["start"])

    def test_cannot_queue_unreviewed_or_cross_case_inputs_or_forged_subject(self):
        draft = self.prepare()
        denied = self.post(action="queue", draft=draft)
        self.assertContains(denied, "Review its exact sources")
        forged = self.interpret(
            draft,
            quote="First plan.",
            subject="person:00000000-0000-0000-0000-000000000000",
        )
        self.assertContains(forged, "valid choice")
        foreign = TextEvidence.objects.create(
            case=self.other_case,
            uploaded_by=self.users["outsider"],
            title="Secret title",
            body="Secret wording",
        )
        missing = self.post(
            action="prepare", **(self.picked | {"claim": f"text:{foreign.pk}"})
        )
        self.assertNotContains(missing, "Secret title")
        self.assertNotContains(missing, "Secret wording")
        self.assertFalse(
            AnalysisRun.objects.filter(operation="journey_claims").exists()
        )
        self.login("viewer")
        self.assertIn(
            self.post(action="prepare", **self.picked).status_code, (403, 404)
        )

    def test_paging_preserves_checked_sources(self):
        for n in range(26):
            TextEvidence.objects.create(
                case=self.case,
                uploaded_by=self.users["editor"],
                title=f"New passage {n:02}",
                body="New wording",
            )
        first = self.client.get(self.create_url, {"operation": "journey_claims"})
        p = first.context["pickers"][0]
        selected = p["page"][0][0]
        second = self.post(
            browse="claim-next", claim_after=p["page"].after, claim=selected
        )
        self.assertContains(second, f'value="{selected}" checked')
        self.assertContains(second, self.source.title)
        picker = second.context["pickers"][0]
        prepared = self.post(
            action="prepare",
            claim=selected,
            movement=self.picked["movement"],
            claim_cursor=picker["cursor"],
        )
        restarted = self.post(action="choose_again", draft=prepared.context["draft"])
        self.assertEqual(restarted.context["pickers"][0]["cursor"], picker["cursor"])
        self.assertContains(restarted, f'value="{selected}" checked')

    def test_restart_recovers_a_removed_selection_and_its_stale_cursor(self):
        replacement = TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["editor"],
            title="Calendar replacement",
            body="Another plan.",
        )
        prepared = self.post(
            action="prepare",
            claim_cursor=self.picked["claim"],
            claim_query="Calendar",
            movement_query="Midday",
            **self.picked,
        )
        self.assertContains(prepared, "Interpret a passage")
        reviewed = self.interpret(prepared.context["draft"], quote="First plan.")
        self.source.delete()
        blocked = self.post(action="queue", draft=reviewed.context["draft"])
        self.assertContains(blocked, "Restart source selection")
        self.assertFalse(
            AnalysisRun.objects.filter(operation="journey_claims").exists()
        )

        restarted = self.post(
            action="choose_again", draft=blocked.context["restart_draft"]
        )
        self.assertContains(
            restarted, "Unavailable sources were removed from the selection."
        )
        self.assertContains(restarted, "Its search restarted from the first page.")
        claim, movement = restarted.context["pickers"]
        self.assertEqual((claim["query"], claim["cursor"]), ("Calendar", ""))
        self.assertEqual(movement["query"], "Midday")
        self.assertNotContains(restarted, f'value="{self.picked["claim"]}"')
        self.assertContains(restarted, f'value="{self.picked["movement"]}" checked')
        self.assertContains(restarted, replacement.title)

        prepared_again = self.post(
            action="prepare",
            claim=f"text:{replacement.pk}",
            movement=self.picked["movement"],
            claim_cursor=self.picked["claim"],
            claim_query="Calendar",
        )
        self.assertContains(prepared_again, "Interpret a passage")
        restarted_again = self.post(
            action="choose_again", draft=prepared_again.context["draft"]
        )
        claim = restarted_again.context["pickers"][0]
        self.assertEqual((claim["query"], claim["cursor"]), ("Calendar", ""))

    def test_form_newlines_map_to_original_offsets(self):
        quoted = "First plan.\nMeet at 11:00."
        reviewed = self.interpret(self.prepare(), quote=quoted)
        self.assertContains(reviewed, "Queue comparison")
        self.post(action="queue", draft=reviewed.context["draft"])
        bounds = AnalysisRun.objects.get(operation="journey_claims").parameters[
            "claims"
        ][0]
        self.assertEqual(
            self.source.body[bounds["start"] : bounds["end"]],
            quoted.replace("\n", "\r\n"),
        )

    def test_second_selected_source_can_anchor_relative_time_or_supply_an_alternative(
        self,
    ):
        reference = TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Dated correspondence",
            body="Written on 20 November 2026 in UTC.",
        )
        selection = self.picked | {
            "claim": [self.picked["claim"], f"text:{reference.pk}"]
        }
        prepared = self.post(action="prepare", **selection)
        unfinished = self.interpret(prepared.context["draft"], quote="First plan.")
        self.assertContains(unfinished, "Choose a passage or time reference")
        self.assertContains(
            self.post(action="queue", draft=unfinished.context["draft"]),
            "Review its exact sources",
        )
        from core.analysis.sources import source_id

        referenced = self.interpret(
            prepared.context["draft"],
            quote="First plan.",
            time_basis="anchored_relative",
            reference_source=source_id({"kind": "text", "id": reference.pk}),
            reference_quote="20 November 2026 in UTC",
        )
        self.assertContains(referenced, "Time reference")
        queued = self.post(action="queue", draft=referenced.context["draft"])
        self.assertEqual(queued.status_code, 302)
        removed = self.post(remove="0", draft=referenced.context["draft"])
        self.assertContains(removed, "Interpret a passage")
        self.assertNotContains(removed, "Queue comparison")

    def test_exact_selected_suggestion_supplies_quote_and_type(self):
        from core.analysis.catalogue import project_result
        from core.analysis.services import snapshot_sources
        from core.models import AnalysisResult

        inputs = snapshot_sources(
            self.case, [{"kind": "text", "id": str(self.source.pk)}]
        )
        key = inputs[0]["id"]
        start = self.source.body.index("Second plan.")
        prior = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.users["owner"],
            operation="content_events",
            method_version="1",
            status="complete",
            inputs=inputs,
        )
        result = AnalysisResult.objects.create(
            case=self.case,
            run=prior,
            data={
                "items": [
                    {
                        "label": "plan",
                        "text": "A second plan was proposed.",
                        "source_ids": [key],
                        "anchors": [
                            {
                                "source_id": key,
                                "start": start,
                                "end": len(self.source.body),
                            }
                        ],
                        "values": {"status": "proposed"},
                    }
                ]
            },
        )
        project_result(result)
        choice = f"finding:{result.pk}:0"
        prepared = self.post(action="prepare", **(self.picked | {"claim": choice}))
        self.assertContains(prepared, "AI suggestion")
        self.assertEqual(prepared.context["form"].initial["status"], "proposed")
        self.assertEqual(prepared.context["form"].initial["quote"], "Second plan.")
        reviewed = self.interpret(
            prepared.context["draft"], quote="Untrusted replacement"
        )
        queued = self.post(action="queue", draft=reviewed.context["draft"])
        self.assertEqual(queued.status_code, 302)
        run = AnalysisRun.objects.get(operation="journey_claims")
        self.assertEqual(
            run.parameters["finding_refs"],
            [{"result_id": str(result.pk), "item_index": 0}],
        )
        self.assertEqual(run.parameters["claims"][0]["start"], start)

    def test_input_budget_applies_before_loading_the_selected_sources(self):
        from unittest.mock import patch

        from django.core.exceptions import ValidationError

        from core.analysis.services import snapshot_sources
        from core.models import AnalysisResult
        from geo.journey_claim_checks import check_material_size

        choice = {"kind": "text", "id": str(self.source.pk)}
        snapshots = snapshot_sources(self.case, [choice])
        with self.settings(PANDECT_ANALYSIS_MAX_BYTES=8):
            with self.assertRaisesMessage(ValidationError, "input limit"):
                check_material_size(self.case, snapshots)
            with patch(
                "geo.journey_claim_views.snapshot_and_resolve_sources"
            ) as snapshot:
                response = self.post(action="prepare", **self.picked)
                self.assertContains(response, "input limit")
                snapshot.assert_not_called()

        # Resolving an extraction materializes its creator's other inputs too.
        extra = TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Ancillary source",
            body="🐝" * 1000,
        )
        ancestors = snapshot_sources(
            self.case, [choice, {"kind": "text", "id": str(extra.pk)}]
        )
        prior = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.users["owner"],
            operation="native_text",
            method_version="1",
            status="complete",
            inputs=ancestors,
        )
        result = AnalysisResult.objects.create(
            case=self.case, run=prior, data={"items": []}
        )
        with self.settings(PANDECT_ANALYSIS_MAX_BYTES=2000):
            check_material_size(self.case, [choice])
            with self.assertRaisesMessage(ValidationError, "input limit"):
                check_material_size(self.case, [choice | {"result_id": str(result.pk)}])

    def test_empty_filtered_derived_page_can_reach_its_next_page(self):
        from unittest.mock import patch

        from django.core.paginator import Paginator

        page = Paginator(list(range(26)), 25).page(1)
        page.object_list = []
        with patch("geo.journey_claim_views.derived_page", return_value=page):
            screen = self.client.get(self.create_url, {"operation": "journey_claims"})
        self.assertContains(screen, "No readable saved items on this page.")
        self.assertContains(screen, 'value="claim-derived-next"')

    def test_picker_pages_and_submitted_tokens_exclude_plate_text(self):
        from unittest.mock import patch

        from django.core.files.uploadedfile import SimpleUploadedFile

        from core.analysis.form_base import source_choices
        from core.analysis.services import snapshot_sources
        from core.models import AnalysisResult, ExtractedPassage
        from image.models import ImageEvidence

        source = ImageEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["editor"],
            title="Scan of a plan",
            file=SimpleUploadedFile("scan.png", b"Synthetic scan"),
        )
        choice = {"kind": "image", "id": str(source.pk)}
        inputs = snapshot_sources(self.case, [choice])
        tokens = {}
        for operation in ("ocr", "plate_ocr"):
            run = AnalysisRun.objects.create(
                case=self.case,
                requested_by=self.users["editor"],
                operation=operation,
                status="complete",
                method_version="1",
                inputs=inputs,
            )
            result = AnalysisResult.objects.create(
                case=self.case,
                run=run,
                data={
                    "items": [
                        {
                            "source_ids": [inputs[0]["id"]],
                            "text": "At noon",
                            "anchors": [{"source_id": inputs[0]["id"]}],
                        }
                    ]
                },
            )
            ExtractedPassage.objects.create(
                case=self.case,
                result=result,
                kind="image",
                evidence_id=source.pk,
                evidence_sha256=source.sha256,
                item_index=0,
                text="At noon",
            )
            tokens[operation] = f"image:{source.pk}:-:{result.pk}"
        self.assertIn(
            tokens["plate_ocr"], dict(source_choices(self.case, "content_events"))
        )
        with patch("core.analysis.form_base.SOURCE_PAGE_SIZE", 1):
            first = self.post(browse="claim-search", claim_query=source.title)
            page = first.context["pickers"][0]["page"]
            self.assertTrue(page.has_next())
            second = self.post(
                browse="claim-next", claim_after=page.after, claim_query=source.title
            )
            self.assertContains(second, f'value="{tokens["ocr"]}"')
            self.assertNotContains(second, tokens["plate_ocr"])
            self.assertFalse(second.context["pickers"][0]["page"].has_next())
        forged = self.post(
            action="prepare", **(self.picked | {"claim": tokens["plate_ocr"]})
        )
        self.assertContains(forged, "Choose its exact retained material")
        self.assertNotContains(forged, "Interpret a passage")
        accepted = self.post(
            action="prepare", **(self.picked | {"claim": tokens["ocr"]})
        )
        self.assertContains(accepted, "Interpret a passage")

    def test_nullable_material_field_does_not_mask_large_message_body(self):
        from django.core.exceptions import ValidationError

        from geo.journey_claim_checks import _payload_bytes, check_material_size
        from phone.models import TextMessageEvidence

        body = "🔎" * 300
        message = TextMessageEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Message without a time",
            sender=self.person,
            recipient=self.person,
            body=body,
        )
        self.assertIsNone(message.occurred_at)
        rows = TextMessageEvidence.objects.filter(pk=message.pk, case=self.case)
        self.assertEqual(
            _payload_bytes(rows, "body", "occurred_at"), len(body.encode())
        )
        with self.settings(PANDECT_ANALYSIS_MAX_BYTES=1000):
            with self.assertRaisesMessage(ValidationError, "input limit"):
                check_material_size(
                    self.case, [{"kind": "text-message", "id": message.pk}]
                )

    def test_joint_participants_survive_review_and_each_requires_current_case(self):
        group = [f"person:{self.person.pk}", f"vehicle:{self.vehicle.pk}"]
        reviewed = self.interpret(self.prepare(), quote="First plan.", subject=group)
        self.assertContains(reviewed, str(self.person))
        self.assertContains(reviewed, str(self.vehicle))
        self.assertEqual(
            self.post(action="queue", draft=reviewed.context["draft"]).status_code, 302
        )
        run = AnalysisRun.objects.get(operation="journey_claims")
        (claim,) = run.parameters["claims"]
        self.assertEqual(set(claim["subjects"]), set(group))
        process_run(run.pk)
        (item,) = run.result.data["items"]
        self.assertEqual(item["label"], "Supporting observation")
        self.assertEqual(item["values"]["Observation subject"], group[0])
        foreign = Person.objects.create(case=self.other_case, first_name="Other case")
        with self.assertRaises(AnalysisError):
            start_run(
                self.users["editor"],
                self.case.pk,
                "journey_claims",
                [
                    {"kind": "text", "id": str(self.source.pk)},
                    {"kind": "person-location", "id": str(self.observation.pk)},
                ],
                parameters=run.parameters
                | {"claims": [claim | {"subjects": [*group, f"person:{foreign.pk}"]}]},
            )
