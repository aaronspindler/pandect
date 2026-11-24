"""Chat windows resolve current same-case versions and quote the exact material."""

from unittest.mock import patch

from allauth.account.models import EmailAddress
from django.test import TestCase, override_settings
from django.utils import timezone

from core.analysis.catalogue import project_result
from core.analysis.services import snapshot_sources
from core.evidence_chat.corpus import cite, discover, rank, read_window
from core.models import AnalysisResult, AnalysisRun, ExtractedPassage
from core.testing import EvidenceTestMixin
from document.models import DocumentEvidence
from geo.models import LocationEvidence
from phone.models import TextMessageEvidence


class CorpusTests(EvidenceTestMixin, TestCase):
    def test_late_match_notes_and_scope(self):
        target = "Café Quayside"
        long_body = "earlier entries\n" * 500 + target
        message = self.create_evidence(TextMessageEvidence, body=long_body)
        other = self.create_evidence(description="only-seen-in-annotations")
        self.create_evidence(case=self.other_case, body=target)
        with (
            override_settings(PANDECT_SEARCH_EMBEDDING_PROVIDER="openai-gateway"),
            patch("core.evidence_chat.corpus_search.related_results") as hosted,
        ):
            hits = rank(
                self.case, "What did the messages say about Quayside?", user=self.owner
            )
            self.assertTrue(hits)
            loc, offset = next(
                (loc, pos) for loc, pos in hits if loc["evidence_id"] == str(message.pk)
            )
            self.assertIn(target, read_window(self.case, loc, offset)["text"])
            annotations = rank(self.case, "only-seen-in-annotations", user=self.owner)
            self.assertFalse(annotations)
            hosted.assert_not_called()
        position = long_body.index(target)
        ref = cite(self.case, loc, position, target)
        self.assertIn("#message-", ref["url"])
        self.assertIsNone(cite(self.case, loc, position - 1, target))
        self.assertIsNone(read_window(self.other_case, loc))
        self.assertNotIn(
            "only-seen-in-annotations", str(list(discover(self.case, False)))
        )
        self.assertNotIn(str(other.pk), [r["evidence_id"] for r, _ in hits])
        message.is_archived = True
        message.archive_changed_by = self.owner

        message.archive_changed_at = timezone.now()
        message.save()
        self.assertIsNone(read_window(self.case, loc))
        self.assertEqual(
            cite(self.case, loc, position, target, include_archived=True)["quote"],
            target,
        )

    def test_cited_text_range_opens_exact_repeated_wording_with_escaping(self):
        item = self.create_evidence(body="café <em> café <em>")
        before_addition = timezone.now()
        self.create_evidence(body="New material")
        (loc,) = discover(self.case, False, through=before_addition)
        quote = "café <em>"
        ref = cite(self.case, loc, item.body.rindex(quote), quote)
        self.assertIn("?passage=10-19#cited-passage", ref["url"])
        self.assertNotIn("caf", ref["url"])
        self.client.force_login(self.owner)
        EmailAddress.objects.create(
            user=self.owner, email="owner@example.test", verified=True
        )
        response = self.client.get(ref["url"])
        self.assertContains(
            response,
            'café &lt;em&gt; <mark id="cited-passage" tabindex="-1">'
            "café &lt;em&gt;</mark>",
        )
        url = ref["url"].split("?")[0]
        self.assertNotContains(
            self.client.get(url, {"passage": "1-999999999999999999"}),
            'id="cited-passage"',
        )
        self.assertNotContains(
            self.client.get(url, {"passage": "4-2"}), 'id="cited-passage"'
        )

    def test_structured_original_and_media_gap(self):
        location = self.create_evidence(LocationEvidence, address="North harbour")
        file = self.create_evidence(DocumentEvidence)
        sources = {loc["pk"]: loc for loc in discover(self.case, False)}
        loc = sources[str(location.pk)]
        window = read_window(self.case, loc)
        quote = "Address: North harbour"
        self.assertEqual(
            cite(self.case, loc, window["text"].index(quote), quote)["url"],
            window["url"],
        )
        self.assertIn("no retained extraction", sources[str(file.pk)]["gap"])
        with file.file.storage.open(file.file.name):
            self.assertIn(
                file.original_filename,
                read_window(self.case, sources[str(file.pk)])["text"],
            )
        file.file.storage.delete(file.file.name)
        self.assertIsNone(read_window(self.case, sources[str(file.pk)]))

    @override_settings(PANDECT_SEARCH_EMBEDDING_PROVIDER="local")
    def test_private_meaning_passage_uses_live_local_source(self):
        item = self.create_evidence(body="First line. " * 60 + "Unload the van here.")
        outsider = self.create_evidence(case=self.other_case)
        offset = item.body.index("Unload")
        related = [
            {
                "source": ("text", key),
                "kind": "text",
                "start": offset,
                "excerpt": "Unload",
            }
            for key in (item.pk, outsider.pk)
        ]
        with patch(
            "core.evidence_chat.corpus_search.related_results",
            return_value=(related, ""),
        ) as meaning:
            hits = rank(self.case, "Where was freight delivered?", user=self.owner)
        self.assertTrue(meaning.call_args.kwargs["include_source"])
        self.assertNotIn("allow_external", meaning.call_args.kwargs)
        ((loc, start),) = hits
        self.assertEqual(loc["pk"], str(item.pk))
        self.assertGreater(start, 0)
        self.assertIn("Unload the van", read_window(self.case, loc, start)["text"])

    def test_file_coverage_requires_a_passage_within_the_review_scope(self):
        first = self.create_evidence(DocumentEvidence, title="First log")
        second = self.create_evidence(DocumentEvidence, title="Second log")
        before_extraction = timezone.now()
        inputs = snapshot_sources(
            self.case, [{"kind": "document", "id": doc.pk} for doc in (first, second)]
        )
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.owner,
            operation="native_text",
            inputs=inputs,
            status="complete",
            method_version="1",
        )
        result = AnalysisResult.objects.create(case=self.case, run=run)
        passage = ExtractedPassage.objects.create(
            case=self.case,
            result=result,
            kind="document",
            evidence_id=first.pk,
            evidence_sha256=first.sha256,
            text="First log entry.",
        )
        second.is_archived = True
        second.archive_changed_by = self.owner
        second.archive_changed_at = timezone.now()
        second.save()

        (active,) = discover(self.case, False)
        self.assertEqual(active["pk"], str(first.pk))
        self.assertIn("no retained extraction", active["gap"])
        included = {loc["pk"]: loc for loc in discover(self.case, True)}
        self.assertFalse(included[str(first.pk)]["gap"])
        self.assertIn(str(passage.pk), included)
        earlier = {
            loc["pk"]: loc
            for loc in discover(self.case, True, through=before_extraction)
        }
        self.assertIn("no retained extraction", earlier[str(first.pk)]["gap"])
        self.assertNotIn(str(passage.pk), earlier)

    def test_partial_page_and_finding_refs_require_intact_original(self):
        document = self.create_evidence(DocumentEvidence)
        inputs = snapshot_sources(self.case, [{"kind": "document", "id": document.pk}])
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.owner,
            operation="native_text",
            inputs=inputs,
            status="partial",
            method_version="1",
        )
        text = "The pickup moved to Friday."
        entry = {
            "label": "Page 9",
            "text": text,
            "source_ids": [inputs[0]["id"]],
            "anchors": [{"source_id": inputs[0]["id"], "page": 9}],
            "values": {},
        }
        result = AnalysisResult.objects.create(
            case=self.case, run=run, data={"items": [entry]}
        )
        project_result(result)
        passage = ExtractedPassage.objects.create(
            case=self.case,
            result=result,
            evidence_id=document.pk,
            evidence_sha256=document.sha256,
            kind="document",
            item_index=0,
            text=text,
            anchors=entry["anchors"],
        )
        values = list(discover(self.case, False))
        self.assertEqual([loc["surface"] for loc in values], ["original", "passage"])
        loc = values[-1]
        self.assertEqual(loc["pk"], str(passage.pk))
        self.assertIn("partial", loc["gap"])
        self.assertIn("Page 9", loc["label"])
        self.assertEqual(
            loc["dependencies"], [{"kind": "document", "id": str(document.pk)}]
        )
        self.assertIn(
            "#finding-1", cite(self.case, loc, text.index("Friday"), "Friday")["url"]
        )
        interpretation = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.owner,
            operation="entities",
            inputs=inputs,
            status="complete",
            method_version="1",
        )
        report = AnalysisResult.objects.create(
            case=self.case,
            run=interpretation,
            data={"items": [dict(entry, label="Retained identifier")]},
        )
        project_result(report)
        finding = next(
            s for s in discover(self.case, False) if s["surface"] == "finding"
        )
        self.assertIn("Derived finding", finding["label"])
        self.assertEqual(finding["record_key"], loc["record_key"])
        document.file.storage.delete(document.file.name)
        self.assertIsNone(read_window(self.case, loc))
        self.assertIsNone(read_window(self.case, finding))
