"""Opt-in, synthetic Evidence Chat qualification against a pinned local Ollama."""

import json
import os
import re
from collections import Counter
from hashlib import sha256
from time import monotonic
from unittest import skipUnless
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from config.settings import PANDECT_LOCAL_AI
from core.analysis.inference_payloads import encode_json
from core.analysis.local import LocalClient
from core.evidence_chat import jobs, services
from core.models import AnalysisRun
from core.testing import EvidenceViewTestMixin
from phone.models import TextMessageEvidence
from text.models import TextEvidence

ARRIVAL_TIMES = [(1, "08:10", r"\b(?:clerk|register)\b"), (2, "09:25", r"\bwitness\b")]
PARCEL_TIMES = [
    (0, "13:12", r"\bseal(?:ed|ing)?\b"),
    (1, "13:47", r"\b(?:left|leaving|departure|departed|exited|exit)\b"),
]
CLOCK = re.compile(r"\b(\d{1,2}):(\d{2})\b")


def claim_supports(text, value, subject):
    if not subject:
        return value.lstrip("0").removesuffix("s").casefold() in text.casefold()
    # In these fixtures a quoted time must be attached to its own source or
    # event. A compound claim can cite both originals while swapping the times.
    clauses = re.split(
        r"[;.!?\n]|\b(?:and|but|while|whereas|then|versus)\b", text, flags=re.I
    )
    for clause in clauses:
        parts = clause.split(",") if len(CLOCK.findall(clause)) > 1 else [clause]
        for part in parts:
            times = {f"{int(h):02d}:{m}" for h, m in CLOCK.findall(part)}
            if times == {value} and re.search(subject, part, re.I):
                return True
    return False


def database_counters():
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT sum(seq_tup_read + idx_tup_fetch) FROM pg_stat_xact_user_tables"
        )
        rows = cursor.fetchone()[0]
        cursor.execute(
            "SELECT sum(pg_stat_get_xact_blocks_hit(oid)), "
            "sum(pg_stat_get_xact_blocks_fetched(oid) "
            "- pg_stat_get_xact_blocks_hit(oid)) "
            "FROM pg_class WHERE relnamespace = 'public'::regnamespace "
            "AND relkind IN ('r', 'i')"
        )
        hits, reads = cursor.fetchone()
    return {
        "rows_read": int(rows or 0),
        "buffer_hits": int(hits or 0),
        "buffer_reads": int(reads or 0),
    }


