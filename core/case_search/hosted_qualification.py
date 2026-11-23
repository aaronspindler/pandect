"""Opt-in live qualification of hosted meaning search on synthetic case records."""

import math
from time import monotonic

from django.db.models import Count, Sum
from django.test import TestCase
from django.urls import reverse

from config.settings import PANDECT_AI_GATEWAY
from core.case_search.approval import search_approval_context
from core.case_search.indexing import index_case_passages
from core.models import CaseSearchIndex, GatewayRequest, SearchPassage
from core.testing import EvidenceViewTestMixin
from text.models import TextEvidence


class HostedQualification(EvidenceViewTestMixin, TestCase):
    def test_synthetic_queries_through_the_gateway(self):
        documents = {
            "pump": (
                "The sump motor lost power overnight. The cellar filled with water."
            ),
            "ticket": (
                "The inspector found that the traveller had boarded the train "
                "without paying a fare."
            ),
            "storm": "Hail shattered three glass panes in the greenhouse.",
            "keys": "A spare brass key was tucked in an envelope beneath the mat.",
            "order": "Order a replacement motor for the upstairs ventilation fan.",
        }
        queries = [
            ("why did the basement flood", ["pump"]),
            ("who rode the railway without a ticket", ["ticket"]),
            ("weather damage to windows in the glasshouse", ["storm"]),
            ("volcanic ash chemistry near a crater", []),
            ("who won the international chess final", []),
        ]
        with self.settings(
            PANDECT_SEARCH_EMBEDDING_PROVIDER="openai-gateway",
            PANDECT_AI_GATEWAY=PANDECT_AI_GATEWAY,
        ):
            metrics = self.evaluate(self.case, documents, queries)
        print("Hosted search qualification", metrics, flush=True)
        self.assertGreaterEqual(metrics["found"], 2)
        self.assertEqual(metrics["unsupported_with_results"], 0)

    def evaluate(self, case, documents, queries):
        self.login("owner")
        ids = {}
        for key, body in documents.items():
            item = TextEvidence.objects.create(
                case=case,
                uploaded_by=case.owner,
                title=f"Synthetic record {key}",
                body=body,
            )
            ids[item.pk] = key
        review = search_approval_context(case, case.owner)
        self.assertTrue(
            review["profile"],
            "Configure a Cloudflare gateway and its OpenAI key alias.",
        )
        approval = self.client.post(
            reverse("cases:case_search_approval", args=[case.pk]),
            {"action": "approve", "confirmation": review["approve_confirmation"]},
        )
        self.assertEqual(approval.status_code, 302)
        index = CaseSearchIndex.objects.get(case=case)
        started = monotonic()
        for _ in range(1000):
            if index.status != "queued":
                break
            index_case_passages.call(str(index.pk), str(index.token))
            index.refresh_from_db()
        self.assertEqual(index.status, "ready", index.error_code)
        index_seconds = monotonic() - started
        self.login("viewer")
        route = reverse("core:search:results", args=[case.pk])
        counts = dict(
            supported=0,
            found=0,
            related_found=0,
            unsupported=0,
            unsupported_with_results=0,
            unsupported_with_combined_results=0,
        )
        query_seconds = []
        for query, expected in queries:
            started = monotonic()
            response = self.client.post(
                route, {"meaning": "1"}, query_params={"q": query}
            )
            self.assertEqual(response.status_code, 200)
            query_seconds.append(monotonic() - started)
            self.assertTrue(response.context["meaning_requested"])
            self.assertFalse(
                response.context["meaning_status"], response.context["meaning_status"]
            )
            related = response.context["related"]
            self.assertLessEqual(len(related), 6)
            matching = {ids[row["id"]] for row in related}
            combined = matching.union(
                ids.get(row["id"]) for row in response.context["page_obj"]
            )
            for row in related:
                self.assertIn(row["excerpt"], documents[ids[row["id"]]])
                self.assertEqual(
                    row["url"],
                    reverse("core:evidence:detail", args=[case.pk, "text", row["id"]]),
                )
            if expected:
                counts["supported"] += 1
                counts["related_found"] += bool(matching.intersection(expected))
                counts["found"] += bool(combined.intersection(expected))
            else:
                counts["unsupported"] += 1
                counts["unsupported_with_results"] += bool(matching)
                counts["unsupported_with_combined_results"] += bool(combined)
        counts["related_misses"] = counts["supported"] - counts["related_found"]
        counts["combined_misses"] = counts["supported"] - counts["found"]
        counts["documents"] = len(documents)
        counts["passages"] = SearchPassage.objects.filter(document__case=case).count()
        counts["index_seconds"] = round(index_seconds, 3)
        query_seconds.sort()
        for name, proportion in (("p50", 0.50), ("p95", 0.95)):
            counts[f"query_{name}_seconds"] = round(
                query_seconds[math.ceil(proportion * len(queries)) - 1], 3
            )
        counts["usage"] = list(
            GatewayRequest.objects.filter(case=case, status="complete")
            .order_by("operation")
            .values("operation")
            .annotate(requests=Count("pk"), tokens=Sum("input_tokens"))
        )
        self.assertEqual(
            GatewayRequest.objects.filter(
                case=case, operation="search_query", status="complete"
            ).count(),
            len(queries),
        )
        return counts
