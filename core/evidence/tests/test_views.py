import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import close_old_connections, connection
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from cases.models import Case, CaseMembership
from core.evidence.types import FILE_KINDS, KINDS, LOCATION_KINDS
from core.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence
from text.models import TextEvidence


class EvidenceViewTests(EvidenceViewTestMixin, TestCase):
    def test_bulk_page_requires_current_case_edit_access(self):
        self.assertEqual(self.client.get(self.url("bulk_create")).status_code, 302)
        for role, status in (
            ("owner", 200),
            ("editor", 200),
            ("viewer", 403),
            ("outsider", 404),
        ):
            self.login(role)
            with self.subTest(role=role):
                response = self.client.get(self.url("bulk_create"))
                self.assertEqual(response.status_code, status)
                collection = self.client.get(self.url("list"))
                if status == 200:
                    self.assertContains(collection, self.url("create"))
                    self.assertContains(
                        self.client.get(self.url("create")), self.url("bulk_create")
                    )
                    self.assertContains(response, "Shared source details")
                    self.assertContains(response, self.url("create"))
                    directives = response["Cache-Control"].split(", ")
                    self.assertIn("private", directives)
                    self.assertIn("no-store", directives)
                elif role == "viewer":
                    self.assertNotContains(collection, self.url("bulk_create"))
        self.login("editor")
        CaseMembership.objects.filter(
            case=self.case, user=self.users["editor"]
        ).delete()
        self.assertEqual(self.client.get(self.url("bulk_create")).status_code, 404)
        self.users["outsider"].is_superuser = True
        self.users["outsider"].save()
        self.login("outsider")
        self.assertEqual(self.client.get(self.url("bulk_create")).status_code, 404)

    def test_bulk_page_rejects_unverified_users_and_writes(self):
        self.login("owner")
        self.assertEqual(
            self.client.post(self.url("bulk_create"), self.payload()).status_code, 405
        )
        EmailAddress.objects.filter(user=self.users["owner"]).update(verified=False)
        self.assertEqual(self.client.get(self.url("bulk_create")).status_code, 302)
        self.assertEqual(TextEvidence.objects.count(), 0)

    @override_settings(PANDECT_MAX_BATCH_FILES=3, PANDECT_MAX_FILE_BYTES=4096)
    def test_bulk_page_displays_configured_limits_and_single_file_fallback(self):
        self.login("editor")
        response = self.client.get(self.url("bulk_create"))
        self.assertContains(response, 'data-max-batch-files="3"')
        self.assertContains(response, 'data-max-file-bytes="4096"')
        self.assertContains(response, "add one file at a time")

    def test_create_all_types_for_owner_and_editor_preserves_originals(self):
        for role in ("owner", "editor"):
            self.login(role)
            for kind, model in KINDS.items():
                with self.subTest(role=role, kind=kind):
                    data = self.payload(kind)
                    if kind in FILE_KINDS:
                        data["file"] = SimpleUploadedFile(
                            "original.bin", f"{role}-{kind} original\x00\r\n".encode()
                        )
                    elif kind == "text":
                        data["body"] += role
                    original = (
                        data["body"].encode()
                        if kind == "text"
                        else bytes(data["file"].read())
                        if kind in FILE_KINDS
                        else None
                    )
                    if kind in FILE_KINDS:
                        data["file"].seek(0)
                    response = self.client.post(
                        self.url("create"), data, HTTP_ACCEPT="application/json"
                    )
                    self.assertEqual(response.status_code, 201, response.content)
                    item = model.objects.get(pk=response.json()["id"])
                    self.assertEqual(item.uploaded_by, self.users[role])
                    self.assertEqual(item.case, self.case)
                    self.assertEqual(item.description, "")
                    self.assertEqual(item.source, "Synthetic witness")
                    self.assertEqual(item.source_description, "Synthetic source")
                    if original is not None:
                        self.assertEqual(
                            item.sha256, hashlib.sha256(original).hexdigest()
                        )
                    if kind == "text":
                        self.assertEqual(item.body, data["body"])
                    elif kind in FILE_KINDS:
                        self.assertEqual(item.file.read(), original)
                    elif kind in LOCATION_KINDS:
                        self.assertEqual(item.address, data["address"])
                    else:
                        self.assertEqual(item.sender_id, self.person.pk)
                        self.assertEqual(item.recipient_id, self.person.pk)

    def test_ordinary_html_forms_and_download_work_for_every_type(self):
        self.login("owner")
        for kind in KINDS:
            with self.subTest(kind=kind):
                form = self.client.get(self.url("create") + f"?kind={kind}")
                self.assertContains(form, "submission_id")
                response = self.client.post(self.url("create"), self.payload(kind))
                self.assertEqual(response.status_code, 302)
                detail = self.client.get(response["Location"])
                self.assertEqual(detail.status_code, 200)
                self.assertContains(detail, "New source")
                cache_directives = detail["Cache-Control"].split(", ")
                self.assertIn("private", cache_directives)
                self.assertIn("no-store", cache_directives)

    def test_viewer_and_unrelated_users_cannot_mutate(self):
        item = self.text()
        for role, status in (("viewer", 403), ("outsider", 404)):
            self.login(role)
            with self.subTest(role=role):
                self.assertEqual(
                    self.client.post(
                        self.url("create"),
                        self.payload(),
                        HTTP_ACCEPT="application/json",
                    ).status_code,
                    status,
                )
                self.assertEqual(
                    self.client.post(self.url("edit", item), {}).status_code, status
                )
                self.assertEqual(
                    self.client.post(self.url("archive", item)).status_code, status
                )
        self.assertEqual(TextEvidence.objects.count(), 1)

    def test_viewer_reads_archived_original_and_text_is_escaped(self):
        item = self.text()
        item.body = "<script>alert('untrusted')</script>"
        # A separate original records changed content, never replacement.
        item = TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Untrusted text",
            body=item.body,
        )
        self.login("editor")
        self.client.post(self.url("archive", item))
        self.login("viewer")
        response = self.client.get(self.url("detail", item))
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, "<script>alert")
        download = self.client.get(self.url("download", item))
        self.assertEqual(download.content, item.body.encode())
        self.assertEqual(download["Content-Type"], "application/octet-stream")
        self.assertEqual(
            download["Content-Disposition"],
            f'attachment; filename="evidence-{item.pk}.txt"',
        )
        self.assertEqual(download["X-Content-Type-Options"], "nosniff")

    def test_direct_object_download_and_search_are_case_scoped(self):
        item = self.text()
        self.login("outsider")
        for name in ("detail", "download", "edit"):
            self.assertEqual(self.client.get(self.url(name, item)).status_code, 404)
            self.assertEqual(
                self.client.get(self.url(name, item, case=self.other_case)).status_code,
                404,
            )
        self.login("owner")
        response = self.client.get(self.url("list"), {"q": "Interview"})
        self.assertContains(response, "Interview notes")
        self.assertNotContains(response, "Other case")
        self.users["outsider"].is_superuser = True
        self.users["outsider"].save()
        self.login("outsider")
        self.assertEqual(self.client.get(self.url("detail", item)).status_code, 404)

    def test_idempotent_retry_after_success_and_archive_returns_same_record(self):
        self.login("editor")
        data = self.payload()
        first = self.client.post(
            self.url("create"), data, HTTP_ACCEPT="application/json"
        )
        item = TextEvidence.objects.get(pk=first.json()["id"])
        self.client.post(self.url("archive", item))
        retry = self.client.post(
            self.url("create"), data, HTTP_ACCEPT="application/json"
        )
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.json()["id"], first.json()["id"])
        self.assertEqual(TextEvidence.objects.count(), 1)
        item.refresh_from_db()
        self.assertTrue(item.is_archived)

    def test_identical_original_requires_review_then_retains_a_separate_record(self):
        self.login("owner")
        first = self.client.post(
            self.url("create"), self.payload(), HTTP_ACCEPT="application/json"
        )
        data = self.payload()
        warning = self.client.post(
            self.url("create"), data, HTTP_ACCEPT="application/json"
        )
        self.assertEqual(warning.status_code, 409)
        self.assertEqual(warning.json()["code"], "duplicate_evidence")
        self.assertEqual(TextEvidence.objects.count(), 1)
        data["duplicate_confirmation"] = warning.json()["confirmation"]
        confirmed = self.client.post(
            self.url("create"), data, HTTP_ACCEPT="application/json"
        )
        self.assertEqual(confirmed.status_code, 201)
        self.assertNotEqual(first.json()["id"], confirmed.json()["id"])
        self.assertEqual(TextEvidence.objects.count(), 2)
        retry = self.client.post(
            self.url("create"), data, HTTP_ACCEPT="application/json"
        )
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.json()["id"], confirmed.json()["id"])
        self.assertEqual(TextEvidence.objects.count(), 2)

    def test_cross_actor_submission_token_is_a_generic_conflict(self):
        self.login("owner")
        data = self.payload()
        first = self.client.post(
            self.url("create"), data, HTTP_ACCEPT="application/json"
        )
        self.login("editor")
        response = self.client.post(
            self.url("create"), data, HTTP_ACCEPT="application/json"
        )
        self.assertEqual(response.status_code, 409)
        self.assertNotContains(response, first.json()["id"], status_code=409)

    def test_revocation_and_downgrade_affect_following_requests(self):
        self.login("editor")
        data = self.payload()
        first = self.client.post(
            self.url("create"), data, HTTP_ACCEPT="application/json"
        )
        item = TextEvidence.objects.get(pk=first.json()["id"])
        membership = CaseMembership.objects.get(
            case=self.case, user=self.users["editor"]
        )
        membership.role = "viewer"
        membership.save()
        response = self.client.post(
            self.url("create"), data, HTTP_ACCEPT="application/json"
        )
        self.assertEqual(response.status_code, 403)
        membership.delete()
        self.assertEqual(self.client.get(self.url("download", item)).status_code, 404)
        self.assertEqual(
            self.client.post(
                self.url("create"), data, HTTP_ACCEPT="application/json"
            ).status_code,
            404,
        )

    def test_details_actions_follow_case_roles_and_keep_collection_context(self):
        item = self.text()
        for role in ("owner", "editor", "viewer"):
            self.login(role)
            response = self.client.get(self.url("detail", item), {"section": "details"})
            if role == "viewer":
                self.assertNotContains(response, "Edit details")
                self.assertNotContains(response, self.url("archive", item))
            else:
                self.assertContains(response, "Edit details")
                self.assertContains(response, self.url("archive", item))
        self.login("editor")
        for action in ("archive", "restore"):
            response = self.client.post(
                self.url(action, item) + "?q=notes&state=all&section=details"
            )
            self.assertRedirects(
                response,
                self.url("detail", item) + "?q=notes&state=all&section=details",
            )

    def test_archive_restore_is_idempotent_and_preserves_original(self):
        item = self.text()
        original = (item.body, item.sha256, item.created_at, item.uploaded_by_id)
        self.login("editor")
        self.assertEqual(self.client.get(self.url("archive", item)).status_code, 405)
        self.client.post(self.url("archive", item))
        item.refresh_from_db()
        first_change = item.archive_changed_at
        self.client.post(self.url("archive", item))
        item.refresh_from_db()
        self.assertTrue(item.is_archived)
        self.assertEqual(item.archive_changed_at, first_change)
        self.assertEqual(item.archive_changed_by, self.users["editor"])
        self.assertNotContains(self.client.get(self.url("list")), "Interview notes")
        self.assertContains(
            self.client.get(self.url("list"), {"state": "archived"}), "Interview notes"
        )
        self.client.post(self.url("restore", item))
        item.refresh_from_db()
        self.assertFalse(item.is_archived)
        self.assertEqual(
            (item.body, item.sha256, item.created_at, item.uploaded_by_id), original
        )

    def test_edit_source_details_rejects_stale_data_and_preserves_original(self):
        item = self.text(description="Earlier investigator context")
        self.login("editor")
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.url("edit", item))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(any("FOR UPDATE" in query["sql"] for query in queries))
        stale = item.edited_at.isoformat()
        item.source_description = "A collaborator's latest source details"
        item.save(update_fields=["source_description"])
        data = {
            "kind": "text",
            "title": item.title,
            "body": "Replacement attempt",
            "description": "Forged description",
            "source_description": "My proposed source correction",
            "expected_edited_at": stale,
        }
        conflict = self.client.post(self.url("edit", item), data)
        self.assertEqual(conflict.status_code, 409)
        self.assertContains(conflict, "My proposed source correction", status_code=409)
        self.assertContains(
            conflict, "A collaborator&#x27;s latest source details", status_code=409
        )
        self.assertContains(conflict, "Save reviewed changes", status_code=409)
        item.refresh_from_db()
        self.assertEqual(
            item.source_description, "A collaborator's latest source details"
        )
        self.assertNotIn("description", conflict.context["form"].fields)
        data["expected_edited_at"] = conflict.context["form"][
            "expected_edited_at"
        ].value()
        response = self.client.post(self.url("edit", item), data)
        self.assertEqual(response.status_code, 302)
        item.refresh_from_db()
        self.assertEqual(item.source_description, "My proposed source correction")
        self.assertEqual(item.description, "Earlier investigator context")
        self.assertNotEqual(item.body, "Replacement attempt")

        self.login("viewer")
        details_url = self.url("detail", item) + "?section=details"
        self.assertContains(
            self.client.get(details_url), "Earlier investigator context"
        )
        self.assertContains(
            self.client.get(self.url("list"), {"q": "Earlier investigator context"}),
            item.title,
        )
        self.login("outsider")
        self.assertEqual(self.client.get(details_url).status_code, 404)

    def test_metadata_and_empty_original_validation_keeps_entered_values(self):
        self.login("owner")
        response = self.client.post(
            self.url("create"), self.payload(body="", title="Keep this title")
        )
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "Keep this title", status_code=400)
        self.assertFalse(TextEvidence.objects.exists())
        response = self.client.post(
            self.url("create"),
            self.payload("image", width=-1),
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("width", response.json()["errors"])

    def test_untrusted_declared_type_and_download_filename_are_bounded(self):
        self.login("owner")
        response = self.client.post(
            self.url("create"),
            self.payload(
                "document",
                file=SimpleUploadedFile(
                    "source.bin", b"original", content_type="x" * 300
                ),
            ),
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("file", response.json()["errors"])
        self.assertFalse(DocumentEvidence.objects.exists())
        item = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Untrusted name",
            file=SimpleUploadedFile("original\nsource.txt", b"original"),
        )
        response = self.client.get(self.url("download", item, kind="document"))
        self.assertEqual(response.status_code, 200)
        self.assertIn('filename="originalsource.txt"', response["Content-Disposition"])
        self.assertEqual(b"".join(response.streaming_content), b"original")

    @override_settings(PANDECT_MAX_TEXT_BYTES=5)
    def test_utf8_text_limit_counts_bytes_not_characters(self):
        self.login("owner")
        response = self.client.post(
            self.url("create"), self.payload(body="ééé"), HTTP_ACCEPT="application/json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("body", response.json()["errors"])
        self.assertFalse(TextEvidence.objects.exists())

    @override_settings(PANDECT_MAX_FILE_BYTES=5)
    def test_streaming_file_limit_stops_oversized_original(self):
        self.login("owner")
        response = self.client.post(
            self.url("create"), self.payload("document"), HTTP_ACCEPT="application/json"
        )
        self.assertEqual(response.status_code, 413)
        self.assertFalse(DocumentEvidence.objects.exists())
        self.assertFalse(list(self.media_root.rglob("*")))

    def test_unavailable_storage_returns_safe_error_and_retains_record(self):
        self.login("owner")
        with patch.object(
            DocumentEvidence, "save", side_effect=OSError("private backend detail")
        ):
            response = self.client.post(
                self.url("create"),
                self.payload("document"),
                HTTP_ACCEPT="application/json",
            )
        self.assertEqual(response.status_code, 503)
        self.assertNotContains(response, "private backend detail", status_code=503)
        self.assertFalse(DocumentEvidence.objects.exists())
        with patch.object(DocumentEvidence, "save", side_effect=OSError("unavailable")):
            response = self.client.post(self.url("create"), self.payload("document"))
        self.assertContains(response, "New source", status_code=503)
        self.assertContains(response, "Synthetic source", status_code=503)
        item = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Missing original",
            file=SimpleUploadedFile("missing.txt", b"original"),
        )
        item.file.storage.delete(item.file.name)
        response = self.client.get(self.url("download", item, kind="document"))
        self.assertEqual(response.status_code, 503)
        self.assertTrue(DocumentEvidence.objects.filter(pk=item.pk).exists())
        self.assertNotContains(response, item.file.name, status_code=503)

    def test_uploads_require_verified_session_and_csrf(self):
        response = self.client.post(
            self.url("create"), self.payload(), HTTP_ACCEPT="application/json"
        )
        self.assertEqual(response.status_code, 401)
        self.login("owner")
        EmailAddress.objects.filter(user=self.users["owner"]).update(verified=False)
        self.assertEqual(self.client.get(self.url("list")).status_code, 302)
        self.assertEqual(
            self.client.post(
                self.url("create"), self.payload(), HTTP_ACCEPT="application/json"
            ).status_code,
            403,
        )
        EmailAddress.objects.filter(user=self.users["owner"]).update(verified=True)
        protected = Client(enforce_csrf_checks=True)
        protected.force_login(self.users["owner"])
        self.assertEqual(
            protected.post(self.url("create"), self.payload()).status_code, 403
        )

    def test_list_is_paginated_metadata_only_without_cross_case_results(self):
        for number in range(28):
            TextEvidence.objects.create(
                case=self.case,
                uploaded_by=self.users["owner"],
                title=f"Source {number}",
                body="original-not-for-list-query",
            )
        TextEvidence.objects.create(
            case=self.other_case,
            uploaded_by=self.users["outsider"],
            title="Private other material",
            body="other",
        )
        self.login("viewer")
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.url("list"))
        self.assertEqual(response.context["page_obj"].paginator.count, 28)
        self.assertEqual(len(response.context["rows"]), 25)
        self.assertNotContains(response, "Private other material")
        self.assertFalse(
            any('"body"' in query["sql"] for query in queries.captured_queries)
        )
        self.assertLess(len(queries), 20)
        second = self.client.get(self.url("list"), {"page": 2})
        self.assertEqual(len(second.context["rows"]), 3)

    def test_pagination_and_detail_links_share_bounded_search_parameters(self):
        for number in range(26):
            TextEvidence.objects.create(
                case=self.case,
                uploaded_by=self.users["owner"],
                title=f"Source {number}",
                body="x" * 250,
            )
        self.login("viewer")
        response = self.client.get(
            self.url("list"), {"q": "x" * 250, "unexpected": "discard"}
        )
        urls = [
            response.context["pagination"]["next"],
            response.context["rows"][0]["url"],
        ]
        for url in urls:
            parameters = parse_qs(urlsplit(url).query)
            self.assertEqual(parameters["q"], ["x" * 200])
            self.assertNotIn("unexpected", parameters)

    def test_invalid_uuid_kind_and_missing_parent_cannot_leak_records(self):
        self.login("owner")
        item = self.text()
        response = self.client.get(self.url("detail", item, kind="unsupported"))
        self.assertEqual(response.status_code, 404)
        response = self.client.post(
            self.url("create"),
            self.payload(kind="unsupported"),
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertNotContains(response, "Interview notes", status_code=400)
        self.assertEqual(
            self.client.get(
                f"/cases/{self.case.pk}/evidence/text/not-a-uuid/"
            ).status_code,
            404,
        )

    def test_submission_id_cannot_be_changed_after_creation(self):
        item = self.text(submission_id=uuid4())
        item.submission_id = uuid4()
        with self.assertRaises(ValidationError):
            item.save()

    def test_submitted_ownership_and_case_values_are_ignored(self):
        self.login("owner")
        data = self.payload(
            case=str(self.other_case.pk),
            uploaded_by=str(self.users["outsider"].pk),
            sha256="f" * 64,
        )
        response = self.client.post(
            self.url("create"), data, HTTP_ACCEPT="application/json"
        )
        self.assertEqual(response.status_code, 201)
        item = TextEvidence.objects.get(pk=response.json()["id"])
        self.assertEqual(item.case_id, self.case.pk)
        self.assertEqual(item.uploaded_by, self.users["owner"])
        self.assertNotEqual(item.sha256, "f" * 64)

    def test_ordinary_users_cannot_delete_individual_evidence(self):
        self.login("owner")
        item = self.text()
        self.assertEqual(
            self.client.post(self.url("delete", item), {"confirm": "on"}).status_code,
            403,
        )
        self.assertTrue(TextEvidence.objects.filter(pk=item.pk).exists())


class ConcurrentSubmissionTests(TransactionTestCase):
    def test_concurrent_retries_create_one_retained_original(self):
        self.check_concurrent_submissions(same_submission=True)

    def test_concurrent_new_copies_require_duplicate_review(self):
        self.check_concurrent_submissions(same_submission=False)

    def check_concurrent_submissions(self, *, same_submission):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        owner = get_user_model().objects.create_user(
            username="parallel-owner", email="parallel@example.test"
        )
        EmailAddress.objects.create(
            user=owner, email=owner.email, verified=True, primary=True
        )
        case = Case.objects.create(owner=owner, name="Concurrent sources")
        url = reverse("core:evidence:create", kwargs={"case_id": case.pk})
        submission = str(uuid4())
        ready = Barrier(2)

        def submit():
            close_old_connections()
            try:
                client = Client()
                client.force_login(owner)
                ready.wait(timeout=10)
                response = client.post(
                    url,
                    {
                        "kind": "document",
                        "title": "One source",
                        "submission_id": submission
                        if same_submission
                        else str(uuid4()),
                        "file": SimpleUploadedFile("original.txt", b"one original"),
                    },
                    HTTP_ACCEPT="application/json",
                )
                return response.status_code, response.json()
            finally:
                connection.close()

        with (
            self.settings(MEDIA_ROOT=directory.name),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            results = list(pool.map(lambda _: submit(), range(2)))
        self.assertEqual(
            sorted(status for status, _ in results),
            [200, 201] if same_submission else [201, 409],
        )
        saved_id = str(DocumentEvidence.objects.get().pk)
        for status, result in results:
            if status == 409:
                self.assertEqual(result["code"], "duplicate_evidence")
                self.assertEqual(result["matches"][0]["id"], saved_id)
            else:
                self.assertEqual(result["id"], saved_id)
        self.assertEqual(DocumentEvidence.objects.count(), 1)
        self.assertEqual(
            len([path for path in Path(directory.name).rglob("*") if path.is_file()]), 1
        )