@skipUnless(
    os.getenv("PANDECT_QUALIFY_CHAT") == "1", "Explicit local qualification only"
)
class LocalChatQualification(EvidenceViewTestMixin, TestCase):
    """Run explicitly on each revision with identical model and runtime settings."""

    def test_grounded_answers_and_scaling(self):
        owner = self.users["owner"]
        records = [
            ("Cobalt permit", "The cobalt permit is valid on Thursdays."),
            ("Juniper register", "The Juniper clerk recorded arrival at 08:10."),
            ("Juniper witness", "The Juniper witness reported arrival at 09:25."),
        ]
        items = [
            TextEvidence.objects.create(
                pk=UUID(int=i + 100),
                case=self.case,
                uploaded_by=owner,
                title=title,
                body=body,
            )
            for i, (title, body) in enumerate(records)
        ]
        cases = [
            ("record", f"Show me record {items[0].pk}", [(0, "Thursdays", "")]),
            (
                "lookup",
                "Which weekday does the cobalt permit specify?",
                [(0, "Thursdays", "")],
            ),
            (
                "conflict",
                "Compare both Juniper accounts. What arrival times do they report?",
                ARRIVAL_TIMES,
            ),
        ]
        self.run_workload(items, cases)

    def test_cross_source_chronology_and_missing_attribution(self):
        owner = self.users["owner"]
        items = [
            TextMessageEvidence.objects.create(
                pk=UUID(int=200),
                case=self.case,
                uploaded_by=owner,
                sender=self.person,
                recipient=self.person,
                title="Packing message",
                body="The Larch parcel was sealed at 13:12.",
            ),
            TextEvidence.objects.create(
                pk=UUID(int=201),
                case=self.case,
                uploaded_by=owner,
                title="Gate register",
                body="At 13:47 the Larch parcel left the gate.",
            ),
            TextEvidence.objects.create(
                pk=UUID(int=202),
                case=self.case,
                uploaded_by=owner,
                title="Larch sign-off",
                body="The inspector field on the Larch sign-off is blank.",
            ),
        ]
        self.run_workload(
            items,
            [
                (
                    "chronology",
                    "When was the Larch parcel sealed and when did it leave the gate?",
                    PARCEL_TIMES,
                ),
                (
                    "missing attribution",
                    "What does the Larch sign-off record in the inspector field?",
                    [(2, "blank", "")],
                ),
            ],
        )

    def run_workload(self, items, cases):
        owner = self.users["owner"]
        self.assertIn(
            urlsplit(PANDECT_LOCAL_AI["text"]["url"]).hostname,
            {"localhost", "127.0.0.1", "::1"},
        )
        original_request = LocalClient._request
        results = {}

        def dispatch(client, path, payload=None, **kw):
            response = original_request(client, path, payload, **kw)
            if path == "/api/chat":
                usage["bytes"] += len(encode_json(payload))
                if not payload.get("_debug_render_only"):
                    usage["calls"] += 1
                    usage["input"] += response["prompt_eval_count"]
                    usage["output"] += response["eval_count"]
            return response

        with (
            self.settings(PANDECT_LOCAL_AI=PANDECT_LOCAL_AI),
            patch.object(LocalClient, "_request", dispatch),
        ):
            for lower, upper in ((0, 1000), (1000, 10000)):
                rows = []
                for number in range(lower, upper):
                    body = f"Inventory carton {number} contains brass fittings."
                    rows.append(
                        TextEvidence(
                            case=self.case,
                            uploaded_by=owner,
                            title="Hardware",
                            body=body,
                            sha256=sha256(body.encode()).hexdigest(),
                        )
                    )
                TextEvidence.objects.bulk_create(rows, batch_size=500)
                # The entire workload runs inside one test transaction, invisible
                # to autovacuum. Supply the planner with the new row distribution.
                with connection.cursor() as cursor:
                    cursor.execute("ANALYZE text_textevidence")
                for name, question, expected in cases:
                    usage = Counter()
                    before = database_counters()
                    start = monotonic()
                    with CaptureQueriesContext(connection) as queries:
                        turn = services.submit_turn(
                            owner, self.case.pk, question, submission_id=uuid4()
                        )
                        for _ in range(4):
                            jobs.process_turn(turn.pk, turn.delivery_token)
                            turn.refresh_from_db()
                            if turn.status not in ("queued", "running"):
                                break
                        answer = services.inspect_turn(turn) or {}
                    claims = answer.get("claims", ())
                    grounded = turn.status == "complete" and all(
                        any(
                            str(items[i].pk) in ref["url"]
                            and word in ref["quote"]
                            and (
                                name == "record"
                                or claim_supports(claim["text"], word, subject)
                            )
                            for claim in claims
                            for ref in claim["citations"]
                        )
                        for i, word, subject in expected
                    )
                    result = {
                        "case": name,
                        "distractors": upper,
                        "grounded": grounded,
                        "claims": [claim["text"] for claim in claims],
                        "error": turn.error_code,
                        "calls": usage["calls"],
                        "input": usage["input"],
                        "output": usage["output"],
                        "bytes": usage["bytes"],
                        "queries": len(queries),
                        "sources": turn.sources.count(),
                        "candidate_reads": turn.plan.get("candidate_reads"),
                        "seconds": round(monotonic() - start, 2),
                        **{
                            key: value - before[key]
                            for key, value in database_counters().items()
                        },
                    }
                    print("CHAT_QUALIFICATION", json.dumps(result), flush=True)
                    results[name, upper] = result
            self.assertFalse(AnalysisRun.objects.filter(case=self.case).exists())
        for name, _, _ in cases:
            small, large = (results[name, n] for n in (1000, 10000))
            with self.subTest(question=name):
                self.assertTrue(small["grounded"] and large["grounded"])
                self.assertEqual(large["calls"], small["calls"])
                self.assertLessEqual(
                    large["input"] + large["output"],
                    1.05 * (small["input"] + small["output"]),
                )
