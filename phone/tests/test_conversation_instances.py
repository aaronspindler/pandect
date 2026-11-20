from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

from django.http import Http404
from django.test import TestCase

from core.analysis.catalogue import finding_rows, project_result
from core.analysis.operations import get_operation
from core.analysis.services import resolve_inputs, snapshot_sources
from core.models import AnalysisResult, AnalysisRun
from core.testing import EvidenceViewTestMixin
from people.models import Person
from phone.conversation_instances import get_session_instance, instance_page
from phone.conversations import pair_messages
from phone.models import TextMessageEvidence


class ConversationInstanceTests(EvidenceViewTestMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.other = Person.objects.create(case=cls.case, first_name="Blair")
        cls.third = Person.objects.create(case=cls.case, first_name="Casey")

    @property
    def messages(self):
        return pair_messages(self.case, self.person, self.other)

    def message(self, number, *, recipient=None, timed=True):
        return TextMessageEvidence.objects.create(
            case=self.case,
            uploaded_by=self.case.owner,
            sender=self.person,
            recipient=recipient or self.other,
            title=f"Original {number}",
            body=f"Synthetic message {number}",
            occurred_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=number)
            if timed
            else None,
        )

    def run_for(self, method, messages, **parameters):
        return AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.case.owner,
            operation=method,
            method_version=get_operation(method).version,
            inputs=snapshot_sources(
                self.case,
                [{"kind": "text-message", "id": item.pk} for item in messages],
            ),
            parameters=parameters,
            status="complete",
        )

    def publish_groups(self, messages, **parameters):
        run = self.run_for("sessions", messages, **parameters)
        data = get_operation("sessions").function(
            resolve_inputs(run, self.case), run.parameters
        )
        result = AnalysisResult.objects.create(case=self.case, run=run, data=data)
        project_result(result)
        return list(result.findings.order_by("item_index"))

    def publish_report(self, messages, chunks, **parameters):
        run = self.run_for("session_summaries", messages, **parameters)
        items = []
        for session, chunk, statement in chunks:
            keys = [run.inputs[index]["id"] for index in chunk]
            items.append(
                {
                    "text": statement,
                    "label": "Summary",
                    "source_ids": keys[:1],
                    "values": {
                        "inferred_session": session,
                        "chunk_source_ids": keys,
                        "status": "unresolved",
                    },
                }
            )
        return AnalysisResult.objects.create(
            case=self.case, run=run, data={"items": items}
        )

    def test_complete_memberships_deduplicate_and_overlap_across_pages(self):
        rows = [self.message(number) for number in range(53)]
        selected = [*rows[:1], *rows[2:]]
        (older,) = self.publish_groups(selected, gap_minutes=60)
        (latest,) = self.publish_groups(selected, gap_minutes=50)
        (overlap,) = self.publish_groups(rows[:3])
        page = instance_page(
            self.case, self.messages, instance_id=latest.pk, message_page=2
        )
        self.assertEqual(page.paginator.count, 2)
        self.assertEqual({row["id"] for row in page}, {latest.pk, overlap.pk})
        group = next(row for row in page if row["id"] == latest.pk)
        self.assertEqual(group["count"], 52)
        self.assertEqual(group["member_ids"], tuple(item.pk for item in selected))
        self.assertEqual(list(group["messages"]), selected[50:])
        self.assertEqual(group["first_at"], selected[0].occurred_at)
        self.assertEqual(group["last_at"], selected[-1].occurred_at)
        self.assertEqual(
            get_session_instance(self.case, self.messages, latest.pk)["source_ids"],
            tuple(latest.result.run.inputs[i]["id"] for i in range(52)),
        )
        with self.assertRaises(Http404):
            get_session_instance(self.case, self.messages, older.pk)

    def test_ordinary_paging_and_linked_instance_include_unknown_times_last(self):
        untimed = self.message(0, timed=False)
        messages = [self.message(i * 10) for i in range(26)]
        groups = self.publish_groups([untimed, *reversed(messages)], gap_minutes=1)
        with patch(
            "phone.conversation_instances.finding_rows", wraps=finding_rows
        ) as resolved:
            first = instance_page(self.case, self.messages)
        self.assertEqual(sum(len(call.args[1]) for call in resolved.call_args_list), 25)
        self.assertEqual(len(first), 25)
        self.assertEqual(first[0]["first_at"], messages[0].occurred_at)
        self.assertTrue(all(row["messages"] is None for row in first))
        last = instance_page(self.case, self.messages, instance_id=groups[-1].pk)
        self.assertEqual(last.number, 2)
        self.assertEqual(
            [row["first_at"] for row in last], [messages[-1].occurred_at, None]
        )
        self.assertEqual(list(last[-1]["messages"]), [untimed])

    def test_older_copies_skip_validation_after_a_current_representative(self):
        message, other = self.message(1), self.message(120)
        for gap in range(1, 55):
            (latest_valid,) = self.publish_groups([message], gap_minutes=gap)
        # This newer run's first group has the same membership, but a different
        # input later changed. The older, independent run still validates.
        self.publish_groups([message, other], gap_minutes=1)
        TextMessageEvidence.objects.filter(pk=other.pk).update(sha256="f" * 64)

        with patch(
            "phone.conversation_instances.finding_rows", wraps=finding_rows
        ) as resolved:
            page = instance_page(self.case, self.messages)
        self.assertEqual(page[0]["id"], latest_valid.pk)
        self.assertEqual(page[1], {"unavailable": True})
        validated = [
            finding for call in resolved.call_args_list for finding in call.args[1]
        ]
        self.assertLessEqual(
            sum(finding.member_key == [message.pk] for finding in validated), 50
        )

    def test_missing_inconsistent_or_stale_groups_are_unavailable(self):
        first, second = self.message(1), self.message(2)
        other_pair = self.message(3, recipient=self.third)
        (foreign,) = self.publish_groups([other_pair])
        (stale,) = self.publish_groups([first, second])
        TextMessageEvidence.objects.filter(pk=second.pk).update(sha256="0" * 64)
        self.assertEqual(
            list(instance_page(self.case, self.messages)), [{"unavailable": True}]
        )
        for target in (foreign.pk, stale.pk, uuid4(), "bad"):
            with self.subTest(target=target), self.assertRaises(Http404):
                get_session_instance(self.case, self.messages, target)

    def test_one_current_report_must_cover_exact_session_across_its_chunks(self):
        originals = [self.message(i) for i in range(4)]
        (group,) = self.publish_groups(originals[:3])

        def summary():
            return get_session_instance(self.case, self.messages, group.pk)["summary"]

        self.publish_report(
            originals, [(1, [0, 1], "Partial"), (2, [2], "A different session")]
        )
        self.publish_report(
            originals, [(1, [0, 1, 2, 3], "A larger selection")], gap_minutes=20
        )
        self.publish_report(originals, [(1, [0, 1], "First two")], gap_minutes=30)
        self.publish_report(originals, [(1, [2], "Third only")], gap_minutes=40)
        # A fourth input may be an empty-output chunk of the same session.
        self.publish_report(
            originals, [(1, [0, 1, 2], "A partial session")], gap_minutes=45
        )
        self.assertIsNone(summary())
        result = self.publish_report(
            originals,
            [
                (1, [0, 1], "Discussed the meeting"),
                (1, [2], "Confirmed the time"),
                (2, [3], "A different session"),
            ],
            gap_minutes=50,
        )
        saved = summary()
        self.assertEqual(
            [s["text"] for s in saved["statements"]],
            ["Discussed the meeting", "Confirmed the time"],
        )
        self.assertTrue(
            all(str(result.run_id) in s["url"] for s in saved["statements"])
        )
        self.publish_report(originals, [], gap_minutes=50)
        self.assertIsNone(summary())

    def test_conflicting_chunk_assignments_never_establish_full_coverage(self):
        originals = [self.message(i) for i in range(2)]
        (group,) = self.publish_groups(originals)
        self.publish_report(
            originals,
            [(1, [0, 1], "One possible account"), (2, [1], "Conflicting assignment")],
        )
        self.assertIsNone(
            get_session_instance(self.case, self.messages, group.pk)["summary"]
        )

    def test_stale_report_inputs_withhold_its_summary(self):
        first, second = self.message(0), self.message(1)
        (group,) = self.publish_groups([first])
        self.publish_report(
            [first, second],
            [(1, [0], "Earlier summary"), (2, [1], "Subsequent conversation")],
        )
        TextMessageEvidence.objects.filter(pk=second.pk).update(sha256="f" * 64)
        self.assertIsNone(
            get_session_instance(self.case, self.messages, group.pk)["summary"]
        )
