"""Passage selection and bounded source navigation for ordinary chat questions."""

from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from uuid import UUID

from django.http import Http404
from django.test import TestCase, override_settings
from django.utils import timezone

from core.analysis.catalogue import project_result
from core.analysis.services import snapshot_sources
from core.evidence_chat.corpus import original_keys, rank, read_window
from core.evidence_chat.corpus_navigation import _closest_event
from core.models import AnalysisResult, AnalysisRun
from core.testing import EvidenceTestMixin
from document.models import DocumentEvidence
from geo.models import LocationEvidence
from phone.models import CommunicationContext, PhoneCallEvidence, TextMessageEvidence


@override_settings(PANDECT_SEARCH_EMBEDDING_PROVIDER="disabled")
class RetrievalTests(EvidenceTestMixin, TestCase):
    def search(self, question, **kwargs):
        return rank(self.case, question, user=self.owner, **kwargs)

    def test_best_lexical_cluster_and_distinct_semantic_window_survive(self):
        first = "The observer arrived."
        correction = "An official later withdrew the estimate."
        answer = "We used the longer track for the count."
        body = first + " filler" * 200 + correction + " filler" * 200 + answer
        target = self.create_evidence(body=body)
        query = "Which observer used the track for the count?"
        ranked = self.search(query)
        self.assertIn(answer, read_window(self.case, *ranked[0], 900)["text"])

        hit = {
            "source": ("text", target.pk),
            "kind": "text",
            "start": body.index(correction),
            "excerpt": correction,
        }
        for i in range(12):
            self.create_evidence(body=f"Observer observed event {i}.")
        ranked = self.search(query, related=lambda _, hit=hit: ([hit], ""))
        windows = [
            read_window(self.case, loc, start, 900)["text"]
            for loc, start in ranked
            if loc["pk"] == str(target.pk)
        ]
        self.assertEqual(len(ranked), 10)
        self.assertEqual(len(windows), 2)
        self.assertIn(correction, windows[0])
        self.assertIn(answer, windows[1])

    def test_repeated_terms_choose_late_cluster_over_spread_first_mentions(self):
        filler = " background" * 160
        scattered = filler.join(("observer", "count", "sound"))
        answer = "The observer used this sound for the count."
        self.create_evidence(body=scattered + filler + answer)
        ranked = self.search("observer sound count")
        self.assertIn(answer, read_window(self.case, *ranked[0], 900)["text"])

    def test_packing_nearby_semantic_and_lexical_spans_preserves_both_anchors(self):
        first, last = "crane", "Final confirmation"
        for spacing in (840, 920):
            with self.subTest(spacing=spacing):
                target = self.create_evidence(
                    body="." * 200 + first + "." * spacing + last
                )
                hit = {
                    "source": ("text", target.pk),
                    "kind": "text",
                    "start": target.body.index(last),
                    "excerpt": last,
                }
                ranked = self.search(first, related=lambda _, hit=hit: ([hit], ""))
                windows = [
                    (start, read_window(self.case, loc, start, 900)["text"])
                    for loc, start in ranked
                    if loc["pk"] == str(target.pk)
                ]
                displayed = " ".join(text for _, text in windows)
                self.assertIn(first, displayed)
                self.assertIn(last, displayed)
                self.assertLessEqual(len(windows), 2)
                if len(windows) == 2:
                    self.assertGreaterEqual(abs(windows[1][0] - windows[0][0]), 900)

    def test_stale_semantic_excerpt_and_out_of_scope_hints_are_skipped(self):
        active = self.create_evidence(body="A dated delivery at the landing.")
        original = self.search("dated")[0]
        earlier = timezone.now()
        later = self.create_evidence(body="A second delivery is recorded.")
        stale = {
            "source": ("text", active.pk),
            "kind": "text",
            "start": 0,
            "excerpt": "Wrong text",
        }
        self.assertFalse(self.search("quasar", related=lambda _: ([stale], "")))
        later_hint = next(
            pair for pair in self.search("delivery") if pair[0]["pk"] == str(later.pk)
        )
        self.assertFalse(self.search("quasar", hints=[later_hint], through=earlier))
        active.is_archived = True
        active.archive_changed_at = timezone.now()
        active.archive_changed_by = self.owner
        active.save()
        self.assertFalse(self.search("quasar", hints=[original]))
        self.assertEqual(
            self.search("quasar", include_archived=True, hints=[original]), [original]
        )
        with self.assertRaises(Http404):
            rank(self.case, "delivery", user=self.outsider)

    def test_invalid_high_scoring_candidates_are_backfilled(self):
        for _ in range(11):
            bad = self.create_evidence(DocumentEvidence, title="Dock crane transfer")
            bad.file.storage.delete(bad.file.name)
        valid = self.create_evidence(body="Crane arrived yesterday.")
        ranked = self.search("dock crane transfer", limit=1)
        self.assertEqual([loc["pk"] for loc, _ in ranked], [str(valid.pk)])

    def test_unique_native_reply_follows_the_original_in_scope(self):
        export = self.create_evidence(DocumentEvidence)
        child = self.create_evidence(TextMessageEvidence, body="Final zephyr.")
        parent = self.create_evidence(TextMessageEvidence, body="The old place.")

        def context(message, row, native, reply=""):
            return CommunicationContext.objects.create(
                case=self.case,
                message=message,
                source_document=export,
                source_sha256=export.sha256,
                source_row=row,
                source_start=row - 1,
                source_end=row,
                parser_version="synthetic",
                timezone="UTC",
                raw_sender="Porter",
                native_message_id=native,
                native_thread_id="reported-thread",
                reply_to_id=reply,
            )

        context(parent, 1, "m1")
        context(child, 2, "m2", "m1")
        self.assertEqual(
            [loc["pk"] for loc, _ in self.search("zephyr")],
            [str(child.pk), str(parent.pk)],
        )
        duplicate = self.create_evidence(
            TextMessageEvidence, body="Independent wording."
        )
        context(duplicate, 3, "m1")
        self.assertEqual(
            [loc["pk"] for loc, _ in self.search("zephyr")], [str(child.pk)]
        )

    def test_direct_record_uuid_skips_embedding_and_unrelated_records(self):
        found = self.create_evidence(body="Exact record lookup.")
        other = self.create_evidence(case=self.other_case, body="Another case.")
        related = Mock(
            side_effect=AssertionError("Direct ID lookups use the primary keys")
        )
        result = self.search(f"Show me record {found.pk}", related=related)
        self.assertEqual([loc["pk"] for loc, _ in result], [str(found.pk)])
        self.assertFalse(self.search(str(other.pk), related=related))
        related.assert_not_called()

    def test_explicit_filters_apply_to_every_candidate_in_case_local_time(self):
        from core.evidence_chat.search_scope import search_filters

        before = self.create_evidence(
            PhoneCallEvidence,
            title="Zephyr",
            occurred_at=datetime(2025, 2, 1, 4, 59, tzinfo=UTC),
        )
        selected = self.create_evidence(
            PhoneCallEvidence,
            title="Zephyr",
            occurred_at=datetime(2025, 2, 1, 5, tzinfo=UTC),
        )
        self.create_evidence(body="Zephyr")
        hints = self.search("Zephyr")
        self.assertIn(str(before.pk), [loc["pk"] for loc, _ in hints])
        filters = search_filters(
            'on:2025-02-01 type:phone-call "on:2024-01-01"', "America/Toronto"
        )
        ranked = self.search("Zephyr", hints=hints, filters=filters)
        self.assertEqual([loc["pk"] for loc, _ in ranked], [str(selected.pk)])

    def test_chronology_uses_reported_times_and_scope(self):
        moment = timezone.now()
        anchor = self.create_evidence(
            PhoneCallEvidence, title="Zephyr call", occurred_at=moment
        )
        after = self.create_evidence(
            LocationEvidence, observed_at=moment + timedelta(minutes=2)
        )
        earlier = timezone.now()
        self.create_evidence(
            LocationEvidence, observed_at=moment + timedelta(minutes=1)
        )
        self.create_evidence(
            title="Unrelated acquisition", collected_at=moment + timedelta(seconds=1)
        )
        ranked = self.search("What happened after Zephyr?", through=earlier)
        self.assertEqual(
            [loc["pk"] for loc, _ in ranked], [str(anchor.pk), str(after.pk)]
        )

    def test_closest_event_preserves_tie_order_with_one_database_query(self):
        stamp = datetime(2026, 11, 10, 12, tzinfo=UTC)
        nearest = stamp - timedelta(seconds=1)
        self.create_evidence(PhoneCallEvidence, occurred_at=nearest, id=UUID(int=1))
        self.create_evidence(LocationEvidence, observed_at=nearest, id=UUID(int=3))
        expected = self.create_evidence(
            LocationEvidence, observed_at=nearest, id=UUID(int=2)
        )
        self.create_evidence(LocationEvidence, observed_at=stamp, id=UUID(int=4))
        with self.assertNumQueries(1):
            self.assertEqual(
                list(_closest_event(self.case, stamp, True, False, None)),
                [("original", "location", str(expected.pk))],
            )

    def test_independent_report_precedes_derived_copies_and_summary_links_to_primary(
        self,
    ):
        first = self.create_evidence(
            title="Zephyr crane transfer", body="First account."
        )
        second = self.create_evidence(title="Transfer", body="Competing account.")
        inputs = snapshot_sources(
            self.case, [{"kind": "text", "id": item.pk} for item in (first, second)]
        )
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.owner,
            operation="entities",
            inputs=inputs,
            status="complete",
            method_version="1",
        )
        item = {
            "label": "Zephyr crane transfer",
            "text": "Rare interpretation",
            "source_ids": [inputs[0]["id"]],
            "anchors": [],
            "values": {},
        }
        result = AnalysisResult.objects.create(
            case=self.case, run=run, data={"items": [item]}
        )
        project_result(result)
        ranked = self.search("Zephyr crane transfer")
        self.assertEqual(
            [loc["pk"] for loc, _ in ranked[:2]], [str(first.pk), str(second.pk)]
        )
        expanded = self.search("Rare interpretation")
        self.assertEqual(
            [loc["surface"] for loc, _ in expanded], ["original", "finding"]
        )
        self.assertEqual(expanded[0][0]["pk"], str(first.pk))
        self.assertEqual(original_keys(expanded[0][0]), original_keys(expanded[1][0]))

    def test_consensus_preserves_top_exact_and_meaning_only_originals(self):
        exact = self.create_evidence(id=UUID(int=1), body="harbour berth ledger")
        shared = [
            self.create_evidence(id=UUID(int=i + 2), body=f"harbour berth {i}")
            for i in range(12)
        ]
        paraphrase = self.create_evidence(body="Moored by the old jetty.")
        weak = self.create_evidence(body="The skiff crossed a channel.")
        hits = [
            {
                "source": ("text", item.pk),
                "kind": "text",
                "start": 0,
                "excerpt": item.body,
                "strong_match": item != weak,
            }
            for item in (paraphrase, weak, *shared)
        ]
        confirmed = set()
        ranked = self.search(
            "harbour berth ledger",
            related=lambda _: (hits, ""),
            semantic_hints=confirmed,
        )
        originals = [loc["pk"] for loc, _ in ranked]
        self.assertEqual(len(set(originals)), 10)
        self.assertIn(str(exact.pk), originals)
        self.assertIn(str(paraphrase.pk), originals)
        self.assertTrue(any(key.endswith(str(paraphrase.pk)) for key, _ in confirmed))
        self.assertFalse(any(key.endswith(str(weak.pk)) for key, _ in confirmed))
        self.assertTrue(confirmed <= {(loc["key"], pos) for loc, pos in ranked})

    def test_reciprocal_ranks_can_promote_shared_candidate(self):
        left = self.create_evidence(id=UUID(int=1), body="zephyr")
        both = self.create_evidence(id=UUID(int=2), body="zephyr")
        semantic = self.create_evidence(id=UUID(int=3), body="independent")

        def hit(row, excerpt):
            return {
                "source": ("text", row.pk),
                "kind": "text",
                "start": 0,
                "excerpt": excerpt,
            }

        ranked = self.search(
            "zephyr",
            related=lambda _: ([hit(semantic, "independent"), hit(both, "zephyr")], ""),
        )
        self.assertEqual(
            [loc["pk"] for loc, _ in ranked],
            [str(both.pk), str(left.pk), str(semantic.pk)],
        )
