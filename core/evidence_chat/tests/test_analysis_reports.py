from datetime import UTC, datetime, timedelta
from hashlib import sha256
from unittest.mock import patch
from uuid import UUID, uuid4

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from core.analysis.catalogue import project_result
from core.analysis.sources import Source, source_id
from core.evidence.deletion import execute_deletion, plan_deletion
from core.evidence_chat import corpus, services
from core.evidence_chat.analysis_reports import read_analysis_material
from core.evidence_chat.checkpoints import source_row
from core.evidence_chat.reports import timing_materials
from core.models import (
    AnalysisReportIndex,
    AnalysisResult,
    AnalysisRun,
    ChatTurn,
    EvidenceChat,
)
from core.tests.testing import EvidenceTestMixin
from document.models import DocumentEvidence
from people.models import Person
from phone.analysis.deterministic.conversations import _pattern_metrics
from phone.models import CommunicationContext, TextMessageEvidence
from text.analysis.attributed_sources import snapshot_messages


class SavedAnalysisTests(EvidenceTestMixin, TestCase):
    def save_report(
        self, operation, inputs, metrics, *, status="complete", parameters=None
    ):
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.owner,
            operation=operation,
            method_version="2",
            inputs=inputs,
            parameters=parameters or {},
            status=status,
        )
        result = AnalysisResult.objects.create(
            case=self.case, run=run, data={"metrics": metrics}
        )
        project_result(result)
        return result

    def test_large_attributed_report_uses_all_members_with_bounded_dependency_links(
        self,
    ):
        rows = [
            TextMessageEvidence(
                pk=UUID(int=number),
                case=self.case,
                uploaded_by=self.owner,
                sender=self.person,
                recipient=self.person,
                title="Message",
                body=f"Parcel {number}",
            )
            for number in range(1, 514)
        ]
        for row in rows:
            row.sha256 = sha256(row.original_bytes()).hexdigest()
        TextMessageEvidence.objects.bulk_create(rows, batch_size=250)
        scope = {"version": 1, "person_id": str(self.person.pk)}
        inputs = snapshot_messages(
            self.case,
            scope,
            ({"kind": "text-message", "id": str(row.pk)} for row in reversed(rows)),
        )
        result = self.save_report(
            "style_profiles",
            inputs,
            {"sources_selected": len(rows), "sources_usable": len(rows)},
            parameters={"writing_scope": scope},
        )

        # Writing report discovery must not use the generic all-at-once resolver.
        with patch.object(corpus, "result_sources", side_effect=AssertionError):
            report = read_analysis_material(self.case, result.pk, 0, 1600)
            self.assertEqual(
                corpus.read_window(self.case, report.locator)["text"], report.text
            )
            self.assertIn("Selected inputs: 513", report.text)
            self.assertEqual(report.locator["selected_count"], 513)
            self.assertEqual(
                report.locator["dependencies"],
                [corpus.dependency("text-message", row.pk) for row in rows[:64]],
            )

            row = rows[-1]
            TextMessageEvidence.objects.filter(pk=row.pk).update(body="Changed bytes")
            self.assertIsNone(corpus.read_window(self.case, report.locator))
            TextMessageEvidence.objects.filter(pk=row.pk).update(body=row.body)
            self.assertIsNotNone(corpus.read_window(self.case, report.locator))

            TextMessageEvidence.objects.filter(pk=rows[-1].pk).update(
                is_archived=True,
                archive_changed_by=self.owner,
                archive_changed_at=timezone.now(),
            )
            self.assertIsNone(corpus.read_window(self.case, report.locator))

    def test_historical_writing_reports_recheck_canonical_message_bytes(self):
        original = self.create_evidence(TextMessageEvidence)
        inputs = [
            {
                "id": source_id({"kind": "text-message", "id": original.pk}),
                "kind": "text-message",
                "evidence_id": str(original.pk),
                "sha256": original.sha256,
            }
        ]
        result = self.save_report("style_profiles", inputs, {"sources_selected": 1})
        self.assertIsNotNone(read_analysis_material(self.case, result.pk, 0, 1600))
        TextMessageEvidence.objects.filter(pk=original.pk).update(body="Changed text")
        self.assertIsNone(read_analysis_material(self.case, result.pk, 0, 1600))

    def test_group_context_change_invalidates_warm_writing_verification(self):
        document = self.create_evidence(DocumentEvidence)
        group = self.create_evidence(
            TextMessageEvidence, group_source=document, recipient=None
        )
        link = CommunicationContext.objects.create(
            case=self.case,
            message=group,
            source_document=document,
            source_sha256=document.sha256,
            source_row=1,
            source_start=0,
            source_end=5,
            parser_version="imessage-csv-v1",
            timezone="UTC",
            raw_sender="Synthetic sender",
        )
        scope = {"version": 1, "person_id": str(self.person.pk)}
        inputs = snapshot_messages(
            self.case, scope, [{"kind": "text-message", "id": str(group.pk)}]
        )
        result = self.save_report(
            "style_profiles",
            inputs,
            {"sources_selected": 1},
            parameters={"writing_scope": scope},
        )
        self.assertIsNotNone(read_analysis_material(self.case, result.pk, 0, 1600))
        with patch(
            "text.analysis.attributed_sources.verify_membership",
            side_effect=AssertionError("The current verification should be reused."),
        ):
            self.assertIsNotNone(read_analysis_material(self.case, result.pk, 0, 1600))

        CommunicationContext.objects.filter(pk=link.pk).update(source_sha256="0" * 64)
        self.assertIsNone(read_analysis_material(self.case, result.pk, 0, 1600))
        CommunicationContext.objects.filter(pk=link.pk).update(
            source_sha256=document.sha256
        )
        self.assertIsNotNone(read_analysis_material(self.case, result.pk, 0, 1600))
        DocumentEvidence.objects.filter(pk=document.pk).update(case=self.other_case)
        self.assertIsNone(read_analysis_material(self.case, result.pk, 0, 1600))

    def test_large_timing_report_reuses_verified_manifest_and_follows_uncited_members(
        self,
    ):
        stamp = datetime(2026, 1, 10, tzinfo=UTC)
        other = Person.objects.create(case=self.case, first_name="Reader")
        rows = [
            TextMessageEvidence(
                case=self.case,
                uploaded_by=self.owner,
                sender=self.person if n % 2 else other,
                recipient=other if n % 2 else self.person,
                title="Message",
                body=f"Parcel {n}",
                occurred_at=stamp + timedelta(seconds=n * 30),
            )
            for n in range(1200)
        ]
        for row in rows:
            row.sha256 = sha256(row.original_bytes()).hexdigest()
        TextMessageEvidence.objects.bulk_create(rows, batch_size=250)
        inputs = [
            {
                "id": source_id({"kind": "text-message", "id": row.pk}),
                "kind": "text-message",
                "evidence_id": str(row.pk),
                "sha256": row.sha256,
            }
            for row in rows
        ]

        # Use the owning method's measurements for a historical result whose
        # selection is larger than today's interactive analysis limit.
        sources = [
            Source(selected["id"], "text-message", row)
            for selected, row in zip(inputs, rows, strict=True)
        ]
        metrics = _pattern_metrics(sources, [{"sources": sources}])
        result = self.save_report("comm_patterns", inputs, metrics)
        result.refresh_from_db()
        self.assertEqual(len(result.report_index.verified_messages["sample"]), 4)
        cutoff = timezone.now()
        with CaptureQueriesContext(connection) as captured:
            (report,) = timing_materials(self.case, through=cutoff)
            self.assertEqual(
                corpus.read_window(self.case, report.locator)["text"], report.text
            )
        self.assertTrue(
            all("phone_textmessageevidence" not in query["sql"] for query in captured)
        )
        self.assertIn("Selected messages: 1200", report.text)
        self.assertIn("Response interval count: 1199", report.text)
        self.assertIn("Median response interval (seconds): 30", report.text)
        self.assertEqual(report.locator["selected_count"], 1200)
        sample = {dep["id"] for dep in report.locator["dependencies"]}
        self.assertEqual(len(sample), 4)
        self.assertNotIn(inputs[-1]["evidence_id"], sample)
        quote = "Selected messages: 1200"
        start = report.text.index(quote)
        turn = ChatTurn.objects.create(
            case=self.case,
            chat=EvidenceChat.objects.create(
                case=self.case, created_by=self.owner, title="Timing"
            ),
            asked_by=self.owner,
            question="How many messages were selected?",
            ordinal=1,
            submission_id=uuid4(),
            status="complete",
            answer={
                "claims": [
                    {
                        "text": quote,
                        "citations": [
                            {
                                "key": report.locator["key"],
                                "start": start,
                                "end": start + len(quote),
                                "quote": quote,
                            }
                        ],
                    }
                ]
            },
        )
        source_row(turn, report.locator, "context").save()
        self.assertEqual(
            services.inspect_turn(turn)["claims"][0]["citations"][0]["quote"], quote
        )
        later = self.save_report(
            "comm_patterns",
            inputs[-1:],
            _pattern_metrics(sources[-1:], [{"sources": sources[-1:]}]),
        )
        (updated,) = timing_materials(
            self.case, through=timezone.now(), after=cutoff, sources=[report.locator]
        )
        self.assertEqual(updated.locator["pk"], str(later.pk))
        row = rows[-1]
        TextMessageEvidence.objects.filter(pk=row.pk).update(
            is_archived=True,
            archive_changed_by=self.owner,
            archive_changed_at=timezone.now(),
        )
        self.assertIsNone(corpus.read_window(self.case, report.locator))
        self.assertIsNone(services.inspect_turn(turn))
        TextMessageEvidence.objects.filter(pk=row.pk).update(
            is_archived=False, archive_changed_at=timezone.now()
        )
        self.assertIsNotNone(corpus.read_window(self.case, report.locator))
        self.assertIsNotNone(services.inspect_turn(turn))
        # The previous case must also invalidate if a record or a reported
        # party moves away. No selected message need otherwise have changed.
        TextMessageEvidence.objects.filter(pk=row.pk).update(case=self.other_case)
        self.assertIsNone(services.inspect_turn(turn))
        TextMessageEvidence.objects.filter(pk=row.pk).update(case=self.case)
        self.assertIsNotNone(services.inspect_turn(turn))
        Person.objects.filter(pk=other.pk).update(case=self.other_case)
        self.assertIsNone(services.inspect_turn(turn))
        Person.objects.filter(pk=other.pk).update(case=self.case)
        self.assertIsNotNone(services.inspect_turn(turn))
        # Keep the body and evidence revision steady but damage the stored selection.
        AnalysisRun.objects.filter(pk=result.run_id).update(
            inputs=inputs[:-1] + [inputs[-1] | {"sha256": "0" * 64}]
        )
        self.assertIsNone(corpus.read_window(self.case, report.locator))
        AnalysisRun.objects.filter(pk=result.run_id).update(inputs=inputs)
        plan = plan_deletion(row, "text-message")
        self.assertFalse(plan.blocker)
        self.assertIn(str(turn.pk), {entry["id"] for entry in plan.entries})
        self.owner.is_superuser = True
        self.owner.save(update_fields=["is_superuser"])
        execute_deletion(plan, self.owner)
        self.assertFalse(ChatTurn.objects.filter(pk=turn.pk).exists())
        self.assertFalse(AnalysisReportIndex.objects.filter(result=result.pk).exists())
        self.assertIsNone(corpus.read_window(self.case, report.locator))
