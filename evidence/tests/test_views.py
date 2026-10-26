import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest.mock import patch
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
from evidence.forms import FILE_KINDS, KINDS, LOCATION_KINDS
from evidence.models import DocumentEvidence, TextEvidence
from people.models import Person


class EvidenceViewTestMixin:
    @classmethod
    def setUpTestData(cls):
        cls.users = {}
        for role in ("owner", "editor", "viewer", "outsider"):
            user = get_user_model().objects.create_user(
                username=role,
                email=f"{role}@example.test",
                password="synthetic-password",
            )
            EmailAddress.objects.create(
                user=user, email=user.email, verified=True, primary=True
            )
            cls.users[role] = user
        cls.case = Case.objects.create(owner=cls.users["owner"], name="Harbour records")
        cls.other_case = Case.objects.create(
            owner=cls.users["outsider"], name="Other case"
        )
        cls.person = Person.objects.create(case=cls.case, first_name="Synthetic person")
        CaseMembership.objects.create(
            case=cls.case, user=cls.users["editor"], role="editor"
        )
        CaseMembership.objects.create(
            case=cls.case, user=cls.users["viewer"], role="viewer"
        )

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.media_root = Path(directory.name)
        settings = self.settings(MEDIA_ROOT=directory.name)
        settings.enable()
        self.addCleanup(settings.disable)

    def url(self, name, item=None, kind="text", case=None):
        kwargs = {"case_id": (case or self.case).pk}
        if item:
            kwargs |= {"kind": kind, "evidence_id": item.pk}
        return reverse(f"evidence:{name}", kwargs=kwargs)

    def login(self, role):
        self.client.force_login(self.users[role])

    def text(self, **values):
        return TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Interview notes",
            body="Original café\r\n  preserved.  ",
            **values,
        )

    def payload(self, kind="text", **overrides):
        data = {
            "kind": kind,
            "submission_id": str(uuid4()),
            "title": "New source",
            "description": "Investigator description",
            "source": "Synthetic witness",
            "source_description": "Synthetic source",
        }
        if kind == "text":
            data["body"] = "  Original café\r\nunchanged.  "
        elif kind in LOCATION_KINDS:
            data["address"] = "  Synthetic harbour office\r\n"
            if kind == "person-location":
                data["person"] = str(self.person.pk)
        else:
            data["file"] = SimpleUploadedFile(
                "original.bin",
                b"Original\x00\r\n",
                content_type="application/octet-stream",
            )
        return data | overrides


class EvidenceViewTests(EvidenceViewTestMixin, TestCase):
    def test_create_all_types_for_owner_and_editor_preserves_originals(self):
        for role in ("owner", "editor"):
            self.login(role)
            for kind, model in KINDS.items():
                with self.subTest(role=role, kind=kind):
                    data = self.payload(kind)
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
                    self.assertEqual(item.source, "Synthetic witness")
                    if original is not None:
                        self.assertEqual(
                            item.sha256, hashlib.sha256(original).hexdigest()
                        )
                    if kind == "text":
                        self.assertEqual(item.body, data["body"])
                    elif kind in FILE_KINDS:
                        self.assertEqual(item.file.read(), original)
                    else:
                        self.assertEqual(item.address, data["address"])

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
        self.assertIn("attachment", download["Content-Disposition"])
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

    def test_different_submission_of_identical_original_is_not_deduplicated(self):
        self.login("owner")
        for _ in range(2):
            self.assertEqual(
                self.client.post(
                    self.url("create"), self.payload(), HTTP_ACCEPT="application/json"
                ).status_code,
                201,
            )
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

    def test_edit_descriptions_rejects_stale_data_and_ignores_original_changes(self):
        item = self.text()
        self.login("editor")
        stale = item.edited_at.isoformat()
        item.description = "A collaborator's latest description"
        item.save(update_fields=["description"])
        data = {
            "kind": "text",
            "title": item.title,
            "body": "Replacement attempt",
            "description": "My proposed correction",
            "expected_edited_at": stale,
        }
        conflict = self.client.post(self.url("edit", item), data)
        self.assertEqual(conflict.status_code, 409)
        self.assertContains(conflict, "My proposed correction", status_code=409)
        self.assertContains(
            conflict, "A collaborator&#x27;s latest description", status_code=409
        )
        self.assertContains(conflict, "Save reviewed changes", status_code=409)
        item.refresh_from_db()
        self.assertEqual(item.description, "A collaborator's latest description")
        data["expected_edited_at"] = conflict.context["form"][
            "expected_edited_at"
        ].value()
        response = self.client.post(self.url("edit", item), data)
        self.assertEqual(response.status_code, 302)
        item.refresh_from_db()
        self.assertEqual(item.description, "My proposed correction")
        self.assertNotEqual(item.body, "Replacement attempt")

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

    def test_no_end_user_route_can_delete_individual_evidence(self):
        self.login("owner")
        item = self.text()
        self.assertEqual(
            self.client.post(self.url("detail", item) + "delete/").status_code, 404
        )
        self.assertTrue(TextEvidence.objects.filter(pk=item.pk).exists())


class ConcurrentSubmissionTests(TransactionTestCase):
    def test_concurrent_retries_create_one_retained_original(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        owner = get_user_model().objects.create_user(
            username="parallel-owner", email="parallel@example.test"
        )
        EmailAddress.objects.create(
            user=owner, email=owner.email, verified=True, primary=True
        )
        case = Case.objects.create(owner=owner, name="Concurrent sources")
        url = reverse("evidence:create", kwargs={"case_id": case.pk})
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
                        "submission_id": submission,
                        "file": SimpleUploadedFile("original.txt", b"one original"),
                    },
                    HTTP_ACCEPT="application/json",
                )
                return response.status_code, response.json()["id"]
            finally:
                connection.close()

        with (
            self.settings(MEDIA_ROOT=directory.name),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            results = list(pool.map(lambda _: submit(), range(2)))
        self.assertEqual(sorted(status for status, _ in results), [200, 201])
        self.assertEqual(len({item_id for _, item_id in results}), 1)
        self.assertEqual(DocumentEvidence.objects.count(), 1)
        self.assertEqual(
            len([path for path in Path(directory.name).rglob("*") if path.is_file()]), 1
        )
