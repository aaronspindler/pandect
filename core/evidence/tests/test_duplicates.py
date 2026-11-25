from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from audio.models import AudioEvidence
from cases.models import Case, CaseMembership
from core.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence
from image.models import ImageEvidence


class DuplicateUploadTests(EvidenceViewTestMixin, TestCase):
    original = b"Synthetic original\x00\r\n"

    def upload(self, *, kind="document", case=None, original=None, **values):
        return self.client.post(
            self.url("create", case=case),
            self.payload(
                kind,
                file=SimpleUploadedFile("renamed.txt", original or self.original),
                **values,
            ),
            HTTP_ACCEPT="application/json",
        )

    def document(self, *, model=DocumentEvidence, **values):
        return model.objects.create(
            case=values.pop("case", self.case),
            uploaded_by=self.users["owner"],
            title=values.pop("title", "Earlier source"),
            file=SimpleUploadedFile(
                "earlier.bin", values.pop("original", self.original)
            ),
            **values,
        )

    def test_owner_and_editor_review_only_active_matches_across_categories(self):
        first = self.document(model=ImageEvidence)
        self.document(
            model=AudioEvidence,
            is_archived=True,
            archive_changed_by=self.users["owner"],
            archive_changed_at=first.created_at,
        )
        stored_files = list(self.media_root.rglob("*"))
        for role in ("owner", "editor"):
            self.login(role)
            with self.subTest(role=role):
                response = self.upload()
                self.assertEqual(response.status_code, 409)
                matches = response.json()["matches"]
                self.assertEqual([row["id"] for row in matches], [str(first.pk)])
                self.assertIn("private", response["Cache-Control"])
                self.assertIn("no-store", response["Cache-Control"])
                self.assertEqual(DocumentEvidence.objects.count(), 0)
                self.assertEqual(list(self.media_root.rglob("*")), stored_files)

    def test_identical_filename_with_different_bytes_is_allowed(self):
        self.document()
        self.login("owner")
        response = self.client.post(
            self.url("create"),
            self.payload(
                "document", file=SimpleUploadedFile("earlier.bin", b"Changed bytes")
            ),
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(response.status_code, 201)

    def test_other_case_matches_are_neither_reported_nor_blocking(self):
        private = self.document(case=self.other_case)
        self.login("owner")
        response = self.upload()
        self.assertEqual(response.status_code, 201)
        self.assertNotIn(str(private.pk), response.content.decode())

    def test_evidence_checksum_filter_preserves_exact_case_scoped_matches(self):
        earlier = self.document()
        self.document(case=self.other_case)
        self.login("viewer")
        for checksum, ids in (
            (earlier.sha256, [earlier.pk]),
            (earlier.sha256.upper(), []),
            (earlier.sha256[:-1], []),
        ):
            with self.subTest(checksum=checksum):
                response = self.client.get(self.url("list"), {"checksum": checksum})
                self.assertEqual([row["id"] for row in response.context["rows"]], ids)

    def test_file_and_captured_utf8_text_match_without_text_normalization(self):
        self.login("owner")
        original = self.text()
        response = self.client.post(
            self.url("create"),
            self.payload(
                "document", file=SimpleUploadedFile("notes.txt", original.body.encode())
            ),
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["matches"][0]["id"], str(original.pk))
        changed = self.client.post(
            self.url("create"),
            self.payload(body=original.body + " "),
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(changed.status_code, 201)

    def test_confirmation_is_bound_to_original_kind_case_and_actor(self):
        self.document()
        self.login("owner")
        confirmation = self.upload().json()["confirmation"]
        changed = self.original + b" revised"
        self.document(original=changed)
        self.assertEqual(
            self.upload(
                original=changed, duplicate_confirmation=confirmation
            ).status_code,
            409,
        )
        self.assertEqual(
            self.upload(kind="image", duplicate_confirmation=confirmation).status_code,
            409,
        )
        other = Case.objects.create(
            owner=self.users["owner"], name="Another owned case"
        )
        self.document(case=other)
        self.assertEqual(
            self.upload(case=other, duplicate_confirmation=confirmation).status_code,
            409,
        )
        self.login("editor")
        self.assertEqual(
            self.upload(duplicate_confirmation=confirmation).status_code, 409
        )
        self.login("owner")
        self.assertEqual(
            self.upload(duplicate_confirmation=confirmation + "tampered").status_code,
            409,
        )
        self.assertEqual(
            self.upload(duplicate_confirmation=confirmation).status_code, 201
        )
        self.assertEqual(DocumentEvidence.objects.filter(case=self.case).count(), 3)

    def test_no_javascript_warning_keeps_details_and_requires_matching_file_reselection(
        self,
    ):
        earlier = self.document()
        self.login("owner")
        data = self.payload(
            "document", file=SimpleUploadedFile("copy.bin", self.original)
        )
        response = self.client.post(self.url("create"), data)
        self.assertEqual(response.status_code, 409)
        self.assertContains(
            response, "Matching evidence already exists", status_code=409
        )
        self.assertContains(
            response, self.url("detail", earlier, "document"), status_code=409
        )
        self.assertEqual(response.context["form"]["title"].value(), data["title"])
        self.assertNotContains(response, "data-evidence-upload", status_code=409)
        data["file"] = SimpleUploadedFile("copy.bin", self.original)
        data["duplicate_confirmation"] = response.context["duplicates"]["confirmation"]
        saved = self.client.post(self.url("create"), data)
        self.assertEqual(saved.status_code, 302)
        self.assertEqual(DocumentEvidence.objects.count(), 2)

    def test_current_access_is_required_for_checks_and_confirmations(self):
        original = self.document()
        for role, status in (("viewer", 403), ("outsider", 404)):
            self.login(role)
            response = self.upload()
            self.assertEqual(response.status_code, status)
            self.assertNotIn(str(original.pk), response.content.decode())
        self.login("editor")
        confirmation = self.upload().json()["confirmation"]
        membership = CaseMembership.objects.get(
            case=self.case, user=self.users["editor"]
        )
        membership.role = "viewer"
        membership.save()
        self.assertEqual(
            self.upload(duplicate_confirmation=confirmation).status_code, 403
        )
        membership.delete()
        self.assertEqual(
            self.upload(duplicate_confirmation=confirmation).status_code, 404
        )
        self.assertEqual(DocumentEvidence.objects.count(), 1)

    def test_match_list_is_bounded_and_escapes_untrusted_titles(self):
        self.login("owner")
        for _ in range(12):
            self.document(title="<script>untrusted</script>")
        response = self.upload()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(len(response.json()["matches"]), 10)
        self.assertTrue(response.json()["has_more"])
        response = self.client.post(
            self.url("create"),
            self.payload(
                "document", file=SimpleUploadedFile("copy.bin", self.original)
            ),
        )
        self.assertContains(
            response, "&lt;script&gt;untrusted&lt;/script&gt;", status_code=409
        )
        self.assertNotContains(response, "<script>untrusted</script>", status_code=409)
