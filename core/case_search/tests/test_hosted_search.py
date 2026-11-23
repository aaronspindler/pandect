"""Paid query embedding requires an explicit, approved, case-scoped submission."""

import hashlib
import math
from unittest.mock import patch

from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.case_search.approval import search_approval_context
from core.case_search.semantic_model import HOSTED_DIMENSIONS, profile
from core.models import CaseSearchIndex, SearchDocument, SearchPassage
from core.testing import EvidenceViewTestMixin
from text.models import TextEvidence


@override_settings(
    PANDECT_SEARCH_EMBEDDING_PROVIDER="openai-gateway",
    PANDECT_AI_GATEWAY={
        "account": "a" * 32,
        "gateway": "synthetic",
        "gateway_token": "synthetic",
        "openai_key_alias": "default",
    },
)
class HostedSearchTests(EvidenceViewTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("core:search:results", args=[self.case.pk])
        self.params = {"q": "departing by sea", "type": "text"}
        self.approval_url = reverse("cases:case_search_approval", args=[self.case.pk])
        self.login("owner")
        self.change_approval("approve")
        self.client = Client(enforce_csrf_checks=True)
        self.login("viewer")

    def change_approval(self, action):
        self.case.refresh_from_db()
        review = search_approval_context(self.case, self.users["owner"])
        owner = Client()
        owner.force_login(self.users["owner"])
        response = owner.post(
            self.approval_url,
            {"confirmation": review[f"{action}_confirmation"], "action": action},
        )
        self.assertEqual(response.status_code, 302)
        self.case.refresh_from_db()

    def passage(self, title, similarity, width=HOSTED_DIMENSIONS, archived=False):
        item = TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title=title,
            body="The ferry moved out of port.",
            is_archived=archived,
            archive_changed_at=timezone.now() if archived else None,
            archive_changed_by=self.case.owner if archived else None,
        )
        doc, _ = SearchDocument.objects.update_or_create(
            case=self.case, text=item, defaults={"complete": True, "profile": profile()}
        )
        SearchPassage.objects.create(
            case=self.case,
            document=doc,
            start=0,
            profile=doc.profile,
            length=len(item.body),
            digest=hashlib.sha256(item.body.encode()).hexdigest(),
            embedding=[similarity, math.sqrt(1 - similarity**2)] + [0.0] * (width - 2),
        )
        return item

    def submit(self, **params):
        token = self.client.cookies["csrftoken"].value
        return self.client.post(
            self.url,
            {"meaning": "1", "csrfmiddlewaretoken": token},
            query_params=self.params | params,
        )

    def test_explicit_submission_threshold_width_state_and_csrf(self):
        active = self.passage("Harbour log", 0.489)
        self.passage("Weaker match", 0.488)
        self.passage("Other width", 1.0, width=384)
        archived = self.passage("Earlier log", 0.9, archived=True)
        CaseSearchIndex.objects.filter(case=self.case).update(status="ready")
        with patch(
            "core.case_search.semantic.embed", return_value=[[1.0] + [0.0] * 1535]
        ) as embed:
            response = self.client.get(self.url, self.params | {"meaning": "1"})
            self.assertContains(response, "Search by meaning")
            self.client.head(self.url, self.params | {"meaning": "1"})
            self.client.get(self.url, self.params | {"page": 2, "meaning": "1"})
            self.client.get(
                reverse("core:search:suggestions", args=[self.case.pk]), self.params
            )
            self.assertEqual(
                self.client.post(
                    self.url, {"meaning": "1"}, query_params=self.params
                ).status_code,
                403,
            )
            embed.assert_not_called()
            for state, expected in (
                ("active", [active.pk]),
                ("archived", [archived.pk]),
                ("all", [archived.pk, active.pk]),
            ):
                response = self.submit(state=state)
                self.assertEqual(
                    [r["id"] for r in response.context["related"]], expected
                )
                self.assertContains(response, "The ferry moved out of port.")
            self.assertEqual(embed.call_count, 3)
            self.change_approval("revoke")
            self.assertNotContains(
                self.client.get(self.url, self.params), "Search by meaning"
            )
            self.assertFalse(self.submit().context["related"])
            self.assertEqual(embed.call_count, 3)

    def test_reauthorization_in_flight_withholds_old_revision(self):
        self.passage("Harbour log", 0.9)
        self.client.get(self.url, self.params)

        def replace_grant(texts, authorize, **kwargs):
            authorize()
            self.change_approval("revoke")
            self.change_approval("approve")
            return [[1.0] + [0.0] * 1535]

        with patch(
            "core.case_search.semantic.embed", side_effect=replace_grant
        ) as embed:
            self.assertFalse(self.submit().context["related"])
            embed.assert_called_once()
