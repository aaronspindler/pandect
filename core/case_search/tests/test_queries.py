from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from core.analysis.catalogue import project_result
from core.analysis.services import snapshot_sources
from core.case_search import retained
from core.case_search.queries import parse_query, search, suggest
from core.models import AnalysisResult, AnalysisRun, ExtractedPassage
from core.testing import EvidenceTestMixin
from document.models import DocumentEvidence
from geo.models import PointOfInterest
from people.models import Person
from phone.models import CallEvidenceAttachment, PhoneCallEvidence, TextMessageEvidence
from socialmedia.models import SocialAccount


class QueryTests(SimpleTestCase):
    def test_literals_operators_and_semantics(self):
        query = parse_query('meeting -"cancelled early" OR "café"')
        self.assertEqual(query.terms, ("meeting", "café"))
        self.assertEqual(query.exclusions, ("cancelled early",))
        self.assertFalse(query.semantic_text)
        self.assertEqual(
            parse_query("meeting postponed -next").semantic_text, "meeting postponed"
        )
        for value in ('"exact two"', "plate-A8", "person@example.test", "-only"):
            self.assertFalse(parse_query(value).semantic_text)
        with self.assertRaises(ValidationError):
            parse_query("term " * 17)


class SearchTests(EvidenceTestMixin, TestCase):
    def test_evidence_complex_query_archive_and_date(self):
        now = timezone.now()
        first = self.create_evidence(
            title="Bridge meeting",
            body="Deadline was extended. 7%_A-8",
            collected_at=now - timedelta(days=10),
        )
        self.create_evidence(body="The bridge meeting was cancelled.")
        self.create_evidence(body="7zzA-8 only")
        self.create_evidence(
            case=self.other_case, title="Bridge meeting", body="Other private material"
        )
        page = search(self.case, query='"bridge meeting" -cancelled')
        self.assertEqual([row["id"] for row in page], [first.pk])
        self.assertEqual(page.paginator.count, 1)
        self.assertEqual(page[0]["date_label"], "Collected")
        self.assertEqual(
            search(
                self.case, query="7%_A-8", start=now - timedelta(days=2)
            ).paginator.count,
            0,
        )
        self.assertEqual(
            [row["id"] for row in search(self.case, query="7%_A-8")], [first.pk]
        )
        page = search(self.case, query="extended OR cancelled")
        self.assertEqual(page.paginator.count, 2)

    def test_participants_entities_accounts_and_call_anchors(self):
        viewer = Person.objects.create(
            case=self.case, first_name="Eloise", last_name="Cartwright"
        )
        SocialAccount.objects.create(
            person=viewer, platform="Test", username="eloise.764"
        )
        poi = PointOfInterest.objects.create(
            person=viewer,
            kind="work",
            address="Appletree Street",
            source="registry",
            created_by=self.owner,
            latitude=1,
            longitude=1,
        )
        message = self.create_evidence(
            TextMessageEvidence, body="Dinner was delayed.", sender=viewer
        )
        call = self.create_evidence(PhoneCallEvidence, sender=viewer)
        addition = CallEvidenceAttachment.objects.create(
            phone_call=call,
            case=self.case,
            uploaded_by=self.owner,
            transcript="Callers gathered downtown.",
        )
        matches = search(self.case, query='"Eloise Cartwright"')
        self.assertEqual(
            {row["kind"] for row in matches},
            {"person", "poi", "conversation", "text-message", "phone-call"},
        )
        msg = next(r for r in matches if r["kind"] == "text-message")
        self.assertTrue(msg["url"].endswith(f"#message-{message.pk}"))
        self.assertEqual(
            [r["id"] for r in search(self.case, query="eloise.764")], [viewer.pk]
        )
        self.assertEqual(
            search(self.case, query="downtown")[0]["url"].split("#")[1],
            f"attachment-{addition.pk}",
        )
        self.assertEqual(search(self.case, query="Appletree")[0]["id"], poi.pk)
        quick, _ = suggest(self.case, "Elo")
        self.assertIn(viewer.pk, {r["id"] for r in quick})

    def test_suggestion_typo_and_empty(self):
        self.create_evidence(title="Accommodation")
        quick, alternative = suggest(self.case, "Accomodation")
        self.assertFalse(quick)
        self.assertEqual(alternative, "Accommodation")
        self.assertEqual(suggest(self.other_case, "Accomodation"), ([], ""))

    def test_overlapping_call_matches_keep_one_excerpt_link_and_attribution(self):
        call = self.create_evidence(PhoneCallEvidence)
        addition = CallEvidenceAttachment.objects.create(
            case=self.case,
            phone_call=call,
            uploaded_by=self.owner,
            transcript="Beacon from supplied transcript.",
        )
        inputs = snapshot_sources(self.case, [{"kind": "phone-call", "id": call.pk}])
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.owner,
            operation="transcribe",
            method_version="1",
            inputs=inputs,
            status="partial",
        )
        result = AnalysisResult.objects.create(
            case=self.case, run=run, data={"items": []}
        )
        ExtractedPassage.objects.create(
            case=self.case,
            result=result,
            kind="phone-call",
            evidence_id=call.pk,
            evidence_sha256=call.sha256,
            text="Beacon from derived passage.",
            item_index=0,
        )
        row = search(self.case, query="beacon", record_type="phone-call")[0]
        self.assertEqual(row["excerpt"], "Beacon from derived passage.")
        self.assertTrue(row["url"].endswith("#finding-1"))
        self.assertEqual(row["badges"], ["Extracted content", "Partial coverage"])
        call.description = "Beacon recorded by investigator."
        call.save(update_fields=["description"])
        row = search(self.case, query="beacon", record_type="phone-call")[0]
        self.assertEqual(row["excerpt"], call.description)
        self.assertTrue(row["url"].endswith(f"/{call.pk}/"))
        self.assertEqual(row["badges"], [])
        row = search(self.case, query="supplied", record_type="phone-call")[0]
        self.assertEqual(row["excerpt"], addition.transcript)
        self.assertTrue(row["url"].endswith(f"#attachment-{addition.pk}"))
        self.assertEqual(row["badges"], ["Supplied call material"])

    def test_bulk_message_metadata_update_refreshes_words_and_retains_original(self):
        message = self.create_evidence(
            TextMessageEvidence, title="Original title", body="Frozen source content"
        )
        checksum = message.sha256
        TextMessageEvidence.objects.filter(pk=message.pk).update(
            title="Renamed recording"
        )
        self.assertEqual(
            search(self.case, query="renamed", record_type="text-message")[0]["id"],
            message.pk,
        )
        self.assertEqual(
            search(
                self.case, query="original", record_type="text-message"
            ).paginator.count,
            0,
        )
        message.refresh_from_db()
        self.assertEqual(message.sha256, checksum)
        self.assertEqual(message.body, "Frozen source content")

    def test_stable_paging_and_long_text(self):
        for number in range(27):
            self.create_evidence(title=f"Anchor {number:02}")
        page = search(self.case, query="anchor", record_type="text")
        self.assertEqual(page.paginator.count, 27)
        self.assertEqual(len(page), 25)
        second = search(self.case, query="anchor", record_type="text", page=2)
        self.assertFalse({row["id"] for row in page} & {row["id"] for row in second})
        item = self.create_evidence(body="lorem " * 10000 + "Tailmarker")
        self.assertEqual(
            search(self.case, query="tailmarker", record_type="text")[0]["id"], item.pk
        )

    def test_analysis_counts_and_precise_links_require_live_sources(self):
        collected = timezone.now()
        doc = self.create_evidence(DocumentEvidence, collected_at=collected)
        inputs = snapshot_sources(self.case, [{"kind": "document", "id": doc.pk}])
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.owner,
            operation="native_text",
            method_version="1",
            inputs=inputs,
            status="partial",
        )
        result = AnalysisResult.objects.create(
            case=self.case,
            run=run,
            data={
                "items": [
                    {
                        "label": "Extracted selection",
                        "text": "notes on archways",
                        "source_ids": [inputs[0]["id"]],
                    }
                ]
            },
        )
        project_result(result)
        ExtractedPassage.objects.create(
            case=self.case,
            result=result,
            kind="document",
            evidence_id=doc.pk,
            evidence_sha256=doc.sha256,
            text="notes on archways",
            item_index=0,
        )
        page = search(self.case, query="archways")
        self.assertEqual(page.paginator.count, 2)
        self.assertEqual({r["kind"] for r in page}, {"document", "finding"})
        self.assertTrue(all("Partial coverage" in row["badges"] for row in page))
        self.assertTrue(all(row["url"].endswith("#finding-1") for row in page))
        with patch.object(
            retained, "resolve_inputs", wraps=retained.resolve_inputs
        ) as resolve:
            older = search(
                self.case, query="archways", record_type="document", end=collected
            )
            self.assertEqual(older.paginator.count, 0)
            resolve.assert_not_called()
            current = search(
                self.case, query="archways", record_type="document", start=collected
            )
            self.assertEqual([row["id"] for row in current], [doc.pk])
            resolve.assert_called()
        doc.file.storage.delete(doc.file.name)
        page = search(self.case, query="archways")
        self.assertEqual(page.paginator.count, 0)
        self.assertFalse(page.object_list)
