"""Opt-in database and private-model qualification using synthetic records only.

Run through Django's test runner as documented in docs/validation.md. Repeated
filler messages have authentic, reused embeddings to separate corpus-size costs
from encoder throughput. The small corpus uses the real indexing worker.
"""

import hashlib
import json
import math
import os
from statistics import median
from time import perf_counter

from allauth.account.models import EmailAddress
from django.db import connection
from django.test import TestCase
from django.urls import reverse

from core.case_search.indexing import index_case_passages, prepare_case_search
from core.case_search.queries import search, suggest
from core.case_search.semantic import related_results
from core.case_search.semantic_model import profile
from core.models import CaseSearchIndex, SearchPassage
from core.testing import EvidenceTestMixin
from phone.models import TextMessageEvidence

QUERIES = (
    ("called off the meeting", "The appointment was cancelled yesterday."),
    ("payment still owed", "The outstanding invoice remains unpaid."),
    ("the car broke down", "Her sedan stalled beside the highway."),
    ("someone lost their keys", "I cannot find the keys to my apartment."),
    ("where the vehicle was left", "The driver parked the van outside a warehouse."),
    (
        "did they refuse entry",
        "The guard would not allow the visitor through the gate.",
    ),
)
DISTRACTORS = (
    "A parcel was delivered to reception.",
    "Dinner is in the oven.",
    "The case file lists an oak desk.",
    "Remind me to bring the scanner tomorrow.",
    "The appointment went ahead as planned.",
    "Car repairs were scheduled for next week.",
)
UNRELATED = (
    "penguin feathers in Antarctica",
    "wavelength of ultraviolet radiation",
    "how to propagate orchids from cuttings",
)


