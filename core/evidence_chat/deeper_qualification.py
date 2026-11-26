"""Measure approval and resumable review against the opt-in dense case fixture."""

import json
from time import monotonic
from unittest.mock import patch

from django.urls import reverse

from core.analysis import chat_usage
from core.evidence_chat import ai, deep_review, engine, jobs, services
from core.models import ChatTurn


def qualify_deeper_review(test, ordinary, typed, selection, count):
    """Exercise the real confirmation page and worker with two metered calls."""
    test.login("owner")
    test.assertTrue(ordinary.plan.get("message_topics"))
    test.assertTrue(selection.plan["recorded_selection"]["snapshot"])
    test.assertTrue(typed.plan["recorded_selection"]["context_only"])
    test.assertEqual(typed.plan["recorded_selection"]["membership"]["count"], 100_000)
    metrics = {}
    original_answer = ordinary.answer

    def classify(turn, authorize, prompt, schema):
        test.assertIs(schema, engine.REVIEW)
        authorize()
        chat_usage.reserve(turn, "answer", len(prompt), output_limit=1024)
        records = json.loads(prompt.split("\n", 1)[1])["records"]
        return {
            "records": [
                {
                    "id": row["id"],
                    "outcome": "match",
                    "citations": [
                        {
                            "id": row["id"],
                            "quote": "Cobalt",
                            "start": row["text"].index("Cobalt"),
                        }
                    ],
                }
                for row in records
            ]
        }

    with patch.object(ai, "infer", side_effect=classify) as model:
        for parent, route in (
            (selection, "selection"),
            (typed, "typed"),
            (ordinary, "ordinary"),
        ):
            url = reverse(
                "core:chat:deep_review", args=(test.case.pk, parent.chat_id, parent.pk)
            )
            start = monotonic()
            page = test.client.get(url)
            test.assertContains(page, f"{count} candidate source entries")
            test.assertEqual(page.context["expected"], 5001)
            metrics[f"{route}_estimate_seconds"] = round(monotonic() - start, 3)
            test.assertEqual(page.context["scope"]["count"], count)
        model.assert_not_called()
        start = monotonic()
        approved = test.client.post(
            url,
            {
                "action": "approve",
                "confirmation": page.context["form"].initial["confirmation"],
                "question": ordinary.question,
                "request_limit": 2,
            },
        )
        test.assertEqual(approved.status_code, 302)
        child = ChatTurn.objects.get(review_of=ordinary)
        jobs.process_turn(child.pk, child.delivery_token)
        child.refresh_from_db()
        metrics["approve_and_freeze_seconds"] = round(monotonic() - start, 3)
        test.assertEqual((child.total, child.plan["stage"]), (count, "review"))
        model.assert_not_called()
        jobs.process_turn(child.pk, child.delivery_token)
        child.refresh_from_db()
        test.assertEqual(child.reviewed, 4)
        start = monotonic()
        old = child.delivery_token
        services.recover_interrupted_delivery(child.pk, old)
        jobs.process_turn(child.pk, old)
        child.refresh_from_db()
        test.assertEqual((child.reviewed, model.call_count), (4, 1))
        for _ in range(3):
            jobs.process_turn(child.pk, child.delivery_token)
            child.refresh_from_db()
        test.assertEqual(child.status, "complete", child.error_code)
        test.assertEqual(
            (child.reviewed, child.gaps, model.call_count), (8, count - 8, 2)
        )
        test.assertEqual(child.review_requests, 2)
        test.assertEqual(child.hits.count(), 8)
        test.assertIsNotNone(services.inspect_turn(child))
        scope, remaining_requests, _ = deep_review.estimate(child, test.users["owner"])
        test.assertTrue(scope["resume"])
        test.assertEqual((scope["count"], remaining_requests), (count - 8, 4999))
        metrics["recover_publish_and_resume_estimate_seconds"] = round(
            monotonic() - start, 3
        )
        ordinary.refresh_from_db()
        test.assertEqual(ordinary.answer, original_answer)
        metrics.update(
            selected=count,
            estimated_requests=5001,
            mock_generations=model.call_count,
            reviewed=child.reviewed,
            remaining=scope["count"],
        )
    print("DENSE_CHAT_DEEP_REVIEW", json.dumps(metrics), flush=True)
