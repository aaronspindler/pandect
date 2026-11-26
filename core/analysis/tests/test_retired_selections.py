"""Selections has no product routes; existing stored references remain intact."""

from django.test import TestCase
from django.urls import reverse

from core.analysis.source_selection import snapshot_sources
from core.models import SavedSelection
from core.testing import EvidenceViewTestMixin


class RetiredSelectionTests(EvidenceViewTestMixin, TestCase):
    def test_removed_routes_and_actions_do_not_expose_or_mutate_stored_collections(
        self,
    ):
        source = self.text()
        members = [
            {
                "type": "source",
                "source": snapshot_sources(
                    self.case, [{"kind": "text", "id": str(source.pk)}]
                )[0],
            }
        ]
        saved = SavedSelection.objects.create(
            case=self.case,
            name="Retained collection",
            created_by=self.users["owner"],
            updated_by=self.users["owner"],
            members=members,
        )
        index = reverse("core:analysis:index", args=[self.case.pk])
        for role in ("owner", "editor", "viewer"):
            self.login(role)
            for url in (index, self.url("detail", source)):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, "Save to selection")
                self.assertNotContains(response, index + "selections/")
            for suffix in (
                "",
                "new/",
                "pick/",
                f"{saved.pk}/",
                f"{saved.pk}/edit/",
                f"{saved.pk}/add/",
                f"{saved.pk}/remove/0/",
                f"{saved.pk}/delete/",
                f"{saved.pk}/brief/",
                f"{saved.pk}/analyze/",
                f"{saved.pk}/synthesize/",
            ):
                url = index + "selections/" + suffix
                with self.subTest(role=role, url=url):
                    self.assertEqual(self.client.get(url).status_code, 404)
                    self.assertEqual(
                        self.client.post(url, {"confirm": "on"}).status_code, 404
                    )
        saved.refresh_from_db()
        self.assertEqual(saved.members, members)
        self.assertTrue(type(source).objects.filter(pk=source.pk).exists())

    def test_stored_references_still_require_current_evidence_deletion_review(self):
        source = self.text()
        member = {
            "type": "source",
            "source": snapshot_sources(
                self.case, [{"kind": "text", "id": str(source.pk)}]
            )[0],
        }
        saved = SavedSelection.objects.create(
            case=self.case,
            name="Retained collection",
            created_by=self.users["owner"],
            updated_by=self.users["owner"],
            members=[member],
        )
        owner = self.users["owner"]
        owner.is_superuser = True
        owner.save(update_fields=["is_superuser"])
        self.login("owner")
        url = self.url("delete", source)
        review = self.client.get(url)
        impact = next(
            entry
            for entry in review.context["deletion_plan"].entries
            if entry["type"] == "Shared selection"
        )
        self.assertEqual(impact["members"], [{"index": 0, "reference": member}])
        saved.revision += 1
        saved.save()
        stale = self.client.post(
            url, {"confirm": "on", "revision": review.context["deletion_revision"]}
        )
        self.assertEqual(stale.status_code, 409)
        self.assertTrue(type(source).objects.filter(pk=source.pk).exists())
        deleted = self.client.post(
            url, {"confirm": "on", "revision": stale.context["deletion_revision"]}
        )
        self.assertEqual(deleted.status_code, 302)
        self.assertFalse(type(source).objects.filter(pk=source.pk).exists())
        saved.refresh_from_db()
        self.assertEqual(saved.members, [member])
