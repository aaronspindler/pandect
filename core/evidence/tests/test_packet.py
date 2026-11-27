from urllib.parse import parse_qs, urlsplit

from django.test import TestCase

from cases.models import CaseMembership
from core.evidence.packet import MAX_EXHIBITS
from core.testing import EvidenceViewTestMixin
from text.models import TextEvidence


class EvidencePacketTests(EvidenceViewTestMixin, TestCase):
    @staticmethod
    def ref(item):
        return f"text:{item.pk}"

    def prepare(self, *items):
        response = self.client.post(
            self.url("packet"), {"source": [self.ref(item) for item in items]}
        )
        return self.selection(response)

    @staticmethod
    def selection(response):
        return parse_qs(urlsplit(response.url).query)["selection"][0]

    @staticmethod
    def exhibit(slot, title="Reviewed excerpt", **fields):
        return {
            f"exhibit-{slot}-{key}": value
            for key, value in {
                "number": slot,
                "title": title,
                "kind": "contrary",
                "citation": "Interview, line 4",
                "excerpt": "[Name omitted]\r\n<Reviewed passage>",
                **fields,
            }.items()
        }

    def test_preview_binds_download_to_reviewed_fields(self):
        item = self.text(source="Private source metadata")
        self.login("editor")
        selection = self.prepare(item)
        data = {
            "selection": selection,
            "title": "Review copy",
            **self.exhibit(1, date="2026-11-05", include_checksum="on"),
        }
        url = self.url("packet_review")
        response = self.client.post(url, {**data, "action": "preview"})
        self.assertContains(response, "Review evidence packet")
        self.assertContains(response, "Contrary material")
        self.assertContains(response, "Retained source SHA-256")
        self.assertNotContains(response, "Private source metadata")

        download = {**data, "action": "download", "review": response.context["review"]}
        changed = self.client.post(url, {**download, "exhibit-1-excerpt": "Changed"})
        self.assertContains(changed, "The packet changed.")
        self.assertFalse(changed.has_header("Content-Disposition"))
        response = self.client.post(url, download)
        self.assertEqual(
            response["Content-Disposition"],
            'attachment; filename="evidence-packet.html"',
        )
        self.assertIn("no-store", response["Cache-Control"])
        self.assertContains(response, "[Name omitted]\n&lt;Reviewed passage&gt;")
        self.assertContains(response, item.sha256)
        for private in (
            "Harbour records",
            "Original café",
            "Private source metadata",
            str(item.pk),
        ):
            self.assertNotContains(response, private)

    def test_selection_spans_searches_and_can_be_reordered(self):
        first = self.text()
        second = TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Second record",
            body="Another original",
        )
        self.login("owner")
        first_selection = self.prepare(first)
        response = self.client.post(
            self.url("packet") + f"?selection={first_selection}&q=Second",
            {"source": self.ref(second)},
        )
        selection = self.selection(response)
        data = {
            "action": "preview",
            "selection": selection,
            "title": "Review copy",
            **self.exhibit(1, title="First", number=2),
            **self.exhibit(2, title="Second", number=1),
        }
        response = self.client.post(self.url("packet_review"), data)
        self.assertContains(response, "Exhibit 1 · Second")
        self.assertContains(response, "Exhibit 2 · First")
        invalid = self.client.post(
            self.url("packet_review"), {**data, "exhibit-2-number": 2}
        )
        self.assertContains(invalid, "Use each exhibit number once.")
        self.assertIsNone(invalid.context.get("preview"))

        removed = self.client.post(
            self.url("packet") + f"?selection={selection}", {"remove": self.ref(first)}
        )
        response = self.client.get(removed.url)
        self.assertEqual(
            [row["ref"] for row in response.context["selected_sources"]],
            [self.ref(second)],
        )

    def test_foreign_source_and_viewer_access_are_rejected(self):
        foreign = TextEvidence.objects.create(
            case=self.other_case,
            uploaded_by=self.users["outsider"],
            title="Foreign source",
            body="Do not disclose",
        )
        self.login("owner")
        response = self.client.post(self.url("packet"), {"source": self.ref(foreign)})
        self.assertContains(response, "Choose active evidence from this case.")
        self.assertNotContains(response, foreign.title)
        for role, status in (("viewer", 403), ("outsider", 404)):
            self.login(role)
            for name in ("packet", "packet_review"):
                self.assertEqual(self.client.get(self.url(name)).status_code, status)

    def test_oversized_selection_keeps_the_packet_and_shows_how_to_recover(self):
        items = [self.text() for _ in range(MAX_EXHIBITS + 1)]
        self.login("editor")
        picker = self.url("packet")

        too_many = self.client.post(
            picker, {"source": [self.ref(item) for item in items]}
        )
        self.assertContains(
            too_many, f"Choose up to {MAX_EXHIBITS} sources per packet."
        )
        self.assertNotContains(too_many, "Choose active evidence from this case.")
        self.assertEqual(too_many.context["selected_sources"], [])

        retained = items[:-2]
        two_more = [self.ref(item) for item in items[-2:]]
        selection = self.prepare(*retained)
        selected_picker = picker + f"?selection={selection}"
        response = self.client.post(selected_picker, {"source": two_more})
        self.assertContains(
            response, "Choose fewer new sources or remove a selected source."
        )
        self.assertEqual(response.context["selection"], selection)
        self.assertEqual(
            [row["ref"] for row in response.context["selected_sources"]],
            [self.ref(item) for item in retained],
        )
        for ref in two_more:
            self.assertContains(response, f'name="source" value="{ref}" checked')

        added = self.client.post(selected_picker, {"source": two_more[:1]}, follow=True)
        self.assertContains(
            added, f"Selected sources ({MAX_EXHIBITS} of {MAX_EXHIBITS})"
        )
        self.assertContains(added, "Remove a selected source to add a different one.")
        self.assertContains(added, "Prepare exhibits")
        self.assertNotContains(added, "Add selected sources")

    def test_archived_or_revoked_sources_stop_review_and_download(self):
        item = self.text()
        self.login("editor")
        selection = self.prepare(item)
        data = {
            "selection": selection,
            "title": "Review copy",
            **self.exhibit(1),
        }
        url = self.url("packet_review")
        preview = self.client.post(url, {**data, "action": "preview"})
        download = {**data, "action": "download", "review": preview.context["review"]}
        self.client.post(self.url("archive", item))
        self.assertEqual(self.client.post(url, download).status_code, 409)
        self.client.post(self.url("restore", item))
        CaseMembership.objects.filter(
            case=self.case, user=self.users["editor"]
        ).delete()
        self.assertEqual(self.client.post(url, download).status_code, 404)
