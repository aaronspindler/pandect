import os
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from time import monotonic
from uuid import uuid4

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from core.analysis.catalogue import project_result
from core.analysis.services import snapshot_sources
from core.analysis.sources import Source, source_id
from core.evidence.deletion import execute_deletion, plan_deletion
from core.evidence_chat import corpus, jobs, services
from core.evidence_chat.analysis_reports import analysis_materials
from core.evidence_chat.reports import timing_materials
from core.models import (
    AnalysisReportIndex,
    AnalysisResult,
    AnalysisRun,
    ChatTurn,
    EvidenceChat,
)
from core.testing import EvidenceTestMixin
from people.models import Person
from phone.analysis.deterministic.conversations import _pattern_metrics
from phone.models import TextMessageEvidence


class SavedAnalysisTests(EvidenceTestMixin, TestCase):
    def save_report(self, operation, inputs, metrics, *, status="complete"):
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.owner,
            operation=operation,
            method_version="2",
            inputs=inputs,
            status=status,
        )
        result = AnalysisResult.objects.create(
            case=self.case, run=run, data={"metrics": metrics}
        )
        project_result(result)
        return result

    def test_methods_publish_searchable_measurements_even_without_findings(self):
        original = self.create_evidence()
        inputs = snapshot_sources(self.case, [{"kind": "text", "id": original.pk}])
        result = self.save_report(
            "style_profiles",
            inputs,
            {"unique_words": 47, "vocabulary_ratio": 0.25, "executor_tokens": 16000},
        )
        self.assertFalse(result.findings.exists())
        through = timezone.now()
        source = {"surface": "original", "kind": "text", "pk": str(original.pk)}
        (report,) = analysis_materials(
            self.case, "unique words", through=through, sources=[source]
        )
        self.assertEqual(report.locator["result_id"], str(result.pk))
        self.assertIn("Unique words: 47", report.text)
        self.assertIn(
            "Vocabulary ratio (unique words / total words): 0.25", report.text
        )
        self.assertNotIn("16000", report.text)
        self.assertIn("Method version: 2", report.text)
        self.assertEqual(
            corpus.read_window(self.case, report.locator)["text"], report.text
        )
        self.assertIsNone(corpus.read_window(self.other_case, report.locator))
        newer = self.save_report(
            "style_profiles", inputs, {"unique_words": 48}, status="partial"
        )
        (after,) = analysis_materials(
            self.case,
            "",
            through=timezone.now(),
            after=through,
            sources=[report.locator],
            methods=["style_profiles"],
        )
        self.assertEqual(after.locator["pk"], str(newer.pk))
        self.assertIn("partial", after.locator["gap"])
        self.assertIn("47", corpus.read_window(self.case, report.locator)["text"])
        empty = self.save_report("entities", inputs, {"occurrences": 0}, status="empty")
        (empty_report,) = analysis_materials(
            self.case, "identifiers", through=timezone.now()
        )
        self.assertEqual(empty_report.locator["result_id"], str(empty.pk))
        self.assertIn("Outcome: No findings", empty_report.text)
        self.assertIn("Occurrences: 0", empty_report.text)
        self.assertEqual(
            analysis_materials(self.other_case, "identifiers", through=timezone.now()),
            [],
        )
        original.is_archived = True
        original.archive_changed_at = timezone.now()
        original.archive_changed_by = self.owner
        original.save()
        self.assertEqual(
            analysis_materials(self.case, "writing style", through=timezone.now()), []
        )
        self.assertIsNone(corpus.read_window(self.case, empty_report.locator))

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
        jobs._row(turn, report.locator, "context").save()
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

    def test_sparse_measurement_search_reads_bounded_candidates_among_saved_results(
        self,
    ):
        from core.analysis.report_index import measurements_v1
        from core.evidence_chat.qualification import database_counters

        item = self.create_evidence()
        inputs = snapshot_sources(self.case, [{"kind": "text", "id": item.pk}])
        exact = self.save_report("style_profiles", inputs, {"unique_words": 93})
        costs = []
        for extra in (100, 2900):
            runs = [
                AnalysisRun(
                    case=self.case,
                    requested_by=self.owner,
                    status="complete",
                    operation="entities",
                    method_version="1",
                    inputs=inputs,
                    equivalence_key=sha256(
                        f"entity-run-{len(costs)}-{n}".encode()
                    ).hexdigest(),
                )
                for n in range(extra)
            ]
            AnalysisRun.objects.bulk_create(runs, batch_size=300)
            results = [
                AnalysisResult(
                    case=self.case, run=run, data={"metrics": {"occurrences": i}}
                )
                for i, run in enumerate(runs)
            ]
            AnalysisResult.objects.bulk_create(results, batch_size=300)
            AnalysisReportIndex.objects.bulk_create(
                [
                    AnalysisReportIndex(
                        result=row,
                        text=measurements_v1("entities", row.data["metrics"]),
                    )
                    for row in results
                ],
                batch_size=300,
            )
            with connection.cursor() as cursor:
                cursor.execute(
                    "ANALYZE core_analysisreportindex, core_analysisresult, "
                    "core_analysisrun"
                )
            before = database_counters()
            started = monotonic()
            with CaptureQueriesContext(connection) as captured:
                (hit,) = analysis_materials(
                    self.case, "unique words", through=timezone.now(), automatic=True
                )
            elapsed = monotonic() - started
            after = database_counters()
            self.assertEqual(hit.locator["result_id"], str(exact.pk))
            costs.append((len(captured), {k: after[k] - v for k, v in before.items()}))
            if os.getenv("PANDECT_QUALIFY_CHAT") == "1":
                print(
                    f"Saved report search after adding {extra}: "
                    f"{elapsed:.3f}s, {costs[-1]}"
                )
        self.assertEqual(costs[0][0], costs[1][0])
        for _, counters in costs:
            self.assertLess(counters["rows_read"], 450, costs)
