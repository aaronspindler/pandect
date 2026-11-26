from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

from django.db import connection
from django.http import Http404
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from core.analysis.bulk import prepare_case_analysis_batch
from core.analysis.catalogue import finding_rows, project_result
from core.analysis.navigation import finding_url
from core.analysis.operations import get_operation
from core.analysis.source_resolution import resolve_inputs
from core.analysis.source_selection import snapshot_sources
from core.models import AnalysisResult, AnalysisRun, CaseAnalysisBatch
from core.testing import EvidenceViewTestMixin
from people.models import Person
from phone.conversation_instances import (
    get_session_instance,
    instance_page,
    result_instance_url,
)
from phone.conversation_sessions import attach_session_groups
from phone.conversations import pair_messages
from phone.daily_chronology import attach_daily_message_previews, daily_message_rows
from phone.grouping_jobs import grouping_scope, start_grouping
from phone.models import ConversationGrouping, TextMessageEvidence


class ConversationInstanceTests(EvidenceViewTestMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.other = Person.objects.create(case=cls.case, first_name="Blair")
        cls.third = Person.objects.create(case=cls.case, first_name="Casey")

    @property
    def messages(self):
        return pair_messages(self.case, self.person, self.other)

    def message(self, number, *, recipient=None, timed=True, body=None):
        return TextMessageEvidence.objects.create(
            case=self.case,
            uploaded_by=self.case.owner,
            sender=self.person,
            recipient=recipient or self.other,
            title=f"Original {number}",
            body=f"Synthetic message {number}" if body is None else body,
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

    def test_group_conversation_reviews_current_pair_and_queues_only_active_messages(
        self,
    ):
        first = self.message(1)
        archived = self.message(2)
        TextMessageEvidence.objects.filter(pk=archived.pk).update(
            is_archived=True,
            archive_changed_at=archived.created_at,
            archive_changed_by=self.case.owner,
        )
        self.message(3, recipient=self.third)
        args = (self.case.pk, *sorted((self.person.pk, self.other.pk)))
        url = reverse("phone:group_conversation", args=args)
        reader = reverse(
            "phone:conversation_detail", args=args, query={"tab": "instances"}
        )
        self.login("viewer")
        self.assertNotContains(self.client.get(reader), "Group this conversation")
        self.assertEqual(self.client.post(url).status_code, 403)
        self.login("editor")
        self.assertContains(self.client.get(reader), "Group this conversation")
        review = self.client.get(url)
        self.assertContains(review, "Group 1 active message")
        form = review.context["form"]
        payload = {
            "reviewed_scope": form["reviewed_scope"].value(),
            "submission_id": form["submission_id"].value(),
            "gap_minutes": 40,
        }
        second = self.message(4)
        updated = self.client.post(url, payload)
        self.assertContains(updated, "The messages changed", status_code=400)
        self.assertFalse(AnalysisRun.objects.exists())
        payload["reviewed_scope"] = updated.context["form"]["reviewed_scope"].value()
        self.assertRedirects(self.client.post(url, payload), reader)
        revision = ConversationGrouping.objects.get()
        self.assertEqual(revision.parameters["gap_minutes"], 40)
        self.assertEqual(prepare_case_analysis_batch(revision.batch_id), "complete")
        session = revision.sessions.get()
        self.assertEqual(
            list(
                session.members.order_by("ordinal").values_list("message_id", flat=True)
            ),
            [first.pk, second.pk],
        )
        self.assertContains(self.client.get(reader), "1 saved session")
        self.assertContains(self.client.get(reader), "Group this conversation")

    def group_pair(self, gap=60):
        _, digest = grouping_scope(self.case, self.person, self.other)
        batch, _ = start_grouping(
            self.case.owner,
            self.case,
            self.person,
            self.other,
            gap_minutes=gap,
            reviewed_scope=digest,
            submission_id=uuid4(),
        )
        for _ in range(25):
            if prepare_case_analysis_batch(batch.pk) != "queued":
                break
        batch.refresh_from_db()
        self.assertEqual(
            batch.status, CaseAnalysisBatch.Status.COMPLETE, batch.error_code
        )
        return batch.conversation_groupings.get()

    def test_long_exchange_pages_one_session_in_reader_and_chronology(self):
        originals = [self.message(n, body="x" * 2000) for n in range(501)]
        self.login("editor")
        url = reverse(
            "phone:group_conversation",
            args=(self.case.pk, *sorted((self.person.pk, self.other.pk))),
        )
        self.assertContains(self.client.get(url), "Group 501 active messages")
        group = self.group_pair().sessions.get()
        page = instance_page(
            self.case, self.messages, instance_id=group.pk, message_page=11
        )
        self.assertEqual(page.paginator.count, 1)
        self.assertEqual(list(page[0]["messages"]), originals[-1:])
        self.assertNotIn("member_ids", page[0])
        attach_session_groups(self.case, [originals[0], originals[-1]])
        self.assertEqual(originals[0].session_marker, "Session starts")
        self.assertEqual(originals[-1].session_marker, "Session ends")
        start = originals[0].occurred_at
        with CaptureQueriesContext(connection) as reads:
            rows = list(daily_message_rows(self.case, start, start + timedelta(days=1)))
        # Whole-session validation must not repeat for every 50 originals.
        self.assertLessEqual(len(reads), 5)
        self.assertEqual([r["id"] for r in rows], [group.pk])
        self.assertIn("501 messages", rows[0]["detail"])
        attach_daily_message_previews(self.case, rows)
        self.assertFalse(rows[0]["overlap"])
        self.assertContains(
            self.client.get(page[0]["url"]), "Why this session starts here"
        )

    def test_saved_session_keeps_distinct_legacy_fragments_and_links(self):
        originals = [self.message(i) for i in range(4)]
        (fragment,) = self.publish_groups(originals[:2])
        (same,) = self.publish_groups(originals)
        self.publish_report(originals, [(1, [0, 1, 2, 3], "Whole exchange")])
        session = self.group_pair().sessions.get()
        page = instance_page(self.case, self.messages, instance_id=same.pk)
        self.assertEqual({r["id"] for r in page}, {session.pk, fragment.pk})
        full = next(r for r in page if r["id"] == session.pk)
        self.assertEqual(full["linked_id"], same.pk)
        self.assertEqual(list(full["messages"]), originals)
        self.assertEqual(full["summary"]["preview"], "Whole exchange")
        attach_session_groups(self.case, originals)
        self.assertEqual(originals[0].session_marker, "Multiple groupings")
        self.assertEqual(originals[-1].session_marker, "Session ends")
        start = originals[0].occurred_at
        rows = list(daily_message_rows(self.case, start, start + timedelta(days=1)))
        attach_daily_message_previews(self.case, rows)
        self.assertEqual({r["id"] for r in rows}, {session.pk, fragment.pk})
        self.assertTrue(all(r["overlap"] for r in rows))
        filtered = instance_page(self.case, self.messages, containing=originals[-1].pk)
        self.assertEqual([row["id"] for row in filtered], [session.pk])
        with self.assertRaises(Http404):
            instance_page(self.case, self.messages, containing=uuid4())
        TextMessageEvidence.objects.filter(pk=originals[-1].pk).update(sha256="c" * 64)
        self.assertFalse(
            any(
                r.get("id") == session.pk
                for r in instance_page(self.case, self.messages)
            )
        )

    def test_delayed_reply_details_link_to_both_originals_and_require_the_pair(self):
        question = self.message(0, body="Can you meet at six?")
        answer = TextMessageEvidence.objects.create(
            case=self.case,
            uploaded_by=self.case.owner,
            sender=self.other,
            recipient=self.person,
            title="Later answer",
            body="Yes, six works.",
            occurred_at=question.occurred_at + timedelta(minutes=121),
        )
        session = self.group_pair().sessions.get()
        args = (self.case.pk, self.person.pk, self.other.pk, session.pk)
        self.login("viewer")
        response = self.client.get(reverse("phone:grouping_details", args=args))
        self.assertContains(response, "121 minutes")
        self.assertContains(response, "appears to answer")
        for message in (question, answer):
            self.assertContains(
                response,
                reverse(
                    "core:evidence:detail",
                    args=(self.case.pk, "text-message", message.pk),
                ),
            )
        self.assertEqual(
            self.client.get(
                reverse(
                    "phone:grouping_details",
                    args=(self.case.pk, self.person.pk, self.third.pk, session.pk),
                )
            ).status_code,
            404,
        )

    def test_repeat_grouping_keeps_public_session_id_and_date_navigation(self):
        originals = [self.message(n * 65) for n in range(28)]
        old = self.group_pair().sessions.order_by("first_at")
        latest = self.group_pair().sessions.order_by("first_at")
        old_last, latest_last = old.last(), latest.last()
        page = instance_page(self.case, self.messages, instance_id=latest_last.pk)
        self.assertEqual(page.paginator.count, 28)
        self.assertEqual(page.number, 2)
        self.assertEqual(page[-1]["id"], old_last.pk)
        self.assertEqual(list(page[-1]["messages"]), originals[-1:])
        day = originals[-1].occurred_at.date()
        page = instance_page(self.case, self.messages, day=day)
        dated = next(row for row in page if row.get("day_message"))
        self.assertEqual(
            dated["day_message"],
            next(row.pk for row in originals if row.occurred_at.date() == day),
        )

    def test_long_summary_builds_citation_links_only_when_expanded(self):
        original = self.message(1)
        group = self.publish_groups([original])[0]
        self.publish_report(
            [original], [(1, [0], str(index) * 4000) for index in range(5)]
        )
        with patch(
            "phone.conversation_instances.finding_url", wraps=finding_url
        ) as url:
            collapsed = instance_page(self.case, self.messages)[0]["summary"]
            self.assertEqual(collapsed["statements"], [])
            url.assert_not_called()
            expanded = instance_page(
                self.case, self.messages, full_summary_id=str(group.pk)
            )[0]["summary"]
            self.assertEqual(expanded["preview"], collapsed["preview"])
            self.assertEqual(len(expanded["statements"]), 5)
            self.assertEqual(url.call_count, 5)

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
                (1, [0, 1], "Proposed a meeting place"),
                (1, [0, 1], "Asked if others could join"),
                (1, [2], "Confirmed the time"),
                (2, [3], "A different session"),
            ],
            gap_minutes=50,
        )
        saved = summary()
        self.assertEqual(
            saved["preview"], "Discussed the meeting Proposed a meeting place"
        )
        self.assertEqual(
            [item["text"] for item in saved["statements"]],
            [
                "Discussed the meeting",
                "Proposed a meeting place",
                "Asked if others could join",
                "Confirmed the time",
            ],
        )
        self.assertEqual(
            [item["url"] for item in saved["statements"]],
            [finding_url(self.case.pk, result.run_id, index) for index in range(4)],
        )
        self.assertIn(str(result.run_id), saved["report_url"])
        self.publish_report(originals, [], gap_minutes=50)
        self.assertIsNone(summary())

    def test_grouping_and_cited_summary_link_to_exact_current_instance(self):
        originals = [self.message(i) for i in range(3)]
        (old,) = self.publish_groups(originals[:2], gap_minutes=20)
        (current,) = self.publish_groups(originals[:2], gap_minutes=30)
        self.publish_groups(originals)
        report = self.publish_report(
            originals,
            [(1, [0], "First topic"), (1, [1], "Second topic"), (2, [2], "Later")],
        )
        url = reverse(
            "phone:conversation_detail",
            args=(self.case.pk, *sorted((self.person.pk, self.other.pk))),
            query={"tab": "instances", "instance": current.pk},
            fragment=f"instance-{current.pk}",
        )
        self.login("viewer")
        for result, index in ((old.result, 0), (report, 1)):
            with self.subTest(operation=result.run.operation):
                self.assertEqual(
                    result_instance_url(
                        self.case,
                        result.run,
                        index,
                        result.data["items"],
                        resolve_inputs(result.run, self.case),
                    ),
                    url,
                )
                detail = self.client.get(
                    finding_url(self.case.pk, result.run_id, index)
                )
                self.assertContains(detail, "View conversation session")
                self.assertContains(detail, f'href="{url.replace("&", "&amp;")}"')
        self.assertEqual(
            result_instance_url(
                self.case,
                report.run,
                2,
                report.data["items"],
                resolve_inputs(report.run, self.case),
            ),
            "",
        )
        self.assertContains(self.client.get(url), "First topic Second topic")

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

    def test_daily_messages_use_exact_instances_across_batches_and_midnight(self):
        earlier, first, separate, last = [self.message(i) for i in (-2, 1, 2, 3)]
        messages = [earlier, first, last]
        self.publish_groups(messages, gap_minutes=20)
        (current,) = self.publish_groups(messages, gap_minutes=30)
        (overlap,) = self.publish_groups([earlier, separate])
        self.publish_report(messages, [(1, [0, 1, 2], "The complete saved summary")])
        ungrouped = self.message(4, recipient=self.third)
        self.message(5, timed=False)
        start = datetime(2026, 1, 1, tzinfo=UTC)
        with patch("phone.daily_chronology.MESSAGES_PER_PAGE", 2):
            rows = list(daily_message_rows(self.case, start, start + timedelta(days=1)))
        attach_daily_message_previews(self.case, rows)
        self.assertEqual(
            [r["id"] for r in rows], [current.pk, overlap.pk, ungrouped.pk]
        )
        group, alternate, single = rows
        self.assertEqual(
            (group["at"], group["end_at"]), (first.occurred_at, last.occurred_at)
        )
        self.assertIn("2 of 3 messages on this day", group["detail"])
        self.assertEqual(group["preview"], "The complete saved summary")
        self.assertEqual(group["preview_label"], "AI summary · Whole session")
        self.assertTrue(group["overlap"])
        self.assertTrue(alternate["overlap"])
        self.assertEqual(alternate["preview"], separate.body)
        self.assertEqual(alternate["preview_label"], str(self.person))
        self.assertEqual(single["preview"], ungrouped.body)
        self.assertIn(f"message={ungrouped.pk}", single["url"])
        self.login("viewer")
        self.assertContains(
            self.client.get(group["url"]), f'id="instance-{current.pk}"'
        )

    def test_daily_stale_instance_leaves_all_its_dated_originals_visible(self):
        first, second = self.message(1), self.message(2)
        self.publish_groups([first, second])
        TextMessageEvidence.objects.filter(pk=second.pk).update(sha256="f" * 64)
        start = datetime(2026, 1, 1, tzinfo=UTC)
        rows = list(daily_message_rows(self.case, start, start + timedelta(days=1)))
        self.assertEqual([r["id"] for r in rows], [first.pk, second.pk])
        self.assertEqual([r["kind"] for r in rows], ["message", "message"])

    def test_archived_member_withholds_saved_session_and_legacy_group(self):
        early, archived, last = [self.message(i) for i in (1, 2, 3)]
        originals = [early, archived, last]
        (legacy,) = self.publish_groups(originals)
        session = self.group_pair().sessions.get()
        self.publish_report(originals, [(1, [0, 1, 2], "Saved group summary")])
        TextMessageEvidence.objects.filter(pk=archived.pk).update(
            is_archived=True,
            archive_changed_at=archived.created_at,
            archive_changed_by=self.case.owner,
        )
        self.assertEqual(instance_page(self.case, self.messages).paginator.count, 0)
        for instance in (legacy, session):
            with self.assertRaises(Http404):
                instance_page(self.case, self.messages, instance_id=instance.pk)
        attach_session_groups(self.case, [early, last])
        self.assertFalse(early.session_groups)
        self.assertFalse(last.session_groups)
        start = datetime(2026, 1, 1, tzinfo=UTC)
        rows = list(daily_message_rows(self.case, start, start + timedelta(days=1)))
        self.assertEqual([row["id"] for row in rows], [early.pk, last.pk])
        self.assertEqual([row["kind"] for row in rows], ["message", "message"])
