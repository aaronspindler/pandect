from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.analysis.bulk import prepare_case_analysis_batch
from core.analysis.source_selection import snapshot_sources
from core.analysis.tests.test_execution import LOCAL
from core.analysis.worker import process_run
from core.case_search.generation import enqueue_generation, set_policy, settings_rows
from core.case_search.generation_briefs import SECTION_CHARS
from core.case_search.indexing import evidence_archive_changed, result_published
from core.case_search.parent_index import index_parent
from core.case_search.parents import parent_results
from core.models import AnalysisResult, AnalysisRun, CaseAnalysisBatch, ExtractedPassage
from core.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence
from phone.grouping_jobs import grouping_scope, start_grouping
from phone.models import CommunicationContext, TextMessageEvidence
from text.models import TextEvidence


@override_settings(PANDECT_LOCAL_AI=LOCAL, PANDECT_ANALYSIS_MAX_SOURCES=2)
class SearchGenerationTests(EvidenceViewTestMixin, TestCase):
    def original(self, body):
        return TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.case.owner,
            title="Delivery notes",
            body=body,
        )

    def prepare(self, batch):
        for _ in range(20):
            if prepare_case_analysis_batch(batch.pk) != "queued":
                break
        batch.refresh_from_db()
        self.assertEqual(batch.status, "complete", batch.error_code)

    def publish(self, run, statement, quotes):
        citations = [
            {"source_id": source["id"], "quote": phrase, "start": start}
            for source, (phrase, start) in zip(run.inputs, quotes, strict=True)
        ]
        response = {
            "items": [{"kind": "Summary", "text": statement, "citations": citations}]
        }
        with patch("core.analysis.local.LocalClient.infer", return_value=response):
            process_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "complete", run.error_code)
        index_parent.call(
            str(self.case.pk), str(self.case.owner_id), "summary", str(run.result.pk)
        )
        return run.result.data["items"][0]["anchors"]

    def search(self, question):
        return parent_results(self.case, user=self.users["viewer"], query=question)

    def test_owner_reviews_backlog_enables_independently_and_rechecks_revocation(self):
        self.original(body="A recorded delivery. " * 50)
        self.login("owner")
        review = self.client.get(
            reverse("cases:case_settings", args=(self.case.pk,)), {"review_search": "1"}
        )
        option = next(
            o for o in review.context["search_generation"] if o["feature"] == "briefs"
        )
        self.assertEqual((option["estimate"], option["units"]), (1, 1))
        url = reverse("cases:search_generation", args=(self.case.pk,))
        form = {
            "feature": "briefs",
            "action": "enable",
            "confirmation": option["confirmation"],
        }
        self.login("editor")
        self.assertEqual(self.client.post(url, form).status_code, 403)
        self.login("owner")
        self.assertEqual(self.client.post(url, form).status_code, 302)
        self.client.post(url, form)
        self.case.refresh_from_db()
        self.assertEqual(set(self.case.search_generation_policy), {"briefs"})
        batch = CaseAnalysisBatch.objects.get(case=self.case, scope__feature="briefs")
        self.prepare(batch)
        run = batch.runs.get()
        set_policy(self.case, "briefs", False)
        with patch("core.analysis.local.LocalClient.infer") as infer:
            process_run(run.pk)
            infer.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.error_code, "execution_changed")

    def test_long_source_retains_exact_second_section_and_skips_completed_work(self):
        set_policy(self.case, "briefs", True)
        body = "x" * SECTION_CHARS + "The schedule was revised."
        item = self.original(body=body)
        batch = CaseAnalysisBatch.objects.get(scope__target="text", case=self.case)
        self.prepare(batch)
        first, second = batch.runs.order_by("parameters___brief_section__start")
        self.assertEqual(second.parameters["_brief_section"]["start"], SECTION_CHARS)
        item.title = "Reviewed delivery notes"
        item.save(update_fields=["title"])
        (anchor,) = self.publish(
            second, "The dispatch timetable changed.", [("schedule was revised", 4)]
        )
        (hit,) = self.search("timetable")
        self.assertEqual(
            (hit["via"], hit["source"], hit["start"], hit["excerpt"]),
            (
                "Document brief",
                ("text", item.pk),
                SECTION_CHARS + 4,
                "schedule was revised",
            ),
        )
        self.assertEqual(anchor["search_source_id"], str(item.pk))
        self.assertEqual(
            body[anchor["search_start"] : anchor["search_end"]], anchor["quote"]
        )

        item.title = "Final delivery notes"
        item.save(update_fields=["title"])
        replay, _ = enqueue_generation(self.case, "briefs")
        self.prepare(replay)
        self.assertEqual(replay.runs.count(), 0)
        self.assertEqual(
            AnalysisRun.objects.filter(
                case=self.case, parameters___search_generation="briefs"
            ).count(),
            2,
        )
        self.assertEqual(first.status, "queued")

    def test_automatic_grouping_uses_missing_ranges_and_checks_reapproval(self):
        set_policy(self.case, "conversations", True)
        for i, body in enumerate(
            ("Moved the delivery.", "I agree.", "The sign was blue.")
        ):
            TextMessageEvidence.objects.create(
                case=self.case,
                uploaded_by=self.users["owner"],
                sender=self.person,
                recipient=self.person,
                title=f"Delivery {i}",
                body=body,
                occurred_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=i),
            )
        _, digest = grouping_scope(self.case, self.person, self.person)
        preparation, _ = start_grouping(
            self.users["owner"],
            self.case,
            self.person,
            self.person,
            gap_minutes=60,
            reviewed_scope=digest,
            submission_id=uuid4(),
        )
        self.prepare(preparation)
        batch = CaseAnalysisBatch.objects.get(case=self.case, scope__target="grouping")
        self.prepare(batch)
        runs = list(batch.runs.order_by("created_at"))
        self.assertEqual(
            [
                (
                    r.parameters["_saved_session_range"]["first"],
                    r.parameters["_saved_session_range"]["last"],
                )
                for r in runs
            ],
            [(1, 2), (3, 3)],
        )
        old_revision = self.case.search_generation_policy["conversations"]["revision"]
        set_policy(self.case, "conversations", False)
        set_policy(self.case, "conversations", True)
        self.assertNotEqual(
            self.case.search_generation_policy["conversations"]["revision"],
            old_revision,
        )
        with patch("core.analysis.local.LocalClient.infer") as infer:
            process_run(runs[0].pk)
            infer.assert_not_called()
        nearest = CaseAnalysisBatch.objects.get(
            case=self.case,
            scope__revision=self.case.search_generation_policy["conversations"][
                "revision"
            ],
        )
        self.prepare(nearest)
        self.assertEqual(nearest.runs.count(), 2)
        self.assertEqual(
            settings_rows(self.case, self.users["owner"], estimate=True)[0]["estimate"],
            1,
        )

    def test_retained_page_and_conversation_paraphrases_lead_to_originals(self):
        set_policy(self.case, "briefs", True)
        set_policy(self.case, "conversations", True)
        pages = ["List of cabinet locks.", "The east ramp will close at noon."]
        document = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.case.owner,
            title="Gate report",
            file=SimpleUploadedFile("report.txt", "\n".join(pages).encode()),
        )
        inputs = snapshot_sources(self.case, [{"kind": "document", "id": document.pk}])
        source_id = inputs[0]["id"]
        extract = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.case.owner,
            operation="native_text",
            status="complete",
            method_version="1",
            inputs=inputs,
        )
        entries = [
            {
                "label": f"Page {i}",
                "text": page,
                "source_ids": [source_id],
                "anchors": [{"source_id": source_id, "page": i}],
                "values": {},
            }
            for i, page in enumerate(pages, 1)
        ]
        result = AnalysisResult.objects.create(
            case=self.case, run=extract, data={"items": entries}
        )
        passages = [
            ExtractedPassage.objects.create(
                result=result,
                case=self.case,
                kind="document",
                evidence_id=document.pk,
                evidence_sha256=document.sha256,
                item_index=index,
                text=entry["text"],
                anchors=entry["anchors"],
            )
            for index, entry in enumerate(entries)
        ]
        index_parent.call(
            str(self.case.pk),
            str(self.case.owner_id),
            "extraction",
            str(passages[0].pk),
        )
        self.assertTrue(self.search("gate"))
        document.title = "Corridor report"
        document.save(update_fields=["title"])
        self.assertFalse(self.search("gate"))
        self.assertTrue(
            any(hit["source"][0] == "extraction" for hit in self.search("corridor"))
        )
        result_published(result)
        batch = CaseAnalysisBatch.objects.get(case=self.case, scope__target="result")
        self.prepare(batch)
        self.assertEqual(batch.runs.count(), 2)
        run = batch.runs.get(parameters___brief_section__id=str(passages[1].pk))
        (ref,) = self.publish(
            run, "The notice describes a midday access restriction.", [(pages[1], 0)]
        )
        self.assertEqual(ref["extraction_ranges"][0]["original_anchors"][0]["page"], 2)
        (hit,) = self.search("midday")
        self.assertEqual(
            (hit["via"], hit["source"], hit["excerpt"]),
            ("Document brief", ("extraction", passages[1].pk), pages[1]),
        )
        self.assertIn("#finding-2", hit["url"])

        dialogue = ["Can we use the west gate?", "Yes, take the side entrance."]
        messages = [
            TextMessageEvidence.objects.create(
                case=self.case,
                uploaded_by=self.case.owner,
                title="Gate discussion",
                sender=self.person,
                recipient=self.person,
                body=body,
                occurred_at=datetime(2026, 1, 2, tzinfo=UTC) + timedelta(minutes=i),
            )
            for i, body in enumerate(dialogue)
        ]
        _, digest = grouping_scope(self.case, self.person, self.person)
        grouping, _ = start_grouping(
            self.case.owner,
            self.case,
            self.person,
            self.person,
            gap_minutes=60,
            reviewed_scope=digest,
            submission_id=uuid4(),
        )
        self.prepare(grouping)
        batch = CaseAnalysisBatch.objects.get(case=self.case, scope__target="grouping")
        self.prepare(batch)
        summary = batch.runs.get()
        originals = {str(message.pk): message.body for message in messages}
        self.publish(
            summary,
            "The participants agreed on an alternative arrival route.",
            [(originals[row["evidence_id"]], 0) for row in summary.inputs],
        )
        self.assertTrue(summary.result.data["metrics"]["range_covered"])
        matches = self.search("alternative")
        self.assertEqual(
            {(h["via"], str(h["source"][1]), h["excerpt"]) for h in matches},
            {("Conversation summary", str(m.pk), m.body) for m in messages},
        )
        replay, _ = enqueue_generation(self.case, "conversations")
        self.prepare(replay)
        self.assertEqual(replay.runs.count(), 0)

    def test_archive_during_inference_withholds_result_and_restoration_requeues_source(
        self,
    ):
        set_policy(self.case, "briefs", True)
        item = self.original("Gate access changed. " * 50)
        another = self.original("Retained ledger entry. " * 50)
        batch = CaseAnalysisBatch.objects.get(case=self.case, scope__pk=str(item.pk))
        self.prepare(batch)
        run = batch.runs.get()

        def archive(*args):
            TextEvidence.objects.filter(pk=item.pk).update(
                is_archived=True,
                archive_changed_by=self.case.owner,
                archive_changed_at=timezone.now(),
            )
            return {
                "items": [
                    {
                        "kind": "Summary",
                        "text": "The access changed.",
                        "citations": [
                            {
                                "source_id": run.inputs[0]["id"],
                                "start": 0,
                                "quote": "Gate access changed.",
                            }
                        ],
                    }
                ]
            }

        with patch("core.analysis.local.LocalClient.infer", side_effect=archive):
            process_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "failed")
        self.assertFalse(AnalysisResult.objects.filter(run=run).exists())
        TextEvidence.objects.filter(pk=item.pk).update(is_archived=False)
        evidence_archive_changed(
            self.case, self.case.owner, "text", item.pk, archived=False
        )
        restored = CaseAnalysisBatch.objects.get(
            case=self.case, scope__target="restored:text"
        )
        self.prepare(restored)
        self.assertEqual(restored.runs.count(), 1)
        self.assertEqual(restored.runs.get().inputs[0]["evidence_id"], str(item.pk))
        self.assertFalse(
            AnalysisRun.objects.filter(
                inputs__contains=[{"evidence_id": str(another.pk)}]
            ).exists()
        )

    def test_archived_export_blocks_summary_preparation_inference_and_publication(self):
        set_policy(self.case, "conversations", True)
        export = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.case.owner,
            title="Dispatch export",
            file=SimpleUploadedFile("dispatch.txt", b"Shift changed."),
        )
        messages = [
            TextMessageEvidence.objects.create(
                case=self.case,
                uploaded_by=self.case.owner,
                title="Dispatch",
                sender=self.person,
                recipient=self.person,
                body=f"Shift {i} changed.",
                occurred_at=datetime(2026, 1, 2, tzinfo=UTC) + timedelta(minutes=i),
            )
            for i in range(3)
        ]
        CommunicationContext.objects.create(
            case=self.case,
            message=messages[0],
            source_document=export,
            source_sha256=export.sha256,
            source_row=1,
            source_start=0,
            source_end=1,
            parser_version="synthetic",
            timezone="UTC",
            raw_sender="Dispatcher",
        )
        _, digest = grouping_scope(self.case, self.person, self.person)
        grouping, _ = start_grouping(
            self.case.owner,
            self.case,
            self.person,
            self.person,
            gap_minutes=60,
            reviewed_scope=digest,
            submission_id=uuid4(),
        )
        self.prepare(grouping)
        self.prepare(
            CaseAnalysisBatch.objects.get(case=self.case, scope__target="grouping")
        )
        first, last = AnalysisRun.objects.filter(
            operation="session_summaries"
        ).order_by("created_at")

        def archive_export():
            DocumentEvidence.objects.filter(pk=export.pk).update(
                is_archived=True,
                archive_changed_at=timezone.now(),
                archive_changed_by=self.case.owner,
            )

        archive_export()
        backfill, _ = enqueue_generation(self.case, "conversations")
        self.prepare(backfill)
        self.assertEqual(backfill.processed_sources, 0)
        with patch("core.analysis.local.LocalClient.infer") as infer:
            process_run(last.pk)
            infer.assert_not_called()
        last.refresh_from_db()
        self.assertEqual(last.error_code, "source_unavailable")

        DocumentEvidence.objects.filter(pk=export.pk).update(is_archived=False)

        def archive_during_inference(*args):
            archive_export()
            return {
                "items": [
                    {
                        "kind": "Summary",
                        "text": "A shift change was reported.",
                        "citations": [
                            {
                                "source_id": first.inputs[0]["id"],
                                "start": 0,
                                "quote": "Shift",
                            }
                        ],
                    }
                ]
            }

        with patch(
            "core.analysis.local.LocalClient.infer",
            side_effect=archive_during_inference,
        ) as infer:
            process_run(first.pk)
            infer.assert_called_once()
        first.refresh_from_db()
        self.assertEqual(first.error_code, "source_unavailable")
        self.assertFalse(AnalysisResult.objects.filter(run__in=(first, last)).exists())

        DocumentEvidence.objects.filter(pk=export.pk).update(is_archived=False)
        evidence_archive_changed(
            self.case, self.case.owner, "document", export.pk, archived=False
        )
        recovered = CaseAnalysisBatch.objects.get(
            case=self.case,
            scope__feature="conversations",
            scope__target="restored:document",
        )
        self.prepare(recovered)
        self.assertEqual(recovered.runs.count(), 2)
