"""In-app superuser deletion retains the existing case and file boundaries."""

import json
from unittest.mock import patch
from uuid import uuid4

from allauth.account.models import EmailAddress
from django.contrib.admin.models import DELETION, LogEntry
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from cases.models import CaseMembership
from core.evidence import deletion
from core.evidence.types import CALL_KINDS, KINDS
from core.models import (
    AnalysisArtifact,
    AnalysisResult,
    AnalysisRun,
    GatewayRequest,
    ImportantItem,
    ImportantNote,
)
from core.tests.testing import ALL_MODELS, EvidenceTestMixin
from document.models import DocumentEvidence
from geo.models import (
    VehicleLocationEvidence,
    VehicleLocationImport,
    VehicleLocationImportRow,
)
from geo.tests.testing import csv_content, parse_csv
from geo.vehicle_import_jobs import process_import
from phone.models import CallEvidenceAttachment, CommunicationContext


class EvidenceDeletionViewTests(EvidenceTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        # Product superusers need no staff/admin access.
        self.operator = get_user_model().objects.create_user(
            username="operator", email="operator@example.test", is_superuser=True
        )
        for user in (
            self.operator,
            self.owner,
            self.editor,
            self.viewer,
            self.outsider,
        ):
            EmailAddress.objects.create(
                user=user, email=f"{user.username}@example.test", verified=True
            )
        self.membership = CaseMembership.objects.create(
            case=self.case, user=self.operator
        )
        self.client.force_login(self.operator)

    def url(self, item, name="delete", case=None):
        kind = next(k for k, model in KINDS.items() if isinstance(item, model))
        return reverse(
            f"core:evidence:{name}",
            kwargs={
                "case_id": (case or item.case).pk,
                "kind": kind,
                "evidence_id": item.pk,
            },
        )

    def confirmation(self, item, url=None):
        response = self.client.get(url or self.url(item))
        return {"confirm": "on", "revision": response.context["deletion_revision"]}

    def test_superuser_can_delete_every_type_and_retained_call_files(self):
        for model in ALL_MODELS:
            with self.subTest(model=model.__name__):
                item = self.create_evidence(model)
                files = [item.file] if getattr(item, "file", None) else []
                kind = next(k for k, candidate in KINDS.items() if candidate is model)
                if kind in CALL_KINDS:
                    attachment = CallEvidenceAttachment.objects.create(
                        case=self.case,
                        uploaded_by=self.editor,
                        **{"phone_call" if kind == "phone-call" else "facetime": item},
                        transcript="Retained transcript",
                        file=SimpleUploadedFile("call.wav", b"Synthetic audio"),
                    )
                    files.append(attachment.file)
                detail = self.client.get(
                    self.url(item, "detail"), {"section": "details"}
                )
                self.assertContains(detail, self.url(item))
                response = self.client.get(self.url(item))
                self.assertContains(response, item.title)
                self.assertIn("private", response["Cache-Control"])
                self.assertIn("no-store", response["Cache-Control"])
                self.assertTrue(model.objects.filter(pk=item.pk).exists())
                with self.captureOnCommitCallbacks(execute=True):
                    response = self.client.post(self.url(item), self.confirmation(item))
                    for original in files:
                        self.assertTrue(original.storage.exists(original.name))
                self.assertRedirects(
                    response,
                    reverse("core:evidence:list", kwargs={"case_id": self.case.pk}),
                )
                self.assertFalse(model.objects.filter(pk=item.pk).exists())
                for original in files:
                    self.assertFalse(original.storage.exists(original.name))
                self.assertTrue(
                    LogEntry.objects.filter(
                        user=self.operator,
                        object_id=str(item.pk),
                        action_flag=DELETION,
                    ).exists()
                )

    def test_confirmation_is_required_and_cancel_preserves_filters(self):
        item = self.create_evidence(DocumentEvidence)
        suffix = "?q=harbour&type=document&state=all&page=2"
        url = self.url(item) + suffix
        response = self.client.post(url, {})
        self.assertEqual(response.status_code, 400)
        self.assertIn("confirm", response.context["form"].errors)
        self.assertEqual(
            response.context["cancel_url"],
            self.url(item, "detail") + suffix + "&info=1#evidence-details-heading",
        )
        self.assertTrue(item.file.storage.exists(item.file.name))
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(url, self.confirmation(item, url))
        self.assertRedirects(
            response,
            reverse("core:evidence:list", kwargs={"case_id": self.case.pk}) + suffix,
        )

    def test_archived_original_and_generated_files_are_deleted(self):
        item = self.create_evidence(
            DocumentEvidence,
            is_archived=True,
            archive_changed_at=timezone.now(),
            archive_changed_by=self.editor,
        )
        source_id = uuid4()
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.operator,
            operation="native_text",
            method_version="1",
            status="complete",
            inputs=[
                {"id": str(source_id), "kind": "document", "evidence_id": str(item.pk)}
            ],
        )
        result = AnalysisResult.objects.create(case=self.case, run=run)
        artifact = AnalysisArtifact.objects.create(
            case=self.case,
            result=result,
            source_id=source_id,
            file=SimpleUploadedFile("preview.jpg", b"Synthetic preview"),
            sha256="a" * 64,
            width=1,
            height=1,
        )
        self.assertContains(self.client.get(self.url(item)), "1 analysis run")
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(self.url(item), self.confirmation(item))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(AnalysisRun.objects.filter(pk=run.pk).exists())
        for original in (item.file, artifact.file):
            self.assertFalse(original.storage.exists(original.name))

    def test_ordinary_case_roles_and_staff_permissions_do_not_grant_delete(self):
        item = self.create_evidence()
        for user in (self.owner, self.editor, self.viewer):
            user.is_staff = True
            user.save(update_fields=["is_staff"])
            user.user_permissions.set(
                Permission.objects.filter(
                    content_type__app_label__in=(
                        "text",
                        "image",
                        "audio",
                        "document",
                        "geo",
                        "phone",
                    )
                )
            )
            self.client.force_login(user)
            self.assertNotContains(
                self.client.get(self.url(item, "detail"), {"section": "details"}),
                "Delete evidence",
            )
            for method in (self.client.get, self.client.post):
                self.assertEqual(
                    method(self.url(item), {"confirm": "on"}).status_code, 403
                )
        self.assertTrue(type(item).objects.filter(pk=item.pk).exists())

    def test_current_case_access_and_parent_case_are_required(self):
        item = self.create_evidence(DocumentEvidence)
        self.assertEqual(self.client.get(self.url(item)).status_code, 200)
        self.assertEqual(
            self.client.post(
                self.url(item, case=self.other_case), {"confirm": "on"}
            ).status_code,
            404,
        )
        other = self.create_evidence(DocumentEvidence, case=self.other_case)
        self.assertEqual(
            self.client.get(self.url(other, case=self.case)).status_code, 404
        )
        self.membership.delete()
        self.assertEqual(
            self.client.post(self.url(item), {"confirm": "on"}).status_code, 404
        )
        self.assertTrue(DocumentEvidence.objects.filter(pk=item.pk).exists())
        self.assertTrue(item.file.storage.exists(item.file.name))

    def test_authentication_verification_and_superuser_status_are_current(self):
        item = self.create_evidence()
        self.client.logout()
        self.assertEqual(
            self.client.post(self.url(item), {"confirm": "on"}).status_code, 302
        )
        self.client.force_login(self.operator)
        EmailAddress.objects.filter(user=self.operator).update(verified=False)
        self.assertRedirects(self.client.get(self.url(item)), reverse("account_email"))
        EmailAddress.objects.filter(user=self.operator).update(verified=True)
        self.operator.is_superuser = False
        self.operator.save(update_fields=["is_superuser"])
        self.assertEqual(
            self.client.post(self.url(item), {"confirm": "on"}).status_code, 403
        )
        self.operator.is_active = False
        self.operator.save(update_fields=["is_active"])
        self.assertEqual(
            self.client.post(self.url(item), {"confirm": "on"}).status_code, 302
        )
        self.assertTrue(type(item).objects.filter(pk=item.pk).exists())

    def test_csrf_and_allowed_methods(self):
        item = self.create_evidence()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.operator)
        self.assertEqual(
            client.post(self.url(item), {"confirm": "on"}).status_code, 403
        )
        self.assertEqual(self.client.put(self.url(item)).status_code, 405)
        self.assertTrue(type(item).objects.filter(pk=item.pk).exists())

    def test_import_source_confirmation_includes_and_deletes_the_import(self):
        item = self.create_evidence(DocumentEvidence)
        batch = VehicleLocationImport.objects.create(
            case=self.case,
            uploaded_by=item.uploaded_by,
            document=item,
            timezone="UTC",
        )
        response = self.client.get(self.url(item))
        self.assertContains(response, str(batch.pk))
        self.assertContains(response, "Permanently delete evidence")
        response = self.client.post(self.url(item), self.confirmation(item))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(DocumentEvidence.objects.filter(pk=item.pk).exists())

    def test_wrong_case_attachment_is_not_disclosed_or_deleted(self):
        item = self.create_evidence(KINDS["phone-call"])
        attachment = CallEvidenceAttachment.objects.create(
            case=self.case,
            phone_call=item,
            uploaded_by=self.editor,
            file=SimpleUploadedFile("foreign-case-original.wav", b"Synthetic audio"),
        )
        CallEvidenceAttachment.objects.filter(pk=attachment.pk).update(
            case=self.other_case
        )
        for method, status in ((self.client.get, 200), (self.client.post, 409)):
            response = method(self.url(item), {"confirm": "on"})
            self.assertContains(response, "inconsistent case links", status_code=status)
            self.assertNotContains(
                response, attachment.original_filename, status_code=status
            )
        self.assertTrue(
            CallEvidenceAttachment.objects.filter(pk=attachment.pk).exists()
        )
        self.assertTrue(attachment.file.storage.exists(attachment.file.name))

    def import_tree(self, count=2):
        original = self.create_evidence(
            DocumentEvidence,
            title="Retained imported source",
            file=SimpleUploadedFile("retained-export.csv", csv_content()),
        )
        batch = VehicleLocationImport.objects.create(
            case=self.case,
            uploaded_by=original.uploaded_by,
            document=original,
            timezone="UTC",
            row_count=count,
        )
        parsed = parse_csv(csv_content(), "UTC")[0]
        observations, staged = [], []
        for number in range(2, count + 2):
            observations.append(
                self.create_evidence(
                    VehicleLocationEvidence,
                    title=f"Imported observation {number}",
                    import_batch=batch,
                    source_row=number,
                    raw_row=parsed.raw,
                    **parsed.values,
                )
            )
            staged.append(
                VehicleLocationImportRow.objects.create(
                    batch=batch,
                    number=number,
                    group_index=0,
                    raw=parsed.raw,
                    fingerprint=parsed.fingerprint,
                )
            )
        message = self.create_evidence(KINDS["text-message"], title="Imported message")
        context = CommunicationContext.objects.create(
            case=self.case,
            message=message,
            source_document=original,
            source_sha256=original.sha256,
            source_row=1,
            source_start=0,
            source_end=1,
            parser_version="synthetic.v1",
            timezone="UTC",
            raw_sender="Synthetic account",
        )
        return original, batch, observations, staged, message, context

    def derived_analysis(self, item, kind, *, extra=None, prior=None, previous=None):
        selected = [
            {
                "id": str(uuid4()),
                "kind": kind,
                "evidence_id": str(item.pk),
                "sha256": item.sha256,
                "result_id": str(prior.pk) if prior else None,
            }
        ]
        if extra:
            selected.append(
                {
                    "id": str(uuid4()),
                    "kind": "location",
                    "evidence_id": str(extra.pk),
                    "sha256": extra.sha256,
                }
            )
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.operator,
            operation="location_quality" if "location" in kind else "entities",
            method_version="1",
            status="complete",
            inputs=selected,
            previous_run=previous,
        )
        result = AnalysisResult.objects.create(case=self.case, run=run)
        artifact = AnalysisArtifact.objects.create(
            case=self.case,
            result=result,
            source_id=selected[0]["id"],
            file=SimpleUploadedFile("derived.jpg", b"Synthetic derived image"),
            width=1,
            height=1,
            sha256="c" * 64,
        )
        return run, result, artifact

    def test_source_inventory_and_purge_cover_all_imported_evidence_and_files(self):
        source, batch, observations, staged, message, context = self.import_tree()
        retained_location = self.create_evidence(KINDS["location"])
        retained_file = self.create_evidence(
            DocumentEvidence,
            file=SimpleUploadedFile("retained-export.csv", csv_content()),
        )
        first = self.derived_analysis(
            observations[0], "vehicle-location", extra=retained_location
        )
        second = self.derived_analysis(message, "text-message")
        downstream = self.derived_analysis(
            retained_location, "location", prior=first[1]
        )
        final = self.derived_analysis(
            retained_location, "location", prior=downstream[1]
        )
        retained_run, retained_result, retained_artifact = self.derived_analysis(
            retained_location, "location", previous=first[0]
        )
        response = self.client.get(self.url(source) + "?download=manifest")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/octet-stream")
        self.assertEqual(
            response["Content-Disposition"],
            f'attachment; filename="deletion-{source.pk}.json"',
        )
        inventory = json.loads(b"".join(response.streaming_content))
        expected = [
            source,
            batch,
            *observations,
            *staged,
            message,
            context,
            *first,
            *second,
            *downstream,
            *final,
        ]
        deleted_ids = {
            row["id"]
            for row in inventory["entries"]
            if row["action"] == "Delete record"
        }
        self.assertEqual(deleted_ids, {str(obj.pk) for obj in expected})
        file_ids = {
            row["id"] for row in inventory["entries"] if row["action"] == "Delete file"
        }
        self.assertEqual(
            file_ids,
            {
                str(obj.pk)
                for obj in (source, first[2], second[2], downstream[2], final[2])
            },
        )
        self.assertEqual(
            inventory["counts"], {"records": len(expected), "files": 5, "updates": 1}
        )
        self.assertTrue(
            any(
                row["action"] == "Update" and row["id"] == str(retained_run.pk)
                for row in inventory["entries"]
            )
        )
        self.assertNotIn(source.file.name, json.dumps(inventory))
        self.assertNotIn("Original message.", json.dumps(inventory))
        files = [
            source.file,
            first[2].file,
            second[2].file,
            downstream[2].file,
            final[2].file,
        ]
        confirmation = self.confirmation(source)
        with (
            self.captureOnCommitCallbacks(execute=True),
            patch.object(
                deletion, "_analysis_runs", wraps=deletion._analysis_runs
            ) as scan,
        ):
            response = self.client.post(self.url(source), confirmation)
            for original in files:
                self.assertTrue(original.storage.exists(original.name))
        scan.assert_called_once()
        self.assertEqual(response.status_code, 302)
        for obj in expected:
            self.assertFalse(type(obj).objects.filter(pk=obj.pk).exists())
        for original in files:
            self.assertFalse(original.storage.exists(original.name))
        for obj in (
            self.person,
            self.vehicle,
            retained_file,
            retained_location,
            retained_run,
            retained_result,
            retained_artifact,
        ):
            self.assertTrue(type(obj).objects.filter(pk=obj.pk).exists())
        retained_run.refresh_from_db()
        self.assertIsNone(retained_run.previous_run_id)
        self.assertTrue(retained_file.file.storage.exists(retained_file.file.name))

    def test_changed_import_inventory_requires_a_fresh_confirmation(self):
        source, batch, _, _, _, _ = self.import_tree()
        old = self.confirmation(source)
        added = self.create_evidence(
            VehicleLocationEvidence,
            title="New imported observation",
            import_batch=batch,
            source_row=10,
        )
        with patch.object(
            deletion, "_analysis_runs", wraps=deletion._analysis_runs
        ) as scan:
            response = self.client.post(self.url(source), old)
        scan.assert_called_once()
        self.assertContains(
            response, "What will be removed has changed", status_code=409
        )
        self.assertContains(response, added.title, status_code=409)
        self.assertFalse(response.context["form"]["confirm"].value())
        self.assertTrue(DocumentEvidence.objects.filter(pk=source.pk).exists())
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                self.url(source),
                {
                    "confirm": "on",
                    "revision": response.context["form"]["revision"].value(),
                },
            )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(VehicleLocationEvidence.objects.filter(pk=added.pk).exists())

    def test_import_inventory_requires_new_confirmation_after_important_note(self):
        source, _, _, _, message, _ = self.import_tree()
        self.client.force_login(self.editor)
        mark_url = reverse(
            "core:important:mark_evidence",
            args=[self.case.pk, "text-message", message.pk],
        )
        self.assertEqual(
            self.client.post(mark_url, {"action": "mark"}).status_code, 302
        )
        item = ImportantItem.objects.get(case=self.case, evidence_id=message.pk)

        self.client.force_login(self.operator)
        confirmation = self.confirmation(source)
        self.client.force_login(self.editor)
        notes_url = reverse("core:important:notes", args=[self.case.pk, item.pk])
        body = "Compare with the original message annotation."
        self.assertEqual(self.client.post(notes_url, {"body": body}).status_code, 302)
        note = ImportantNote.objects.get(item=item)

        self.client.force_login(self.operator)
        response = self.client.post(self.url(source), confirmation)
        self.assertContains(response, deletion.CHANGED, status_code=409)
        self.assertTrue(type(message).objects.filter(pk=message.pk).exists())
        export = self.client.get(self.url(source) + "?download=manifest")
        manifest = b"".join(export.streaming_content)
        ids = {row["id"] for row in json.loads(manifest)["entries"]}
        self.assertTrue({str(item.pk), str(note.pk)} <= ids)
        self.assertNotIn(body.encode(), manifest)

        response = self.client.post(self.url(source), self.confirmation(source))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(ImportantItem.objects.filter(pk=item.pk).exists())
        self.assertFalse(ImportantNote.objects.filter(pk=note.pk).exists())

    def test_confirmation_cannot_be_omitted_tampered_or_reused_for_another_target(self):
        first = self.create_evidence(DocumentEvidence)
        second = self.create_evidence(DocumentEvidence)
        for data, status in (
            ({"confirm": "on"}, 400),
            ({"confirm": "on", "revision": "tampered"}, 409),
            (self.confirmation(first), 409),
        ):
            response = self.client.post(self.url(second), data)
            self.assertEqual(response.status_code, status)
            self.assertTrue(DocumentEvidence.objects.filter(pk=second.pk).exists())
        self.assertTrue(second.file.storage.exists(second.file.name))

    def test_complete_inventory_is_paginated_without_hiding_rows(self):
        source, _, observations, staged, _, _ = self.import_tree(count=60)
        first = self.client.get(self.url(source) + "?q=retained&state=all&page=2")
        self.assertEqual(len(first.context["deletion_page"]), 50)
        self.assertIn("q=retained", first.context["deletion_links"]["next"])
        self.assertIn("page=2", first.context["deletion_links"]["next"])
        self.assertEqual(first.context["deletion_page"].paginator.count, 125)
        with patch("core.evidence.record_views.plan_deletion") as rebuild:
            second = self.client.get(
                self.url(source) + first.context["deletion_links"]["next"]
            )
        rebuild.assert_not_called()
        self.assertEqual(second.context["deletion_page"].number, 2)
        self.assertEqual(len(second.context["deletion_page"]), 50)
        export = self.client.get(self.url(source) + "?download=manifest")
        inventory = json.loads(b"".join(export.streaming_content))
        self.assertEqual(len(inventory["entries"]), 125)
        ids = {row["id"] for row in inventory["entries"]}
        self.assertTrue({str(obj.pk) for obj in [*observations, *staged]} <= ids)
        self.assertIn("no-store", export["Cache-Control"])

    def test_active_usage_change_restarts_paginated_review(self):
        source, *_ = self.import_tree(count=60)
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.editor,
            operation="extract_text",
            method_version="1",
            status=AnalysisRun.Status.RUNNING,
            inputs=[{"kind": "document", "evidence_id": str(source.pk)}],
        )
        first = self.client.get(self.url(source))
        GatewayRequest.objects.create(
            case=self.case, run=run, model="synthetic", request_bytes=64
        )
        second = self.client.get(
            self.url(source) + first.context["deletion_links"]["next"]
        )
        self.assertContains(second, "What will be removed has changed", status_code=409)
        self.assertEqual(second.context["deletion_page"].number, 1)

    def test_analysis_publication_restarts_paginated_review(self):
        source = self.create_evidence(body="Synthetic retained source. " * 100)
        AnalysisRun.objects.bulk_create(
            AnalysisRun(
                case=self.case,
                requested_by=self.editor,
                operation="entities",
                method_version="1",
                inputs=[{"kind": "text", "evidence_id": str(source.pk)}],
            )
            for _ in range(55)
        )
        url = self.url(source)
        first = self.client.get(url)
        self.assertEqual(first.context["deletion_page"].paginator.count, 56)

        def publish_before_cache_lookup(*args):
            AnalysisRun.objects.create(
                case=self.case,
                requested_by=self.editor,
                operation="entities",
                method_version="1",
                inputs=[{"kind": "text", "evidence_id": str(source.pk)}],
            )
            return deletion.load_deletion_review(*args)

        with patch(
            "core.evidence.record_views.load_deletion_review",
            publish_before_cache_lookup,
        ):
            second = self.client.get(url + first.context["deletion_links"]["next"])
        self.assertContains(second, "What will be removed has changed", status_code=409)
        self.assertEqual(second.context["deletion_page"].number, 1)
        self.assertGreater(second.context["deletion_page"].paginator.count, 56)

    def test_foreign_case_import_links_block_inventory_and_all_deletion(self):
        for part in ("batch", "observation", "context", "message"):
            with self.subTest(part=part):
                source, batch, observations, _, message, context = self.import_tree(
                    count=1
                )
                target = {
                    "batch": batch,
                    "observation": observations[0],
                    "context": context,
                    "message": message,
                }[part]
                type(target).objects.filter(pk=target.pk).update(case=self.other_case)
                response = self.client.get(self.url(source))
                self.assertContains(response, "inconsistent case links")
                self.assertNotContains(response, str(target.pk))
                self.assertNotContains(
                    response, "Download the complete deletion inventory"
                )
                response = self.client.post(
                    self.url(source), {"confirm": "on", "revision": "invalid"}
                )
                self.assertEqual(response.status_code, 409)
                self.assertTrue(DocumentEvidence.objects.filter(pk=source.pk).exists())
                self.assertTrue(type(target).objects.filter(pk=target.pk).exists())
                self.assertTrue(source.file.storage.exists(source.file.name))

    def test_import_purge_rollback_preserves_every_record_and_file(self):
        source, batch, observations, staged, message, context = self.import_tree()
        run, result, artifact = self.derived_analysis(message, "text-message")
        data = self.confirmation(source)
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            with transaction.atomic():
                self.assertEqual(
                    self.client.post(self.url(source), data).status_code, 302
                )
                transaction.set_rollback(True)
        self.assertEqual(callbacks, [])
        for obj in [
            source,
            batch,
            *observations,
            *staged,
            message,
            context,
            run,
            result,
            artifact,
        ]:
            self.assertTrue(type(obj).objects.filter(pk=obj.pk).exists())
        for original in (source.file, artifact.file):
            self.assertTrue(original.storage.exists(original.name))

    def test_import_workers_cannot_recreate_deleted_source_derivatives(self):
        for phase in ("prepare", "publish"):
            with self.subTest(phase=phase):
                source, batch, observations, _, _, _ = self.import_tree(count=1)
                VehicleLocationImport.objects.filter(pk=batch.pk).update(
                    status="queued", phase=phase, requested_by=self.editor
                )
                row = parse_csv(csv_content(), "UTC")[0]

                def remove_source_during_parsing(
                    *args, source=source, row=row, **kwargs
                ):
                    self.assertEqual(
                        self.client.post(
                            self.url(source), self.confirmation(source)
                        ).status_code,
                        302,
                    )
                    yield row

                with (
                    self.captureOnCommitCallbacks(execute=True),
                    patch(
                        "geo.vehicle_import_jobs.iter_import",
                        side_effect=remove_source_during_parsing,
                    ),
                ):
                    process_import(str(batch.pk), str(batch.job_token))
                self.assertFalse(DocumentEvidence.objects.filter(pk=source.pk).exists())
                self.assertFalse(
                    VehicleLocationImport.objects.filter(pk=batch.pk).exists()
                )
                self.assertFalse(
                    VehicleLocationEvidence.objects.filter(
                        import_batch_id=batch.pk
                    ).exists()
                )
                self.assertFalse(source.file.storage.exists(source.file.name))
                self.assertEqual(
                    process_import(str(batch.pk), str(batch.job_token)), "unavailable"
                )

    def test_admin_also_reviews_and_removes_imported_evidence(self):
        source, batch, observations, staged, message, context = self.import_tree()
        self.operator.is_staff = True
        self.operator.save(update_fields=["is_staff"])
        url = reverse("admin:document_documentevidence_delete", args=[source.pk])
        response = self.client.get(url)
        self.assertContains(response, "Deletion summary")
        self.assertContains(response, str(self.case))
        self.assertNotContains(response, f">{message.title}<")
        self.assertNotContains(response, observations[0].title)
        self.assertNotContains(response, str(staged[0].pk))
        manifest = self.client.get(url + response.context["deletion_download_url"])
        inventory = json.loads(b"".join(manifest.streaming_content))
        ids = {entry["id"] for entry in inventory["entries"]}
        self.assertTrue(
            {str(message.pk), str(observations[0].pk), str(staged[0].pk)} <= ids
        )
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                url, {"post": "yes", "revision": response.context["deletion_revision"]}
            )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(DocumentEvidence.objects.filter(pk=source.pk).exists())
        self.assertFalse(VehicleLocationImport.objects.filter(pk=batch.pk).exists())
        self.assertFalse(type(message).objects.filter(pk=message.pk).exists())
        self.assertFalse(source.file.storage.exists(source.file.name))

    def test_new_analysis_files_require_another_inventory_review(self):
        source, _, _, _, message, _ = self.import_tree()
        old = self.confirmation(source)
        run, result, artifact = self.derived_analysis(message, "text-message")
        response = self.client.post(self.url(source), old)
        self.assertContains(
            response, "What will be removed has changed", status_code=409
        )
        self.assertContains(response, str(artifact.pk), status_code=409)
        self.assertEqual(response.context["deletion_plan"].counts["files"], 2)
        self.assertTrue(AnalysisRun.objects.filter(pk=run.pk).exists())
        self.assertTrue(artifact.file.storage.exists(artifact.file.name))

    def test_reviewed_inventory_is_bound_to_the_current_superuser(self):
        source = self.create_evidence(DocumentEvidence)
        other_actor_data = self.confirmation(source)
        self.viewer.is_superuser = True
        self.viewer.save(update_fields=["is_superuser"])
        self.client.force_login(self.viewer)
        response = self.client.post(self.url(source), other_actor_data)
        self.assertEqual(response.status_code, 409)
        self.assertTrue(DocumentEvidence.objects.filter(pk=source.pk).exists())
        self.assertTrue(source.file.storage.exists(source.file.name))

    def test_inventory_changes_during_pagination_or_download_restart_review(self):
        source, batch, _, _, _, _ = self.import_tree(count=60)
        first = self.client.get(self.url(source))
        next_url = self.url(source) + first.context["deletion_links"]["next"]
        old_download = self.url(source) + first.context["deletion_download_url"]
        self.create_evidence(
            VehicleLocationEvidence, import_batch=batch, source_row=100
        )
        for url in (next_url, old_download):
            response = self.client.get(url)
            self.assertContains(
                response, "What will be removed has changed", status_code=409
            )
            self.assertFalse(response.streaming)
            self.assertEqual(response.context["deletion_page"].number, 1)
            self.assertFalse(response.context["form"]["confirm"].value())
            self.assertTrue(DocumentEvidence.objects.filter(pk=source.pk).exists())
        response = self.client.get(
            self.url(source) + response.context["deletion_download_url"]
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            len(json.loads(b"".join(response.streaming_content))["entries"]), 126
        )

    def test_noncanonical_file_keys_never_expose_or_remove_foreign_files(self):
        source = self.create_evidence(DocumentEvidence)
        foreign = self.create_evidence(
            DocumentEvidence,
            case=self.other_case,
            file=SimpleUploadedFile(
                "foreign-private.txt", b"Synthetic foreign original"
            ),
        )
        legitimate_name = source.file.name
        foreign_suffix = foreign.file.name.removeprefix("evidence/cases/")
        unsafe = [
            f"evidence/cases/{self.case.pk}/../{foreign_suffix}",
            legitimate_name.replace("/documentevidence/", "/./documentevidence/"),
            legitimate_name.replace("/documentevidence/", "//documentevidence/"),
            legitimate_name.replace("/", "\\"),
        ]
        for name in unsafe:
            with self.subTest(name=name):
                DocumentEvidence.objects.filter(pk=source.pk).update(file=name)
                with patch.object(source.file.storage, "delete") as storage_delete:
                    response = self.client.get(self.url(source))
                    self.assertContains(response, "inconsistent case links")
                    self.assertNotContains(response, foreign.original_filename)
                    response = self.client.post(
                        self.url(source), {"confirm": "on", "revision": "invalid"}
                    )
                    self.assertEqual(response.status_code, 409)
                    storage_delete.assert_not_called()
                self.assertTrue(DocumentEvidence.objects.filter(pk=source.pk).exists())
                self.assertTrue(source.file.storage.exists(legitimate_name))
                self.assertTrue(foreign.file.storage.exists(foreign.file.name))
