"""Debug-only bulk deletion retains the confirmed, case-scoped deletion contract."""

import json
from unittest.mock import patch
from uuid import uuid4

from django.contrib.admin.models import DELETION, LogEntry
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from cases.models import CaseMembership
from core.evidence.deletion import CHANGED, execute_deletion
from core.models import AnalysisRun
from core.testing import ALL_MODELS, EvidenceTestMixin
from document.models import DocumentEvidence
from geo.models import VehicleLocationImport
from phone.models import CallEvidenceAttachment, PhoneCallEvidence


@override_settings(DEBUG=True)
class DebugBulkEvidenceDeletionTests(EvidenceTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.operator = get_user_model().objects.create_superuser(
            username="operator", email="operator@example.com", password=None
        )
        self.membership = CaseMembership.objects.create(
            case=self.case, user=self.operator
        )
        self.client.force_login(self.operator)

    def submit(self, items, **extra):
        model = type(items[0])
        return self.client.post(
            reverse(
                f"admin:{model._meta.app_label}_{model._meta.model_name}_changelist"
            ),
            {
                "action": "delete_selected",
                "_selected_action": [str(item.pk) for item in items],
                **extra,
            },
        )

    def confirmation(self, items):
        response = self.submit(items)
        self.assertEqual(response.status_code, 200)
        return {
            "revision": response.context["revisions"],
            "post": "yes",
            "confirm": "yes",
        }

    def test_every_evidence_type_requires_review_and_deletes_only_selected(self):
        for model in ALL_MODELS:
            with self.subTest(model=model.__name__):
                items = [
                    self.create_evidence(
                        model,
                        is_archived=True,
                        archive_changed_at=timezone.now(),
                        archive_changed_by=self.operator,
                    )
                    for _ in range(2)
                ]
                ids = [item.pk for item in items]
                retained = self.create_evidence(model)
                files = [item.file for item in items if getattr(item, "file", None)]
                confirmation = self.confirmation(items)
                self.assertEqual(model.objects.filter(pk__in=ids).count(), 2)
                with self.captureOnCommitCallbacks(execute=True):
                    response = self.submit(items, **confirmation)
                    for original in files:
                        self.assertTrue(original.storage.exists(original.name))
                self.assertEqual(response.status_code, 302)
                self.assertFalse(model.objects.filter(pk__in=ids).exists())
                self.assertTrue(model.objects.filter(pk=retained.pk).exists())
                for original in files:
                    self.assertFalse(original.storage.exists(original.name))
                self.assertEqual(
                    LogEntry.objects.filter(
                        user=self.operator,
                        action_flag=DELETION,
                        object_id__in=[str(pk) for pk in ids],
                    ).count(),
                    2,
                )

    def test_debug_false_blocks_both_initial_and_already_confirmed_requests(self):
        items = [self.create_evidence() for _ in range(2)]
        confirmation = self.confirmation(items)
        with self.settings(DEBUG=False):
            for data in ({}, confirmation):
                response = self.submit(items, **data)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(
                    response, "Delete selected evidence (DEBUG only)"
                )
        self.assertEqual(type(items[0]).objects.count(), 2)

    def test_staff_permissions_do_not_grant_bulk_delete(self):
        items = [self.create_evidence()]
        for user in (self.owner, self.editor, self.viewer, self.outsider):
            with self.subTest(user=user):
                user.is_staff = True
                user.save(update_fields=["is_staff"])
                user.user_permissions.set(
                    Permission.objects.filter(content_type__app_label="text")
                )
                self.client.force_login(user)
                response = self.submit(items, post="yes", confirm="yes")
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(
                    response, "Delete selected evidence (DEBUG only)"
                )
        self.assertTrue(type(items[0]).objects.filter(pk=items[0].pk).exists())

    def test_missing_or_forged_confirmation_and_changed_selection_require_review(self):
        items = [self.create_evidence() for _ in range(2)]
        confirmation = self.confirmation(items)
        for extra in (
            {"post": "yes", "confirm": "yes"},
            {**confirmation, "revision": ["forged"]},
        ):
            response = self.submit(items, **extra)
            self.assertContains(response, CHANGED, status_code=409)
        self.assertEqual(
            self.submit(items, **{**confirmation, "confirm": ""}).status_code, 200
        )
        response = self.submit(items[:1], **confirmation)
        self.assertContains(response, CHANGED, status_code=409)
        self.assertEqual(type(items[0]).objects.count(), 2)

    def test_shared_analysis_is_inventoried_once_and_new_analysis_requires_review(self):
        items = [self.create_evidence() for _ in range(2)]
        inputs = [
            {"id": str(uuid4()), "kind": "text", "evidence_id": str(item.pk)}
            for item in items
        ]
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.operator,
            operation="native_text",
            inputs=inputs,
        )
        response = self.submit(items)
        entries = response.context["deletion_plan"]["counts"]
        self.assertEqual(entries["records"], 3)
        confirmation = self.confirmation(items)
        newer = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.operator,
            operation="native_text",
            inputs=inputs,
        )
        response = self.submit(items, **confirmation)
        self.assertContains(response, CHANGED, status_code=409)
        self.assertTrue(AnalysisRun.objects.filter(pk=run.pk).exists())
        self.submit(items, **self.confirmation(items))
        self.assertFalse(AnalysisRun.objects.filter(pk__in=[run.pk, newer.pk]).exists())

    def test_revoked_case_changes_inventory_without_partial_deletion(self):
        CaseMembership.objects.create(case=self.other_case, user=self.operator)
        items = [self.create_evidence(), self.create_evidence(case=self.other_case)]
        confirmation = self.confirmation(items)
        self.membership.delete()
        response = self.submit(items, **confirmation)
        self.assertContains(response, CHANGED, status_code=409)
        self.assertNotContains(response, str(items[0].pk), status_code=409)
        self.assertEqual(type(items[0]).objects.count(), 2)

    def test_foreign_case_is_not_disclosed_or_deleted(self):
        item = self.create_evidence()
        hidden = self.create_evidence(
            case=self.other_case, title="Hidden foreign source"
        )
        response = self.submit([item, hidden])
        self.assertNotContains(response, hidden.title)
        self.assertNotContains(response, str(hidden.pk))
        self.submit([item, hidden], **self.confirmation([item, hidden]))
        self.assertTrue(type(hidden).objects.filter(pk=hidden.pk).exists())

    def test_cross_case_failure_rolls_back_records_history_and_files(self):
        CaseMembership.objects.create(case=self.other_case, user=self.operator)
        items = [
            self.create_evidence(DocumentEvidence),
            self.create_evidence(DocumentEvidence, case=self.other_case),
        ]
        confirmation = self.confirmation(items)
        with (
            patch("core.evidence.admin.execute_deletion") as execute,
            self.captureOnCommitCallbacks(execute=True) as callbacks,
        ):
            # Execute the first plan for real before failing the second case.
            def fail_second(plan, actor):
                if execute.call_count == 2:
                    raise RuntimeError("Synthetic failure")
                return execute_deletion(plan, actor)

            execute.side_effect = fail_second
            with self.assertRaisesMessage(RuntimeError, "Synthetic failure"):
                self.submit(items, **confirmation)
        self.assertEqual(callbacks, [])
        self.assertEqual(DocumentEvidence.objects.count(), 2)
        self.assertFalse(LogEntry.objects.filter(action_flag=DELETION).exists())
        for item in items:
            self.assertTrue(item.file.storage.exists(item.file.name))

    def test_inconsistent_cascade_blocks_whole_batch_without_private_labels(self):
        items = [self.create_evidence(PhoneCallEvidence) for _ in range(2)]
        attachment = CallEvidenceAttachment.objects.create(
            case=self.case,
            phone_call=items[0],
            uploaded_by=self.editor,
            file=SimpleUploadedFile("hidden.wav", b"Synthetic audio"),
        )
        confirmation = self.confirmation(items)
        CallEvidenceAttachment.objects.filter(pk=attachment.pk).update(
            case=self.other_case
        )
        response = self.submit(items, **confirmation)
        self.assertContains(response, "inconsistent case links", status_code=409)
        self.assertNotContains(response, attachment.original_filename, status_code=409)
        self.assertEqual(type(items[0]).objects.count(), 2)

    def test_import_dependents_manifest_pagination_and_select_across(self):
        items = [self.create_evidence(DocumentEvidence) for _ in range(26)]
        batch = VehicleLocationImport.objects.create(
            case=self.case, uploaded_by=self.editor, document=items[0], timezone="UTC"
        )
        confirmation = self.confirmation(items)
        response = self.submit(
            items, revision=confirmation["revision"], review_page="2"
        )
        self.assertEqual(response.context["deletion_page"].number, 2)
        response = self.submit(
            items, revision=confirmation["revision"], download="manifest"
        )
        inventory = json.loads(b"".join(response.streaming_content))
        self.assertEqual(inventory["counts"], {"files": 26, "records": 27})
        self.assertEqual(len(inventory["entries"]), 53)
        self.assertIn(str(batch.pk), {entry["id"] for entry in inventory["entries"]})
        self.assertIn("private", response["Cache-Control"])
        self.assertIn("no-store", response["Cache-Control"])
        response = self.submit(items[:1], select_across="1")
        self.assertEqual(len(response.context["items"]), 26)
        self.assertEqual(len(response.context["selected_items"]), 1)
        self.submit(
            items[:1],
            select_across="1",
            **{**confirmation, "revision": response.context["revisions"]},
        )
        self.assertFalse(DocumentEvidence.objects.exists())
        self.assertFalse(VehicleLocationImport.objects.exists())

    def test_csrf_is_required(self):
        item = self.create_evidence()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.operator)
        response = client.post(
            reverse("admin:text_textevidence_changelist"),
            {"action": "delete_selected", "_selected_action": str(item.pk)},
        )
        self.assertEqual(response.status_code, 403)
