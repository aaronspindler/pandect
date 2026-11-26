from django.test import TestCase

from core.evidence_chat import corpus
from core.evidence_chat.lineage import corroboration, forwarded_origins
from core.testing import EvidenceTestMixin
from document.models import DocumentEvidence
from phone.models import CommunicationContext, TextMessageEvidence


class SourceLineageTests(EvidenceTestMixin, TestCase):
    def test_native_forward_marker_agrees_in_counts_and_excerpts(self):
        document = self.create_evidence(DocumentEvidence)
        for row, value in enumerate(("yEs", "True", True, 1, "No"), 1):
            with self.subTest(value=value):
                message = self.create_evidence(TextMessageEvidence, body="Arrival")
                CommunicationContext.objects.create(
                    case=self.case,
                    message=message,
                    source_document=document,
                    source_sha256=document.sha256,
                    source_row=row,
                    source_start=row - 1,
                    source_end=row,
                    parser_version="fixture",
                    timezone="UTC",
                    raw_sender="Observer",
                    source_context={"raw_fields": {"Forwarded": value}},
                )
                loc = next(
                    loc
                    for loc in corpus.discover(self.case, False, review=True)
                    if loc["pk"] == str(message.pk)
                )
                expected = value != "No"
                self.assertEqual(loc["message_lineage"]["forwarded"], expected)
                self.assertEqual(bool(forwarded_origins(self.case, [loc])), expected)

    def test_marked_forward_and_finding_are_not_new_independent_accounts(self):
        text = "Dispatch recorded the arrival at 09:25."
        original = self.create_evidence(TextMessageEvidence, body=text)
        forwarded = self.create_evidence(TextMessageEvidence, body="Fwd: " + text)
        rows = {
            loc["evidence_id"]: loc
            for loc in corpus.discover(self.case, False, review=True)
        }
        loc = rows[str(original.pk)]
        copied = rows[str(forwarded.pk)]
        finding = loc | {"key": "finding", "surface": "finding"}
        summary = corroboration(
            [loc, copied, finding], forwarded_origins(self.case, [loc, copied, finding])
        )
        self.assertEqual(
            summary,
            {"originals": 2, "partial": False, "forwards": 1, "repeated_wording": 1},
        )
        self.assertTrue(corpus.read_window(self.case, copied, 0, 4))
        self.assertIsNone(
            corpus.read_window(
                self.case, copied | {"message_lineage": loc["message_lineage"]}, 0, 4
            )
        )
        self.assertTrue(
            corpus.read_window(
                self.case,
                {k: v for k, v in copied.items() if k != "message_lineage"},
                0,
                4,
            )
        )

    def test_hosted_message_search_hit_can_follow_its_reported_reply(self):
        from allauth.account.models import EmailAddress
        from django.utils import timezone

        from core.case_search.hosted import search
        from core.case_search.tests.fixtures import indexed
        from core.evidence_chat.corpus_navigation import linked_materials

        EmailAddress.objects.create(
            user=self.owner, email="owner@example.test", verified=True, primary=True
        )
        export = self.create_evidence(DocumentEvidence)
        first = self.create_evidence(TextMessageEvidence, body="Synthetic dispatch.")
        second = self.create_evidence(TextMessageEvidence, body="I received your note.")
        for i, message in enumerate((first, second), 1):
            CommunicationContext.objects.create(
                case=self.case,
                message=message,
                source_document=export,
                source_sha256=export.sha256,
                source_row=i,
                source_start=i - 1,
                source_end=i,
                parser_version="fixture",
                timezone="UTC",
                native_message_id=str(i),
                raw_sender="Observer",
                native_thread_id="thread",
                reply_to_id="1" if i == 2 else "",
            )
        with indexed(self.case, [("original", "text-message", first.pk)]):
            cutoff = timezone.now()
            ((locator, _),) = search(
                self.case, self.owner, "Synthetic dispatch", through=cutoff
            )
            self.assertTrue(locator["hosted_metadata"])
            linked = linked_materials(
                self.case, locator, "Who responded?", through=cutoff, filters={}
            )
            self.assertEqual(
                [row.locator["evidence_id"] for row in linked], [str(second.pk)]
            )
