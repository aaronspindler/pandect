from datetime import UTC, datetime, timedelta
from hashlib import sha256

from django.core.files.base import ContentFile
from django.test import TestCase

from core.analysis.automatic import start_call_history_analysis
from core.analysis.bulk import prepare_case_analysis_batch
from core.analysis.services import resolve_snapshots, snapshot_sources
from core.analysis.sources import Source
from core.evidence.queries import person_evidence, scoped_evidence
from core.evidence_chat.corpus import original_metadata
from core.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence
from people.models import Person
from phone.analysis.deterministic.conversations import cross_channel_links
from phone.analysis.deterministic.reports import call_report
from phone.models import (
    CallHistoryImport,
    CallHistoryRow,
    CallImportSource,
    PhoneCallEvidence,
    link_imported_person,
)


class CallHistoryReaderTests(EvidenceViewTestMixin, TestCase):
    def imported_call(self, *, number=2, identifier="+12025550101"):
        actor = self.users["owner"]
        moment = datetime(2026, 6, 3, 8, number, tzinfo=UTC)
        source, _ = CallImportSource.objects.get_or_create(
            case=self.case, label="Garden line", created_by=actor
        )
        batch = CallHistoryImport.objects.filter(case=self.case, source=source).first()
        if batch is None:
            document = DocumentEvidence.objects.create(
                case=self.case,
                uploaded_by=actor,
                title="Synthetic call history",
                file=ContentFile(b"synthetic call history\n", name="history.csv"),
            )
            batch = CallHistoryImport.objects.create(
                case=self.case,
                document=document,
                source=source,
                source_sha256=document.sha256,
                source_label=source.label,
                uploaded_by=actor,
                requested_by=actor,
                timezone="UTC",
                parser_version="call-history-test",
                status=CallHistoryImport.Status.COMPLETED,
            )
        row = CallHistoryRow.objects.create(
            batch=batch,
            number=number,
            raw={
                "Call type": "Incoming",
                "Date": moment.strftime("%Y-%m-%d %H:%M:%S"),
                "Duration": "00:00:00",
                "Number": identifier,
                "Contact": "Reported contact",
                "Location": "",
                "Service": "Phone",
            },
            kind="phone-call",
            direction="incoming",
            occurred_at=moment,
            duration=timedelta(),
            identifier=identifier,
            disposition=CallHistoryRow.Disposition.PUBLISHED,
        )
        return PhoneCallEvidence.objects.create(
            case=self.case,
            uploaded_by=actor,
            import_row=row,
            title=f"Reported incoming call {number}",
            occurred_at=moment,
            duration=timedelta(),
        )

    def test_imported_call_is_visible_without_people_and_has_source(self):
        call = self.imported_call()
        self.login("viewer")
        response = self.client.get(self.url("detail", call, "phone-call"))
        self.assertContains(response, "Garden line")
        self.assertContains(response, "+12025550101")
        self.assertContains(response, "View retained CSV")
        self.assertNotContains(response, "Link a person")
        day = self.client.get(
            self.url("chronology"), {"view": "day", "day": "2026-06-03"}
        )
        self.assertContains(day, "+12025550101 to Garden line")

    def test_person_link_changes_filters_and_preserves_earlier_input(self):
        call = self.imported_call()
        selected = [{"kind": "phone-call", "id": str(call.pk)}]
        prior = snapshot_sources(self.case, selected)
        original = call.original_bytes()
        chat_source = original_metadata(call, "phone-call")
        link_imported_person(
            call, role="sender", person=self.person, actor=self.users["editor"]
        )
        call.refresh_from_db()
        self.assertEqual(call.original_bytes(), original)
        self.assertEqual(call.sha256, sha256(original).hexdigest())
        self.assertEqual(original_metadata(call, "phone-call"), chat_source)
        visible = scoped_evidence(self.case, "phone-call")
        self.assertEqual(person_evidence(visible, self.person).get(), call)
        self.assertIsNone(resolve_snapshots(prior, self.case)[0].item.sender_id)
        self.assertEqual(
            snapshot_sources(self.case, selected)[0]["call_person_ids"],
            [
                str(self.person.pk),
                None,
            ],
        )

    def test_unmatched_calls_count_without_forming_a_person_pair(self):
        calls = [self.imported_call(number=number) for number in (2, 3)]
        result = call_report(self.case, None, None, None, None, 1, 25, 1)
        self.assertEqual(result["metrics"]["Phone calls"], 2)
        self.assertEqual(result["pair_page"].paginator.count, 0)
        sources = [Source(str(call.pk), "phone-call", call) for call in calls]
        self.assertEqual(cross_channel_links(sources, {})["items"], [])

    def test_import_checks_prepare_only_calls_with_two_confirmed_people(self):
        call = self.imported_call()
        self.imported_call(number=3)
        other = Person.objects.create(case=self.case, first_name="Other participant")
        actor = self.users["owner"]
        link_imported_person(call, role="sender", person=self.person, actor=actor)
        link_imported_person(call, role="recipient", person=other, actor=actor)
        checks, _ = start_call_history_analysis(actor, call.import_row.batch)

        self.assertEqual(prepare_case_analysis_batch(checks.pk), "queued")
        self.assertEqual(prepare_case_analysis_batch(checks.pk), "complete")
        run = checks.runs.get(operation="conversation_link")
        self.assertEqual([row["evidence_id"] for row in run.inputs], [str(call.pk)])
        self.assertEqual(
            run.inputs[0]["call_person_ids"],
            [
                str(self.person.pk),
                str(other.pk),
            ],
        )
