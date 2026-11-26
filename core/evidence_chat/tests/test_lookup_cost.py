"""Bound ordinary chat model input as unrelated active case evidence grows."""

import json
from hashlib import sha256
from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase, override_settings

from core.analysis.execution import configured_external_executions
from core.analysis.testing import approve_case_execution
from core.analysis.tests.test_gateway import response
from core.analysis.tests.test_openai_gateway import OPENAI, openai_reply
from core.evidence_chat import corpus, jobs, services
from core.evidence_chat.tests.test_engine import decision
from core.models import AnalysisRun
from core.testing import EvidenceViewTestMixin
from text.models import TextEvidence


@override_settings(PANDECT_AI_GATEWAY=OPENAI)
class ChatLookupCostTests(EvidenceViewTestMixin, TestCase):
    def test_irrelevant_case_growth_does_not_expand_model_input(self):
        owner = self.users["owner"]
        self.case.ai_backend = "gateway"
        self.case.save(update_fields=["ai_backend"])
        approve_case_execution(
            self.case, owner, configured_external_executions(case=self.case)
        )
        statement = "The saffron permit allows access on Fridays."
        target = TextEvidence.objects.create(
            case=self.case, uploaded_by=owner, title="Saffron permit", body=statement
        )
        question = "When does the saffron permit allow access?"
        requests = []

        def reply(request, **kwargs):
            requests.append(request.data)
            prompt = json.loads(json.loads(request.data)["input"][1]["content"])
            records = json.loads(prompt["task"].split("\n", 1)[1])["records"]
            self.assertEqual(len(records), 1)
            record = records[0]
            claim = {
                "text": statement,
                "citations": [
                    {
                        "id": record["id"],
                        "quote": statement,
                        "start": record["text"].index(statement),
                    }
                ],
            }
            result = openai_reply(decision("answer", claims=[claim]))
            result.pop("usage")  # A mocked transport has no measured token usage.
            return response(result)

        def inventory_record(number):
            body = f"Inventory carton {number} contains brass fittings."
            return TextEvidence(
                case=self.case,
                uploaded_by=owner,
                title=f"Inventory {number}",
                body=body,
                sha256=sha256(body.encode()).hexdigest(),
            )

        for lower, upper in ((0, 1_000), (1_000, 10_000)):
            with self.subTest(irrelevant_records=upper):
                TextEvidence.objects.bulk_create(
                    [inventory_record(i) for i in range(lower, upper)], batch_size=500
                )
                turn = services.submit_turn(
                    owner, self.case.pk, question, submission_id=uuid4()
                )
                with (
                    patch("core.analysis.gateway.build_opener") as transport,
                    patch.object(
                        corpus,
                        "discover",
                        side_effect=AssertionError("unexpected full review"),
                    ),
                ):
                    transport.return_value.open.side_effect = reply
                    jobs.process_turn(turn.pk, turn.delivery_token)
                    transport.return_value.open.assert_called_once()
                turn.refresh_from_db()
                self.assertEqual((turn.status, turn.mode), ("complete", "ordinary"))
                self.assertEqual(turn.sources.get().evidence_id, target.pk)
                self.assertEqual(
                    services.inspect_turn(turn)["claims"][0]["citations"][0]["quote"],
                    statement,
                )
                self.assertFalse(AnalysisRun.objects.filter(case=self.case).exists())

        self.assertEqual(
            requests[0], requests[1], "Unrelated records changed the provider input"
        )
