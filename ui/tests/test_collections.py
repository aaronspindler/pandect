from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from cases.models import CaseMembership
from core.testing import EvidenceViewTestMixin
from phone.models import TextMessageEvidence


class CollectionFragmentTests(EvidenceViewTestMixin, TestCase):
    def test_collection_fragments_share_authorized_filters_and_pagination(self):
        source = self.text()
        routes = (
            (reverse("cases:case_list"), {}, "cases"),
            (reverse("people:list", args=[self.case.pk]), {}, "people"),
            (reverse("vehicle:list", args=[self.case.pk]), {}, "vehicles"),
            (reverse("core:analysis:browse", args=[self.case.pk]), {}, "findings"),
            (self.url("list"), {"view": "gallery", "type": "image"}, "images"),
            (self.url("list"), {"type": "text"}, "evidence"),
            (self.url("detail", source), {"type": "text"}, "evidence"),
        )
        self.login("viewer")
        for url, params, kind in routes:
            with self.subTest(kind=kind, url=url):
                params = params | {"q": "Original", "page": 1}
                page = self.client.get(url, params)
                fragment = self.client.get(
                    url, params, HTTP_X_PANDECT_LIVE="collection"
                )
                self.assertContains(fragment, f'data-collection-kind="{kind}"')
                self.assertNotContains(fragment, "<!doctype html>")
                self.assertNotContains(fragment, 'id="evidence-reader"')
                self.assertIn("X-Pandect-Live", fragment["Vary"])
                self.assertEqual(fragment["X-Pandect-Live"], "collection")
                # Evidence places its summary above the full-page filter toolbar.
                overview, records_start, records = fragment.content.partition(
                    b'<div data-collection-kind="evidence"'
                )
                if records_start and overview.strip():
                    self.assertIn(overview.strip(), page.content)
                    self.assertIn((records_start + records).strip(), page.content)
                else:
                    self.assertIn(fragment.content.strip(), page.content)
        CaseMembership.objects.filter(
            case=self.case, user=self.users["viewer"]
        ).delete()
        for url, params, kind in routes[1:]:
            with self.subTest(revoked=kind):
                response = self.client.get(
                    url, params, HTTP_X_PANDECT_LIVE="collection"
                )
                self.assertEqual(response.status_code, 404)
                self.assertNotIn("X-Pandect-Live", response)
        response = self.client.get(routes[0][0], HTTP_X_PANDECT_LIVE="collection")
        self.assertNotContains(response, self.case.name)

    def test_selected_evidence_filter_skips_the_original_and_findings(self):
        source = self.text()
        self.login("owner")
        with patch(
            "core.evidence.workspace_views.evidence_findings_context"
        ) as findings:
            response = self.client.get(
                self.url("detail", source),
                {"q": "café", "type": "text", "state": "all"},
                HTTP_X_PANDECT_LIVE="collection",
            )
        self.assertContains(response, source.title)
        self.assertContains(response, 'aria-current="true"')
        findings.assert_not_called()

    def test_progress_poll_skips_the_collection_and_reader(self):
        source = TextMessageEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            sender=self.person,
            recipient=self.person,
            title="Saved message",
            body="A synthetic retained message.",
        )
        self.login("viewer")
        with (
            patch("core.evidence.workspace_views._list_context") as collection,
            patch("phone.presentation.reader_context") as reader,
        ):
            response = self.client.get(
                self.url("detail", source, kind="text-message"),
                {"q": "café"},
                HTTP_X_PANDECT_LIVE="1",
            )
        self.assertContains(response, 'id="evidence-findings"')
        collection.assert_not_called()
        reader.assert_not_called()
