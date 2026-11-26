from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from django.apps import apps
from django.conf import settings
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from cases.models import Case, CaseInvitation, CaseMembership
from core.models import AnalysisResult, AnalysisRun, CaseAnalysisBatch, CaseSearchIndex
from geo.trip_models import VehicleTripState
from people.models import Person
from phone.models import (
    ConversationGrouping,
    ConversationSession,
    ConversationSessionMember,
    TextMessageEvidence,
)
from socialmedia.models import SocialAccount
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

    def test_project_models_have_an_admin_or_an_intentional_workflow(self):
        without_standalone_admin = {
            "cases.CaseMembership",  # Case inline
            "core.ChatAnalysisProposal",  # Reviewed in Evidence chat
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