class SearchQualification(EvidenceTestMixin, TestCase):
    def test_private_model_and_large_export(self):
        config = {
            key: os.environ.get("PANDECT_LOCAL_EMBEDDING_" + key.upper(), "")
            for key in ("url", "model", "sha256", "runtime")
        }
        self.assertTrue(all(config.values()), "Set the four private embedding pins.")
        with self.settings(PANDECT_LOCAL_EMBEDDING=config):
            self.qualify()

    def qualify(self):
        items = []
        EmailAddress.objects.create(
            user=self.owner, email="synthetic@example.test", verified=True
        )
        for i, body in enumerate([text for _, text in QUERIES] + list(DISTRACTORS)):
            items.append(
                self.create_evidence(
                    TextMessageEvidence,
                    body=body,
                    title=f"Synthetic excerpt {i}",
                    sender=self.person,
                    recipient=self.person,
                )
            )
        started = perf_counter()
        prepare_case_search(self.case, self.owner)
        index = CaseSearchIndex.objects.get(case=self.case)
        for _ in range(30):
            index.refresh_from_db()
            if index.status != "queued":
                break
            index_case_passages.call(str(index.pk), str(index.token))
        self.assertEqual(index.status, "ready", index.error_code)
        self.assertEqual(
            SearchPassage.objects.filter(case=self.case).count(), len(items)
        )
        small_backfill = perf_counter() - started
        lexical_found = related_found = 0
        for (query, _), target in zip(QUERIES, items, strict=False):
            lexical_found += (
                target.pk in [row["id"] for row in search(self.case, query=query)][:5]
            )
            related, _status = related_results(self.case, user=self.owner, query=query)
            related_found += target.pk in [row["id"] for row in related[:5]]
            for row in related:
                self.assertIn(row["excerpt"], [item.body for item in items])
        for query in UNRELATED:
            matches, _ = related_results(self.case, user=self.owner, query=query)
            self.assertEqual(matches, [])
        self.assertGreater(related_found, lexical_found)
        print(
            f"Top-five expected passages: keywords {lexical_found}/{len(QUERIES)}, "
            f"meaning {related_found}/{len(QUERIES)}. "
            f"Unrelated hits 0/{len(UNRELATED)}. "
            f"Worker backfill, {len(items)} passages: {small_backfill:.3f}s."
        )

        target_count = int(os.environ.get("PANDECT_QUALIFY_PASSAGES", "250000"))
        self.assertGreaterEqual(target_count, len(items))
        started = perf_counter()
        self.populate(target_count - len(items), items[len(QUERIES)])
        seeded_time = perf_counter() - started
        self.assertEqual(
            SearchPassage.objects.filter(case=self.case).count(), target_count
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_total_relation_size('core_searchpassage') + "
                "pg_total_relation_size('core_searchdocument')"
            )
            size = cursor.fetchone()[0]
            cursor.execute(
                "SELECT COALESCE(SUM(pg_relation_size(oid)),0) FROM pg_class "
                "WHERE relkind='i' AND left(relname,5)='srch_'"
            )
            lexical_indexes = int(cursor.fetchone()[0])
            cursor.execute(
                "SELECT SUM(pg_column_size(search_words)) "
                "FROM phone_textmessageevidence"
            )
            lexical_words = int(cursor.fetchone()[0])
        unique = str(items[0].pk)
        self.client.force_login(self.owner)
        url = reverse("core:search:results", args=[self.case.pk])

        def keyword():
            self.assertEqual(search(self.case, query=unique)[0]["id"], items[0].pk)

        def suggestions():
            rows, _ = suggest(self.case, "Synthetic excerpt", spelling=False)
            self.assertTrue(rows)

        def spelling():
            rows, correction = suggest(self.case, "Synthetc excerp")
            self.assertEqual(rows, [])
            self.assertTrue(correction.startswith("Synthetic excerpt"))

        def selective_keywords():
            self.assertEqual(
                search(self.case, query="guard visitor")[0]["id"], items[5].pk
            )

        def common_keywords():
            page = search(self.case, query="parcel reception")
            self.assertEqual(page.paginator.count, target_count - len(items) + 1)

        def meaning():
            response = self.client.post(
                url, {"meaning": "1"}, query_params={"q": QUERIES[0][0]}
            )
            self.assertEqual(response.status_code, 200)
            self.assertIn(
                items[0].pk, [row["id"] for row in response.context["related"]]
            )

        timings = {}
        for name, operation in (
            ("suggestions", suggestions),
            ("spelling", spelling),
            ("keyword", keyword),
            ("selective_keywords", selective_keywords),
            ("common_keywords", common_keywords),
            ("hybrid_results_page", meaning),
        ):
            samples = []
            for _ in range(21):
                start = perf_counter()
                operation()
                samples.append(round((perf_counter() - start) * 1000, 2))
            timings[name] = {
                "first_ms": samples[0],
                "warm_median_ms": median(samples[1:]),
                "warm_p95_ms": sorted(samples[1:])[math.ceil(20 * 0.95) - 1],
            }
            print(f"{name}: {timings[name]}", flush=True)
        print(
            json.dumps(
                {
                    "passages": target_count,
                    "semantic_tables_bytes": size,
                    "lexical_indexes_bytes": lexical_indexes,
                    "message_search_column_bytes": lexical_words,
                    "scale_fixture_seconds": round(seeded_time, 3),
                    "retrieval": timings,
                },
                sort_keys=True,
            )
        )

    def populate(self, extra, filler):
        # Reuse identical source bytes and vector only for identical filler text.
        # Unique message/record IDs and titles exercise real joins, filters and indexes.
        columns = [
            connection.ops.quote_name(f.column)
            for f in TextMessageEvidence._meta.concrete_fields
            if not f.generated
        ]
        target_columns = ", ".join(columns)
        replica_columns = ", ".join(f"replica.{column}" for column in columns)
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO phone_textmessageevidence ({target_columns})
                SELECT {replica_columns}
                FROM phone_textmessageevidence template
                CROSS JOIN generate_series(1,%s) n
                CROSS JOIN LATERAL jsonb_populate_record(template, jsonb_build_object(
                    'id', gen_random_uuid(), 'title', 'Dispatch-' || n::text)) replica
                WHERE template.id=%s
            """,
                [extra, filler.pk],
            )
            cursor.execute(
                """
                INSERT INTO core_searchdocument
                    (id,case_id,message_id,profile,"offset",complete)
                SELECT gen_random_uuid(),case_id,id,%s,length(body),true
                FROM phone_textmessageevidence
                WHERE case_id=%s AND title LIKE 'Dispatch-%%'
            """,
                [profile(), self.case.pk],
            )
            digest = hashlib.sha256(filler.body.encode()).hexdigest()
            cursor.execute(
                """
                INSERT INTO core_searchpassage
                    (id,case_id,document_id,profile,start,length,digest,embedding)
                SELECT gen_random_uuid(),d.case_id,d.id,d.profile,0,%s,%s,
                       prototype.embedding
                FROM core_searchdocument d
                JOIN phone_textmessageevidence m ON m.id=d.message_id
                CROSS JOIN (SELECT embedding FROM core_searchpassage
                            WHERE digest=%s LIMIT 1) prototype
                WHERE d.case_id=%s AND m.title LIKE 'Dispatch-%%'
            """,
                [len(filler.body), digest, digest, self.case.pk],
            )
            for relation in (
                "phone_textmessageevidence",
                "core_searchdocument",
                "core_searchpassage",
            ):
                cursor.execute(f"ANALYZE {relation}")
