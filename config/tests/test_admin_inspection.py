from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from django.apps import apps
from django.conf import settings
from django.contrib import admin
from django.contrib.admin.models import DELETION, LogEntry
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from cases.models import Case, CaseInvitation, CaseMembership
from core.models import (
    AnalysisResult,
    AnalysisRun,
    CaseAnalysisBatch,
    CaseSearchIndex,
    ChatAnalysisProposal,
    ChatMatch,
    ChatSource,
    ChatTurn,
    EvidenceChat,
    GatewayRequest,
    ImportantItem,
    ImportantNote,
)
from document.models import DocumentEvidence
from geo.trip_models import VehicleTripState
from people.models import Person
from phone.models import (
    ConversationGrouping,
    ConversationSession,
    ConversationSessionMember,
    TextMessageEvidence,
)
from socialmedia.models import SocialAccount
from text.models import TextEvidence
from vehicle.models import Vehicle


def admin_url(model, action, obj=None):
    return reverse(
        f"admin:{model._meta.app_label}_{model._meta.model_name}_{action}",
        args=[obj.pk] if obj is not None else None,
    )


class AdminInspectionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        user_model = get_user_model()
        cls.owner = user_model.objects.create_user(username="owner", is_staff=True)
        cls.viewer = user_model.objects.create_user(username="viewer", is_staff=True)
        cls.operator = user_model.objects.create_superuser(username="operator")
        cls.case = Case.objects.create(owner=cls.owner, name="Visible case")
        cls.other_case = Case.objects.create(owner=cls.operator, name="Other case")
        cls.membership = CaseMembership.objects.create(case=cls.case, user=cls.viewer)
        permissions = Permission.objects.filter(
            content_type__model__in=("caseinvitation", "analysisrun", "analysisresult")
        )
        cls.owner.user_permissions.set(permissions)
        cls.viewer.user_permissions.set(permissions)
        cls.invitation = CaseInvitation.objects.create(
            case=cls.case,
            email="invitee@example.com",
            invited_by=cls.owner,
            expires_at=timezone.now() + timedelta(days=1),
            token_digest=uuid4().hex * 2,
        )
        cls.analysis_run = AnalysisRun.objects.create(
            case=cls.case,
            requested_by=cls.owner,
            operation="example-analysis",
            method_version="v1",
            parameters={"private": "synthetic-hidden-parameter"},
        )
        cls.chat = EvidenceChat.objects.create(
            case=cls.case, created_by=cls.owner, title="Synthetic chat question"
        )
        cls.turn = ChatTurn.objects.create(
            case=cls.case,
            chat=cls.chat,
            asked_by=cls.owner,
            ordinal=1,
            question=cls.chat.title,
            submission_id=uuid4(),
            status=ChatTurn.Status.COMPLETE,
        )

    def test_project_models_have_an_admin_or_an_intentional_workflow(self):
        without_standalone_admin = {
            "cases.CaseMembership",  # Case inline
            "core.ChatAnalysisProposal",  # Reviewed in Evidence chat
            "core.ChatSelectionMember",  # Source-bound matching records
            "core.AnalysisReportIndex",  # Derived report discovery and validation cache
            "core.ExtractionReuse",  # Extraction submission attribution
            "core.ChatRoutingApproval",  # Owner approval in Case Settings
            "core.DeletionReview",  # Evidence confirmation
            "core.PendingFileDeletion",  # File cleanup command
            "geo.VehicleLocationImportRow",  # Paginated import review
            "geo.VehicleTripInput",  # Trip observation provenance
            "geo.VehicleTripSource",  # Trip observation provenance
            "phone.CallEvidenceAttachment",  # Call inlines
            "phone.CallHistoryImport",  # Case import review and Processing
            "phone.CallHistoryRow",  # Paginated import review
            "phone.CallImportSource",  # Case import source chooser
            "phone.CallPersonLink",  # Attributed links in the call reader
            "phone.ConversationPairRevision",  # Conversation processing marker
            "phone.ConversationSessionMember",  # Paginated session reader
            "socialmedia.InstagramConnection",  # Personal account settings
        }
        self.assertEqual(
            {
                model._meta.label
                for app in apps.get_app_configs()
                if Path(app.path).parent == settings.BASE_DIR
                for model in app.get_models()
                if not admin.site.is_registered(model)
            },
            without_standalone_admin,
        )

    def test_invitation_inspection_is_owner_only_and_hides_token(self):
        listing = admin_url(CaseInvitation, "changelist")
        detail = admin_url(CaseInvitation, "change", self.invitation)
        self.client.force_login(self.owner)
        self.assertContains(
            self.client.get(listing, {"role__exact": "viewer"}), self.invitation.email
        )
        response = self.client.get(detail)
        self.assertContains(response, self.invitation.email)
        self.assertNotContains(response, self.invitation.token_digest)
        self.assertNotContains(response, 'name="_save"')
        self.assertEqual(
            self.client.get(listing, {"token_digest__startswith": "a"}).status_code,
            400,
        )

        for user in (self.viewer, self.operator):
            with self.subTest(user=user):
                self.client.force_login(user)
                self.assertNotContains(self.client.get(listing), self.invitation.email)
                for action in ("change", "history"):
                    self.assertEqual(
                        self.client.get(
                            admin_url(CaseInvitation, action, self.invitation)
                        ).status_code,
                        302,
                    )

    def test_analysis_inspection_requires_current_access_and_model_permission(self):
        listing = admin_url(AnalysisRun, "changelist")
        detail = admin_url(AnalysisRun, "change", self.analysis_run)
        self.client.force_login(self.viewer)
        self.assertContains(
            self.client.get(listing, {"q": str(self.analysis_run.pk)}),
            self.analysis_run.operation,
        )
        response = self.client.get(detail)
        self.assertContains(response, self.analysis_run.operation)
        self.assertNotContains(response, "synthetic-hidden-parameter")
        self.assertFalse(response.context["has_change_permission"])
        self.viewer.user_permissions.clear()
        self.assertEqual(self.client.get(detail).status_code, 403)
        self.viewer.user_permissions.set(
            Permission.objects.filter(codename="view_analysisrun")
        )
        self.membership.delete()
        self.assertNotContains(self.client.get(listing), self.analysis_run.operation)
        self.assertEqual(self.client.get(detail).status_code, 302)

    def test_inspection_never_allows_direct_mutations(self):
        self.client.force_login(self.owner)
        for action, obj in (
            ("add", None),
            ("change", self.analysis_run),
            ("delete", self.analysis_run),
        ):
            with self.subTest(action=action):
                self.assertEqual(
                    self.client.post(
                        admin_url(AnalysisRun, action, obj), {"post": "yes"}
                    ).status_code,
                    403,
                )
        self.assertTrue(AnalysisRun.objects.filter(pk=self.analysis_run.pk).exists())

    def test_inconsistent_parent_relationship_is_excluded(self):
        result = AnalysisResult.objects.create(case=self.case, run=self.analysis_run)
        AnalysisResult.objects.filter(pk=result.pk).update(case=self.other_case)
        self.client.force_login(self.operator)
        listing = self.client.get(admin_url(AnalysisResult, "changelist"))
        self.assertEqual(list(listing.context["cl"].queryset), [])
        self.assertEqual(
            self.client.get(admin_url(AnalysisResult, "change", result)).status_code,
            302,
        )

    def grant_chat_admin_permissions(self, user):
        user.user_permissions.add(
            *Permission.objects.filter(
                content_type__app_label="core",
                codename__in=("view_evidencechat", "delete_evidencechat"),
            )
        )

    def test_chat_delete_removes_saved_replies_and_keeps_cited_evidence(self):
        evidence = TextEvidence.objects.create(
            case=self.case, uploaded_by=self.owner, title="Example", body="Example text"
        )
        source = ChatSource.objects.create(
            case=self.case,
            turn=self.turn,
            key="source",
            locator={},
            evidence_kind="text",
            evidence_id=evidence.pk,
        )
        match = ChatMatch.objects.create(
            case=self.case,
            turn=self.turn,
            source=source,
            start=0,
            end=7,
            quote="Example",
        )
        usage = GatewayRequest.objects.create(
            case=self.case,
            chat_turn=self.turn,
            operation=GatewayRequest.Operation.EVIDENCE_CHAT,
            model="synthetic-model",
            request_bytes=1,
        )
        mark = ImportantItem.objects.create(
            case=self.case,
            chat_turn=self.turn,
            marked_by=self.owner,
            marked_at=timezone.now(),
        )
        note = ImportantNote.objects.create(
            item=mark, author=self.owner, body="Example note"
        )
        other_chat = EvidenceChat.objects.create(
            case=self.case, created_by=self.owner, title="Another saved chat"
        )
        self.grant_chat_admin_permissions(self.owner)
        self.client.force_login(self.owner)
        delete_url = admin_url(EvidenceChat, "delete", self.chat)

        response = self.client.get(admin_url(EvidenceChat, "change", self.chat))
        self.assertContains(response, f'href="{delete_url}"')
        self.assertNotContains(response, 'name="_save"')
        response = self.client.get(delete_url)
        self.assertContains(response, "Chat reply 1")
        self.assertContains(response, str(note))
        self.assertTrue(EvidenceChat.objects.filter(pk=self.chat.pk).exists())

        self.assertRedirects(
            self.client.post(delete_url, {"post": "yes"}),
            reverse("admin:index"),
        )
        for obj in (self.chat, self.turn, source, match, usage, mark, note):
            self.assertFalse(type(obj).objects.filter(pk=obj.pk).exists())
        self.assertTrue(TextEvidence.objects.filter(pk=evidence.pk).exists())
        self.assertTrue(EvidenceChat.objects.filter(pk=other_chat.pk).exists())
        entry = LogEntry.objects.get(object_id=str(self.chat.pk), action_flag=DELETION)
        self.assertEqual(entry.object_repr, str(self.chat.pk))

    def test_chat_delete_requires_model_permission_and_case_ownership(self):
        delete_url = admin_url(EvidenceChat, "delete", self.chat)
        change_url = admin_url(EvidenceChat, "change", self.chat)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.post(delete_url, {"post": "yes"}).status_code, 403)

        self.owner.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="core", codename="delete_evidencechat"
            )
        )
        self.assertEqual(self.client.get(delete_url).status_code, 403)
        self.assertEqual(self.client.post(delete_url, {"post": "yes"}).status_code, 403)

        self.grant_chat_admin_permissions(self.viewer)
        self.client.force_login(self.viewer)
        for role in (CaseMembership.Role.VIEWER, CaseMembership.Role.EDITOR):
            with self.subTest(role=role):
                self.membership.role = role
                self.membership.save(update_fields=["role"])
                self.assertNotContains(
                    self.client.get(change_url), f'href="{delete_url}"'
                )
                self.assertEqual(
                    self.client.post(delete_url, {"post": "yes"}).status_code, 403
                )
        self.membership.delete()
        self.assertEqual(self.client.post(delete_url, {"post": "yes"}).status_code, 302)

        self.client.force_login(self.operator)
        self.assertEqual(self.client.post(delete_url, {"post": "yes"}).status_code, 302)
        self.assertTrue(EvidenceChat.objects.filter(pk=self.chat.pk).exists())

    def test_chat_delete_checks_for_active_replies_on_confirmation(self):
        self.grant_chat_admin_permissions(self.owner)
        self.client.force_login(self.owner)
        delete_url = admin_url(EvidenceChat, "delete", self.chat)
        self.assertContains(self.client.get(delete_url), 'name="post"')
        for status in (ChatTurn.Status.QUEUED, ChatTurn.Status.RUNNING):
            with self.subTest(status=status):
                ChatTurn.objects.filter(pk=self.turn.pk).update(status=status)
                response = self.client.post(delete_url, {"post": "yes"})
                self.assertContains(
                    response, "Wait for the current reply or approved extraction"
                )
                self.assertNotContains(response, 'name="post"')
                self.assertTrue(EvidenceChat.objects.filter(pk=self.chat.pk).exists())

    def test_chat_delete_waits_for_approved_extraction_and_keeps_its_run(self):
        media_root = self.enterContext(TemporaryDirectory())
        self.enterContext(self.settings(MEDIA_ROOT=media_root))
        document = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Example PDF",
            file=SimpleUploadedFile("example.pdf", b"%PDF-1.7\n%%EOF\n"),
        )
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.owner,
            operation="native_text",
            method_version="example",
        )
        proposal = ChatAnalysisProposal.objects.create(
            parent=self.turn,
            document=document,
            sha256=document.sha256,
            method_version=run.method_version,
            status=ChatAnalysisProposal.Status.APPROVED,
            approved_by=self.owner,
            run=run,
        )
        self.grant_chat_admin_permissions(self.owner)
        self.client.force_login(self.owner)
        delete_url = admin_url(EvidenceChat, "delete", self.chat)

        response = self.client.post(delete_url, {"post": "yes"})
        self.assertContains(
            response, "Wait for the current reply or approved extraction"
        )
        self.assertNotContains(response, 'name="post"')
        self.assertTrue(EvidenceChat.objects.filter(pk=self.chat.pk).exists())

        run.status = AnalysisRun.Status.FAILED
        run.save(update_fields=("status",))
        self.assertRedirects(
            self.client.post(delete_url, {"post": "yes"}), reverse("admin:index")
        )
        self.assertFalse(ChatAnalysisProposal.objects.filter(pk=proposal.pk).exists())
        self.assertTrue(AnalysisRun.objects.filter(pk=run.pk).exists())
        self.assertTrue(DocumentEvidence.objects.filter(pk=document.pk).exists())

    def test_chat_delete_blocks_cross_case_cascades(self):
        ChatTurn.objects.filter(pk=self.turn.pk).update(case=self.other_case)
        self.grant_chat_admin_permissions(self.owner)
        self.client.force_login(self.owner)
        delete_url = admin_url(EvidenceChat, "delete", self.chat)
        response = self.client.post(delete_url, {"post": "yes"})
        self.assertContains(response, "Related records have inconsistent case links")
        self.assertNotContains(response, str(self.turn.pk))
        self.assertTrue(EvidenceChat.objects.filter(pk=self.chat.pk).exists())

    def test_session_link_respects_archived_sources(self):
        person = Person.objects.create(case=self.case, first_name="Example")
        message = TextMessageEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            sender=person,
            recipient=person,
            title="Synthetic message",
            body="Example text",
        )
        batch = CaseAnalysisBatch.objects.create(
            case=self.case, requested_by=self.owner
        )
        grouping = ConversationGrouping.objects.create(
            case=self.case,
            batch=batch,
            first_person=person,
            second_person=person,
            method_version="v1",
            source_count=1,
        )
        session = ConversationSession.objects.create(
            case=self.case, grouping=grouping, start_ordinal=0, message_count=1
        )
        ConversationSessionMember.objects.create(
            case=self.case,
            session=session,
            message=message,
            ordinal=0,
            sha256=message.sha256,
        )
        grouping.published_at = timezone.now()
        grouping.save(update_fields=["published_at"])
        self.owner.user_permissions.add(
            Permission.objects.get(codename="view_conversationsession")
        )
        self.client.force_login(self.owner)
        detail = admin_url(ConversationSession, "change", session)
        self.assertContains(self.client.get(detail), "Open saved session and messages")
        TextMessageEvidence.objects.filter(pk=message.pk).update(
            is_archived=True,
            archive_changed_by=self.owner,
            archive_changed_at=timezone.now(),
        )
        response = self.client.get(detail)
        url = reverse(
            "phone:conversation_detail",
            args=(self.case.pk, person.pk, person.pk),
            query={"tab": "instances"},
        )
        self.assertContains(response, f'<a href="{url}">Open available sessions</a>')

    def test_parent_deletion_retains_existing_cascades(self):
        person = Person.objects.create(case=self.case, first_name="Example")
        social = SocialAccount.objects.create(
            person=person, platform="Example", username="account"
        )
        vehicle = Vehicle.objects.create(case=self.case, make="Example")
        trips = VehicleTripState.objects.create(case=self.case, vehicle=vehicle)
        self.owner.user_permissions.set(
            Permission.objects.filter(
                codename__in=("delete_person", "delete_vehicle", "delete_case")
            )
        )
        self.client.force_login(self.owner)
        for parent, child in (
            (person, social),
            (vehicle, trips),
            (self.case, self.invitation),
        ):
            with self.subTest(parent=parent):
                response = self.client.post(
                    admin_url(type(parent), "delete", parent), {"post": "yes"}
                )
                self.assertEqual(response.status_code, 302)
                self.assertFalse(type(child).objects.filter(pk=child.pk).exists())
        self.assertFalse(EvidenceChat.objects.filter(pk=self.chat.pk).exists())

        index = CaseSearchIndex.objects.create(
            case=self.other_case, requested_by=self.viewer, profile="v1"
        )
        self.client.force_login(self.operator)
        user_model = get_user_model()
        response = self.client.post(
            admin_url(user_model, "delete", self.viewer), {"post": "yes"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(CaseSearchIndex.objects.filter(pk=index.pk).exists())
