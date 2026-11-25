import hashlib
from datetime import timedelta
from unittest.mock import patch

from allauth.account.models import EmailAddress
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core import mail
from django.db import IntegrityError, connection, transaction
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from cases.admin import CaseAdmin
from cases.invitations import InvitationUnavailable, resolve_invitation
from cases.models import Case, CaseInvitation, CaseMembership
from core.analysis.errors import AnalysisError
from core.analysis.execution import (
    available_executions,
    case_grant_revision,
    configured_external_executions,
    validate_case_execution_grant,
)
from core.analysis.testing import approve_case_execution
from core.analysis.tests.test_openai_gateway import AUTO_OPENAI, OPENAI
from geo.models import PersonLocationEvidence
from geo.testing import LOCAL_GEOCODER
from people.models import Person
from phone.models import PhoneCallEvidence
from text.models import TextEvidence


@override_settings(PANDECT_PUBLIC_ORIGIN="https://pandect.example")
class CaseInterfaceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        for name in ("owner", "viewer", "editor", "outsider", "invitee"):
            user = get_user_model().objects.create_user(
                username=name,
                email=f"{name}@example.com",
                password="synthetic-pass-753!",
            )
            EmailAddress.objects.create(
                user=user, email=user.email, verified=True, primary=True
            )
            setattr(cls, name, user)
        cls.case = Case.objects.create(owner=cls.owner, name="Harbour records")
        cls.other_case = Case.objects.create(
            owner=cls.outsider, name="Private unrelated"
        )
        cls.viewer_membership = CaseMembership.objects.create(
            case=cls.case, user=cls.viewer
        )
        cls.editor_membership = CaseMembership.objects.create(
            case=cls.case, user=cls.editor, role="editor"
        )

    def url(self, name, *args):
        return reverse(f"cases:{name}", args=args or (self.case.pk,))

    def login(self, user=None):
        self.client.force_login(user or self.owner)

    def invite(self, *, email=None, role="viewer"):
        self.login()
        response = self.client.post(
            self.url("invitation_create"),
            {"email": email or self.invitee.email, "role": role},
        )
        self.assertEqual(response.status_code, 302)
        invitation = CaseInvitation.objects.latest("created_at")
        token = mail.outbox[-1].body.split("/invitations/", 1)[1].split("/", 1)[0]
        return invitation, token

    def test_case_list_is_currently_scoped_even_for_superusers(self):
        self.login(self.viewer)
        response = self.client.get(reverse("cases:case_list"))
        self.assertContains(response, self.case.name)
        self.assertNotContains(response, self.other_case.name)
        self.viewer.is_superuser = True
        self.viewer.save()
        self.assertEqual(
            self.client.get(self.url("case_detail", self.other_case.pk)).status_code,
            404,
        )
        self.viewer_membership.delete()
        self.assertEqual(self.client.get(self.url("case_detail")).status_code, 404)

    def test_case_list_roles_are_read_with_the_paginated_cases(self):
        expected = {self.case.pk: "viewer"}
        for index in range(30):
            case = Case.objects.create(owner=self.owner, name=f"Shared {index}")
            role = "editor" if index % 2 else "viewer"
            CaseMembership.objects.create(case=case, user=self.viewer, role=role)
            expected[case.pk] = role
        owned = Case.objects.create(owner=self.viewer, name="Owned case")
        expected[owned.pk] = "owner"
        self.login(self.viewer)
        for page_number in (1, 2):
            # Session, user, verification, count and page; roles need no extra
            # query or an unbounded dictionary of every membership.
            with self.assertNumQueries(5):
                response = self.client.get(
                    reverse("cases:case_list"), {"page": page_number}
                )
            for case in response.context["page_obj"]:
                self.assertEqual(case.current_role, expected.pop(case.pk))
        self.assertFalse(expected)

    def test_case_links_and_legacy_case_url_open_case_tools(self):
        home_url = reverse("core:analysis:index", args=(self.case.pk,))
        for user in (self.owner, self.editor, self.viewer):
            with self.subTest(user=user):
                self.login(user)
                response = self.client.get(reverse("cases:case_list"))
                self.assertContains(response, f'href="{home_url}"')
                self.assertRedirects(self.client.get(self.url("case_detail")), home_url)

    def test_empty_case_list_hides_search_and_filtered_empty_list_has_one_reset(self):
        self.login(self.invitee)
        url = reverse("cases:case_list")
        self.assertNotContains(self.client.get(url), 'aria-label="Search cases"')
        response = self.client.get(url, {"q": "No synthetic case"})
        self.assertContains(response, 'aria-label="Search cases"')
        self.assertContains(response, "Clear filters", count=1)

    @override_settings(**(LOCAL_GEOCODER | {"PANDECT_GEOCODER_LOCAL": False}))
    def test_case_owner_approves_current_destinations_and_can_revoke(self):
        self.login()
        settings = self.url("case_settings")
        action = self.url("case_external_analysis")
        form = self.client.get(settings)
        consent = {
            "action": "approve",
            "confirmation": form.context["external_analysis"]["approve_confirmation"],
        }
        self.assertContains(form, "geocoder.internal:8080")
        self.assertContains(form, "Addresses or coordinates")
        self.assertEqual(available_executions(self.case, "geocode_locations", {}), {})

        for actor, status in (
            (self.viewer, 403),
            (self.editor, 403),
            (self.outsider, 404),
        ):
            self.login(actor)
            self.assertEqual(self.client.post(action, consent).status_code, status)
        self.login()
        with self.settings(PANDECT_GEOCODER_VERSION="new-dataset"):
            self.assertContains(
                self.client.post(action, consent, follow=True), "Settings changed"
            )
        self.case.refresh_from_db()
        self.assertFalse(self.case.external_analysis_approval)

        self.assertEqual(self.client.get(action).status_code, 405)
        self.assertEqual(self.client.post(action, consent).status_code, 302)
        self.case.refresh_from_db()
        self.assertEqual(
            self.case.external_analysis_approval["approved_by"], str(self.owner.pk)
        )
        self.assertIn(
            "external", available_executions(self.case, "geocode_locations", {})
        )
        self.assertFalse(self.other_case.external_analysis_approval)
        admin_case = Case.objects.get(pk=self.case.pk)
        revoke = self.client.get(settings).context["external_analysis"][
            "revoke_confirmation"
        ]
        self.assertEqual(
            self.client.post(
                action, {"action": "revoke", "confirmation": revoke}
            ).status_code,
            302,
        )
        CaseAdmin(Case, admin.site).save_model(None, admin_case, None, change=True)
        self.client.post(action, consent)
        self.case.refresh_from_db()
        self.assertEqual(available_executions(self.case, "geocode_locations", {}), {})
        self.assertFalse(self.case.external_analysis_approval.get("executions"))

    def ai_draft(self, **changes):
        form = self.client.get(self.url("case_settings")).context["ai_settings_form"]
        return {
            "confirmation": form["confirmation"].value(),
            "ai_backend": "gateway",
            **changes,
        }

    def ai_confirm(self, review, allowed=None, **changes):
        data = dict(review.context["proposal"])
        if allowed is None:
            allowed = [item["scope_id"] for item in review.context["destinations"]]
        return self.client.post(
            self.url("case_ai_settings"),
            data
            | {
                "action": "approve",
                "review_confirmation": review.context["review_confirmation"],
                "allow": allowed,
            }
            | changes,
        )

    @override_settings(PANDECT_AI_GATEWAY=AUTO_OPENAI)
    def test_first_use_requires_owner_review_and_model_switch_needs_only_save(self):
        self.login()
        action = self.url("case_ai_settings")
        draft = self.ai_draft(content_events="gpt-6.2-sol")
        review = self.client.post(action, draft)
        self.assertContains(review, "Approve and save")
        self.assertContains(review, "chat questions")
        self.case.refresh_from_db()
        self.assertFalse(self.case.external_analysis_approval)
        self.assertEqual(self.case.ai_backend, "local")
        self.assertEqual(self.case.external_analysis_models, {})
        for actor, status in (
            (self.viewer, 403),
            (self.editor, 403),
            (self.outsider, 404),
        ):
            self.login(actor)
            self.assertEqual(self.client.post(action, draft).status_code, status)
            self.assertEqual(self.ai_confirm(review).status_code, status)
        self.login()
        self.assertEqual(self.client.get(action).status_code, 405)
        self.assertEqual(
            self.ai_confirm(review, content_events="gpt-6.1-terra").status_code, 409
        )
        self.assertEqual(self.ai_confirm(review).status_code, 302)
        self.case.refresh_from_db()
        self.assertEqual(self.case.ai_backend, "gateway")
        self.assertEqual(len(self.case.external_analysis_approval["grants"]), 1)
        before = self.case.external_analysis_approval
        self.assertFalse(self.other_case.external_analysis_approval)
        self.assertEqual(
            self.client.post(
                action, self.ai_draft(content_events="gpt-6.1-terra")
            ).status_code,
            302,
        )
        self.case.refresh_from_db()
        route = available_executions(self.case, "content_events", {})["gateway"]
        self.assertEqual(route["model"], "gpt-6.1-terra")
        self.assertIn("gateway", available_executions(self.case, "evidence_chat", {}))
        self.assertEqual(self.case.external_analysis_approval, before)
        self.assertEqual(self.client.post(action, draft).status_code, 409)
        self.assertEqual(
            self.client.post(action, self.ai_draft(content_events="")).status_code, 302
        )
        self.case.refresh_from_db()
        self.assertEqual(self.case.external_analysis_models, {})

    @override_settings(PANDECT_AI_GATEWAY=AUTO_OPENAI)
    def test_local_preferences_unavailable_models_and_legacy_exact_grants(self):
        self.login()
        action = self.url("case_ai_settings")
        self.assertTrue(
            self.client.post(action, self.ai_draft(content_events="gpt-not-configured"))
            .context["ai_settings_form"]
            .has_error("content_events", "invalid_choice")
        )
        executions = configured_external_executions(self.case)
        approve_case_execution(self.case, self.owner, executions)
        old_approval = self.case.external_analysis_approval
        self.assertEqual(self.client.post(action, self.ai_draft()).status_code, 302)
        self.assertContains(
            self.client.post(action, self.ai_draft(content_events="gpt-6.2-sol")),
            "Approve and save",
        )
        self.case.refresh_from_db()
        self.assertEqual(self.case.external_analysis_approval, old_approval)
        self.assertEqual(
            self.client.post(
                action, self.ai_draft(ai_backend="local", content_events="gpt-6.2-sol")
            ).status_code,
            302,
        )
        self.case.refresh_from_db()
        self.assertEqual(self.case.ai_backend, "local")
        self.assertNotIn(
            "gateway", available_executions(self.case, "content_events", {})
        )
        self.assertIn("gateway", available_executions(self.case, "text_topics", {}))
        self.case.external_analysis_models = {"text_topics": "retired-model"}
        self.case.save(update_fields=("external_analysis_models",))
        settings = self.client.get(self.url("case_settings"))
        self.assertContains(settings, "retired-model (unavailable)")
        self.assertNotIn("gateway", available_executions(self.case, "text_topics", {}))

    @override_settings(
        PANDECT_AI_GATEWAY=AUTO_OPENAI | {"workers_token": "synthetic-workers-token"}
    )
    def test_separate_audio_scope_and_targeted_revocation(self):
        self.login()
        action = self.url("case_ai_settings")
        review = self.client.post(action, self.ai_draft())
        text_scope = [
            row["scope_id"]
            for row in review.context["destinations"]
            if row["name"] == "OpenAI"
        ]
        self.assertEqual(len(review.context["destinations"]), 2)
        self.assertContains(self.ai_confirm(review, []), "Select the destinations")
        self.assertEqual(self.ai_confirm(review, text_scope).status_code, 302)
        self.case.refresh_from_db()
        route = available_executions(self.case, "content_events", {})["gateway"]
        text_revision = case_grant_revision(self.case, "content_events", route)
        saved_approval = self.case.external_analysis_approval
        self.assertEqual(self.client.post(action, self.ai_draft()).status_code, 302)
        self.case.refresh_from_db()
        self.assertEqual(self.case.external_analysis_approval, saved_approval)
        self.assertNotIn(
            "gateway", available_executions(self.case, "transcription", {})
        )
        self.assertEqual(
            self.client.post(
                action, self.ai_draft(content_events="gpt-6.2-sol")
            ).status_code,
            302,
        )
        audio_review = self.client.post(action, {"action": "review_current"})
        self.assertEqual(len(audio_review.context["destinations"]), 1)
        self.assertEqual(self.ai_confirm(audio_review).status_code, 302)
        self.case.refresh_from_db()
        audio = available_executions(self.case, "transcription", {})["gateway"]
        audio_revision = case_grant_revision(self.case, "transcription", audio)

        def revoke_audio():
            current = self.client.get(self.url("case_settings")).context[
                "external_analysis"
            ]
            row = next(
                row
                for row in current["destinations"]
                if "recordings" in row["material"]
            )
            return self.client.post(
                self.url("case_external_analysis"),
                {
                    "action": "revoke_destination",
                    "confirmation": row["revoke_confirmation"],
                },
            )

        self.assertEqual(revoke_audio().status_code, 302)
        self.case.refresh_from_db()
        self.assertNotIn(
            "gateway", available_executions(self.case, "transcription", {})
        )
        self.assertEqual(
            case_grant_revision(self.case, "content_events", route), text_revision
        )
        self.assertEqual(
            self.ai_confirm(
                self.client.post(action, {"action": "review_current"})
            ).status_code,
            302,
        )
        self.case.refresh_from_db()
        with self.assertRaisesMessage(
            AnalysisError, "external_processing_not_approved"
        ):
            validate_case_execution_grant(
                self.case, "transcription", audio, audio_revision
            )
        validate_case_execution_grant(self.case, "content_events", route, text_revision)

    @override_settings(
        PANDECT_AI_GATEWAY=AUTO_OPENAI
        | {"workers_token": "synthetic-workers-token", "unified_billing": True}
    )
    def test_switch_between_approved_providers_and_preserve_old_receipts(self):
        self.login()
        action = self.url("case_ai_settings")
        review = self.client.post(action, self.ai_draft())
        self.assertEqual(self.ai_confirm(review).status_code, 302)
        self.case.refresh_from_db()
        original = available_executions(self.case, "content_events", {})["gateway"]
        revision = case_grant_revision(self.case, "content_events", original)
        model = "anthropic/claude-sonnet-5.5"
        review = self.client.post(action, self.ai_draft(evidence_chat=model))
        self.assertEqual(
            [row["name"] for row in review.context["destinations"]], ["Anthropic"]
        )
        self.assertEqual(self.ai_confirm(review).status_code, 302)
        self.case.refresh_from_db()
        validate_case_execution_grant(self.case, "content_events", original, revision)
        self.assertEqual(self.case.external_analysis_models["evidence_chat"], model)
        for choice, provider in (("gpt-6.1-terra", "openai"), (model, "anthropic")):
            self.assertEqual(
                self.client.post(
                    action, self.ai_draft(content_events=choice)
                ).status_code,
                302,
            )
            self.case.refresh_from_db()
            route = available_executions(self.case, "content_events", {})["gateway"]
            self.assertEqual(route["provider"], provider)
        with self.settings(PANDECT_AI_GATEWAY=AUTO_OPENAI):
            rejected = self.client.post(action, self.ai_draft(question_synthesis=model))
            self.assertTrue(
                rejected.context["ai_settings_form"].has_error("question_synthesis")
            )
            self.case.refresh_from_db()
            self.assertNotIn("question_synthesis", self.case.external_analysis_models)

    @override_settings(PANDECT_AI_GATEWAY=OPENAI)
    def test_changed_account_or_alias_invalidates_review_without_saving(self):
        self.login()
        action = self.url("case_ai_settings")
        review = self.client.post(action, self.ai_draft())
        with self.settings(
            PANDECT_AI_GATEWAY=OPENAI | {"openai_key_alias": "replacement"}
        ):
            self.assertEqual(self.ai_confirm(review).status_code, 409)
        self.case.refresh_from_db()
        self.assertEqual(self.case.ai_backend, "local")
        self.assertFalse(self.case.external_analysis_approval)

    @override_settings(
        PANDECT_AI_GATEWAY={}, **(LOCAL_GEOCODER | {"PANDECT_GEOCODER_LOCAL": False})
    )
    def test_geographic_approval_does_not_enable_external_ai_backend(self):
        self.login()
        settings = self.client.get(self.url("case_settings"))
        self.assertEqual(
            self.client.post(
                self.url("case_external_analysis"),
                {
                    "action": "approve",
                    "confirmation": settings.context["external_analysis"][
                        "approve_confirmation"
                    ],
                },
            ).status_code,
            302,
        )
        self.assertContains(
            self.client.post(self.url("case_ai_settings"), self.ai_draft()),
            "External AI needs at least one configured model destination.",
        )
        self.case.refresh_from_db()
        self.assertEqual(self.case.ai_backend, "local")

    def test_settings_keeps_actionable_invitations_outside_history(self):
        now = timezone.now()
        invitations = {}
        for state, values in (
            ("pending", {}),
            ("failed", {"delivery_failed": True}),
            ("expired", {"expires_at": now - timedelta(days=1)}),
            ("accepted", {"accepted_at": now, "accepted_by": self.invitee}),
            ("revoked", {"revoked_at": now}),
        ):
            invitations[state] = CaseInvitation.objects.create(
                case=self.case,
                email=f"{state}@example.com",
                invited_by=self.owner,
                token_digest=hashlib.sha256(state.encode()).hexdigest(),
                **{"expires_at": now + timedelta(days=1), **values},
            )
        self.login()
        response = self.client.get(self.url("case_settings"))
        self.assertCountEqual(
            response.context["pending_invitations"],
            [invitations[state] for state in ("pending", "failed", "expired")],
        )
        self.assertCountEqual(
            response.context["invitation_history"],
            [invitations[state] for state in ("accepted", "revoked")],
        )
        self.assertContains(response, "Invitation history (2)")
        self.assertContains(response, "Expired on <time")
        self.assertContains(response, "Change role and resend")
        for state in ("pending", "failed", "expired"):
            self.assertContains(
                response,
                self.url("invitation_resend", self.case.pk, invitations[state].pk),
            )

    def test_empty_posts_show_required_errors_without_mutating_records(self):
        invitation, _ = self.invite()
        urls = (
            (reverse("cases:case_create"), "name"),
            (self.url("case_delete"), "confirm"),
            (
                self.url("membership_edit", self.case.pk, self.viewer_membership.pk),
                "role",
            ),
            (
                self.url("membership_remove", self.case.pk, self.viewer_membership.pk),
                "confirm",
            ),
            (self.url("invitation_revoke", self.case.pk, invitation.pk), "confirm"),
        )
        for url, field in urls:
            with self.subTest(url=url):
                response = self.client.post(url, {})
                self.assertEqual(response.status_code, 200)
                self.assertIn(field, response.context["form"].errors)
                self.assertContains(response, "data-error-summary")
                self.assertContains(response, f'href="#id_{field}"')
        invitation.refresh_from_db()
        self.assertTrue(invitation.is_pending)
        self.viewer_membership.refresh_from_db()
        self.assertEqual(self.viewer_membership.role, "viewer")
        self.assertEqual(Case.objects.count(), 2)

    def test_workspace_scopes_evidence_but_deletion_counts_all_case_evidence(self):
        person = Person.objects.create(case=self.case, first_name="Reported person")
        other_person = Person.objects.create(
            case=self.other_case, first_name="Unrelated person"
        )
        common = {"case": self.case, "uploaded_by": self.owner}
        location = PersonLocationEvidence.objects.create(
            **common,
            person=person,
            address="Reported address",
            title="Invalid location",
        )
        call = PhoneCallEvidence.objects.create(
            **common, sender=person, recipient=person, title="Invalid call"
        )
        PersonLocationEvidence.objects.filter(pk=location.pk).update(
            person=other_person
        )
        PhoneCallEvidence.objects.filter(pk=call.pk).update(
            recipient=other_person,
            is_archived=True,
            archive_changed_by=self.owner,
            archive_changed_at=timezone.now(),
        )
        active = TextEvidence.objects.create(**common, title="Active note", body="Text")
        archived = TextEvidence.objects.create(
            **common,
            title="Archived note",
            body="Earlier text",
            is_archived=True,
            archive_changed_by=self.owner,
            archive_changed_at=timezone.now(),
        )
        for user in (self.owner, self.editor, self.viewer):
            with self.subTest(user=user):
                self.login(user)
                response = self.client.get(
                    reverse("core:evidence:list", args=(self.case.pk,)),
                    {"state": "all"},
                )
                self.assertCountEqual(
                    [item["title"] for item in response.context["rows"]],
                    [active.title, archived.title],
                )
                self.assertNotContains(response, location.title)
                self.assertNotContains(response, call.title)
                if user != self.owner:
                    self.assertEqual(
                        self.client.get(self.url("case_delete")).status_code, 403
                    )
        self.login(self.owner)
        for response in (
            self.client.get(self.url("case_delete")),
            self.client.post(self.url("case_delete"), {}),
        ):
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.context["active_evidence_count"], 2)
            self.assertEqual(response.context["archived_evidence_count"], 2)
            self.assertNotContains(response, location.title)
            self.assertNotContains(response, call.title)
            self.assertNotContains(response, other_person.first_name)
        self.assertTrue(Case.objects.filter(pk=self.case.pk).exists())

    def test_create_sets_current_owner_and_requires_a_name(self):
        self.login(self.viewer)
        response = self.client.post(
            reverse("cases:case_create"),
            {
                "name": "New investigation",
                "owner": str(self.outsider.pk),
                "description": "Forged description",
            },
        )
        case = Case.objects.get(name="New investigation")
        self.assertEqual(case.owner, self.viewer)
        self.assertEqual(case.description, "")
        self.assertRedirects(response, reverse("core:analysis:index", args=(case.pk,)))
        response = self.client.post(reverse("cases:case_create"), {"name": "   "})
        self.assertEqual(response.status_code, 200)
        self.assertIn("name", response.context["form"].errors)

    def test_owner_only_case_mutations(self):
        for user in (self.editor, self.viewer):
            self.login(user)
            for route in ("case_settings", "case_delete", "invitation_create"):
                with self.subTest(user=user.username, route=route):
                    self.assertEqual(self.client.get(self.url(route)).status_code, 403)
                    self.assertEqual(
                        self.client.post(
                            self.url(route), {"confirm": "on"}
                        ).status_code,
                        403,
                    )
        self.case.refresh_from_db()
        self.assertEqual(self.case.owner, self.owner)

    def test_case_edit_detects_conflict_and_preserves_draft(self):
        self.login()
        revision = self.case.updated_at.isoformat()
        self.case.name = "Newer case name"
        self.case.description = "Previous investigation context"
        self.case.save()
        response = self.client.post(
            self.url("case_settings"),
            {
                "name": "Draft case name",
                "description": "My draft",
                "expected_updated_at": revision,
            },
        )
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "Draft case name", status_code=409)
        self.assertContains(response, "Newer case name", status_code=409)
        self.assertNotIn("description", response.context["form"].fields)
        self.assertContains(response, "Save reviewed changes", status_code=409)
        self.assertContains(response, 'id="rename-case" open', status_code=409)
        reviewed_revision = response.context["form"]["expected_updated_at"].value()
        self.case.refresh_from_db()
        self.assertEqual(self.case.name, "Newer case name")
        self.assertEqual(self.case.description, "Previous investigation context")
        self.assertEqual(reviewed_revision, self.case.updated_at.isoformat())
        response = self.client.post(
            self.url("case_settings"),
            {
                "name": "Draft case name",
                "description": "My draft",
                "expected_updated_at": reviewed_revision,
                "owner": self.outsider.pk,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.case.refresh_from_db()
        self.assertEqual(self.case.name, "Draft case name")
        self.assertEqual(self.case.description, "Previous investigation context")
        self.assertEqual(self.case.owner, self.owner)

    def test_case_edit_updates_timestamp_and_preserves_owner(self):
        self.login()
        before = self.case.updated_at
        response = self.client.post(
            self.url("case_settings"),
            {
                "name": "Updated harbour",
                "description": "Synthetic",
                "expected_updated_at": before.isoformat(),
                "owner": self.outsider.pk,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.case.refresh_from_db()
        self.assertEqual(self.case.name, "Updated harbour")
        self.assertEqual(self.case.owner, self.owner)
        self.assertGreater(self.case.updated_at, before)

    def test_delete_needs_confirmation_and_keeps_accounts(self):
        self.login()
        response = self.client.post(self.url("case_delete"), {})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Case.objects.filter(pk=self.case.pk).exists())
        response = self.client.post(self.url("case_delete"), {"confirm": "on"})
        self.assertRedirects(response, reverse("cases:case_list"))
        self.assertFalse(Case.objects.filter(pk=self.case.pk).exists())
        self.assertTrue(get_user_model().objects.filter(pk=self.owner.pk).exists())

    def test_csrf_required_for_product_mutations(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        self.assertEqual(
            client.post(self.url("case_delete"), {"confirm": "on"}).status_code, 403
        )

    def test_new_invitation_hashes_token_and_sends_configured_origin(self):
        invitation, token = self.invite(role="editor")
        self.assertEqual(invitation.role, "editor")
        self.assertEqual(
            invitation.token_digest, hashlib.sha256(token.encode()).hexdigest()
        )
        self.assertNotEqual(invitation.token_digest, token)
        self.assertEqual(mail.outbox[-1].to, [self.invitee.email])
        self.assertIn("https://pandect.example/invitations/", mail.outbox[-1].body)
        self.assertFalse(invitation.delivery_failed)
        self.assertIsNotNone(invitation.last_sent_at)
        self.assertLess(
            abs((invitation.expires_at - timezone.now()).total_seconds() - 7 * 86400),
            10,
        )

    def test_get_accept_does_not_grant_access_and_post_accepts_once(self):
        invitation, token = self.invite(role="editor")
        self.login(self.invitee)
        url = reverse("invitation_accept", args=(token,))
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(any("FOR UPDATE" in query["sql"] for query in queries))
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(response["Referrer-Policy"], "same-origin")
        self.assertFalse(
            CaseMembership.objects.filter(case=self.case, user=self.invitee).exists()
        )
        self.assertEqual(self.client.post(url).status_code, 302)
        self.assertEqual(self.client.post(url).status_code, 302)
        membership = CaseMembership.objects.get(case=self.case, user=self.invitee)
        self.assertEqual(membership.role, "editor")
        self.assertEqual(
            CaseMembership.objects.filter(case=self.case, user=self.invitee).count(), 1
        )
        invitation.refresh_from_db()
        self.assertEqual(invitation.accepted_by, self.invitee)
        self.assertIsNotNone(invitation.accepted_at)

    def test_revoked_accepted_access_cannot_be_restored_by_replay(self):
        _, token = self.invite()
        self.login(self.invitee)
        url = reverse("invitation_accept", args=(token,))
        self.client.post(url)
        CaseMembership.objects.filter(case=self.case, user=self.invitee).delete()
        self.assertEqual(self.client.post(url).status_code, 400)
        self.assertFalse(
            CaseMembership.objects.filter(case=self.case, user=self.invitee).exists()
        )

    def test_invitation_service_checks_current_account_status(self):
        invitation, token = self.invite()
        get_user_model().objects.filter(pk=self.invitee.pk).update(is_active=False)

        with self.assertRaisesMessage(InvitationUnavailable, "wrong_email"):
            resolve_invitation(self.invitee, token, accept=True)

        invitation.refresh_from_db()
        self.assertIsNone(invitation.accepted_at)
        self.assertFalse(
            CaseMembership.objects.filter(case=self.case, user=self.invitee).exists()
        )

    def test_acceptance_does_not_change_existing_member_role(self):
        _, token = self.invite(role="editor")
        CaseMembership.objects.create(case=self.case, user=self.invitee, role="viewer")
        self.login(self.invitee)
        self.client.post(reverse("invitation_accept", args=(token,)))
        self.assertEqual(
            CaseMembership.objects.get(case=self.case, user=self.invitee).role, "viewer"
        )

    def test_wrong_account_cannot_see_case_or_accept(self):
        _, token = self.invite()
        self.login(self.outsider)
        response = self.client.post(reverse("invitation_accept", args=(token,)))
        self.assertEqual(response.status_code, 403)
        self.assertNotContains(response, "Harbour records", status_code=403)
        self.assertContains(response, "Manage email addresses", status_code=403)
        self.assertContains(response, "Sign in with another account", status_code=403)
        self.assertFalse(
            CaseMembership.objects.filter(case=self.case, user=self.outsider).exists()
        )

    def test_invited_address_itself_must_be_verified(self):
        _, token = self.invite()
        EmailAddress.objects.filter(user=self.invitee).update(verified=False)
        EmailAddress.objects.create(
            user=self.invitee, email="other-invitee@example.com", verified=True
        )
        self.login(self.invitee)
        self.assertEqual(
            self.client.post(reverse("invitation_accept", args=(token,))).status_code,
            403,
        )

    def test_expired_revoked_invalid_and_deleted_case_links(self):
        invitation, token = self.invite()
        self.login(self.invitee)
        url = reverse("invitation_accept", args=(token,))
        invitation.expires_at = timezone.now() - timedelta(seconds=1)
        invitation.save()
        response = self.client.post(url)
        self.assertEqual(response.status_code, 400)
        self.assertNotContains(response, "Manage email addresses", status_code=400)
        self.assertNotContains(
            response, "Sign in with another account", status_code=400
        )
        invitation.expires_at = timezone.now() + timedelta(days=1)
        invitation.revoked_at = timezone.now()
        invitation.save()
        self.assertEqual(self.client.post(url).status_code, 400)
        self.assertEqual(
            self.client.get(
                reverse("invitation_accept", args=("invalid",))
            ).status_code,
            404,
        )
        self.case.delete()
        self.assertEqual(self.client.post(url).status_code, 404)

    def test_resend_and_role_change_rotate_token(self):
        invitation, original_token = self.invite()
        response = self.client.post(
            self.url("invitation_resend", self.case.pk, invitation.pk)
        )
        self.assertEqual(response.status_code, 302)
        invitation.refresh_from_db()
        resent_digest = invitation.token_digest
        self.assertNotEqual(
            resent_digest, hashlib.sha256(original_token.encode()).hexdigest()
        )
        response = self.client.post(
            self.url("invitation_edit", self.case.pk, invitation.pk), {"role": "editor"}
        )
        self.assertEqual(response.status_code, 302)
        invitation.refresh_from_db()
        self.assertNotEqual(invitation.token_digest, resent_digest)
        self.assertEqual(invitation.role, "editor")
        self.login(self.invitee)
        self.assertEqual(
            self.client.get(
                reverse("invitation_accept", args=(original_token,))
            ).status_code,
            404,
        )

    def test_email_failure_keeps_pending_invitation_and_can_retry(self):
        self.login()
        with patch(
            "cases.invitations.EmailMessage.send",
            side_effect=OSError("synthetic failure"),
        ):
            response = self.client.post(
                self.url("invitation_create"),
                {"email": self.invitee.email, "role": "viewer"},
            )
        self.assertEqual(response.status_code, 302)
        invitation = self.case.invitations.get()
        self.assertTrue(invitation.delivery_failed)
        self.assertTrue(invitation.is_pending)
        response = self.client.post(
            self.url("invitation_resend", self.case.pk, invitation.pk)
        )
        self.assertEqual(response.status_code, 302)
        invitation.refresh_from_db()
        self.assertFalse(invitation.delivery_failed)
        self.assertIsNotNone(invitation.last_sent_at)

    def test_owner_current_member_and_duplicate_pending_invites_rejected(self):
        self.login()
        secondary = EmailAddress.objects.create(
            user=self.viewer, email="viewer-secondary@example.com", verified=True
        )
        EmailAddress.objects.filter(user=self.editor).delete()
        for email in (
            self.owner.email,
            self.viewer.email,
            secondary.email,
            self.editor.email,
        ):
            response = self.client.post(
                self.url("invitation_create"),
                {"email": email.upper(), "role": "viewer"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertIn("email", response.context["form"].errors)
        invitation, _ = self.invite()
        response = self.client.post(
            self.url("invitation_create"),
            {"email": self.invitee.email.upper(), "role": "editor"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.case.invitations.count(), 1)
        with transaction.atomic(), self.assertRaises(IntegrityError):
            CaseInvitation.objects.create(
                case=self.case,
                email=self.invitee.email.upper(),
                role="viewer",
                invited_by=self.owner,
                expires_at=timezone.now() + timedelta(days=1),
                token_digest="b" * 64,
            )
        self.assertEqual(self.case.invitations.get().pk, invitation.pk)

    def test_expired_invitation_can_be_replaced(self):
        invitation, _ = self.invite()
        invitation.expires_at = timezone.now() - timedelta(seconds=1)
        invitation.save()
        self.login()
        response = self.client.post(
            self.url("invitation_create"),
            {"email": self.invitee.email, "role": "editor"},
        )
        self.assertEqual(response.status_code, 302)
        invitation.refresh_from_db()
        self.assertIsNotNone(invitation.revoked_at)
        self.assertEqual(
            self.case.invitations.filter(
                accepted_at__isnull=True, revoked_at__isnull=True
            ).count(),
            1,
        )

    def test_invitation_and_member_ids_are_scoped_to_case(self):
        self.login(self.outsider)
        response = self.client.post(
            self.url(
                "membership_remove", self.other_case.pk, self.viewer_membership.pk
            ),
            {"confirm": "on"},
        )
        self.assertEqual(response.status_code, 404)
        invitation, _ = self.invite()
        self.login(self.outsider)
        response = self.client.post(
            self.url("invitation_resend", self.other_case.pk, invitation.pk)
        )
        self.assertEqual(response.status_code, 404)

    def test_membership_changes_revoke_and_downgrade_on_subsequent_request(self):
        self.login()
        response = self.client.post(
            self.url("membership_edit", self.case.pk, self.editor_membership.pk),
            {"role": "viewer"},
        )
        self.assertEqual(response.status_code, 302)
        self.editor_membership.refresh_from_db()
        self.assertEqual(self.editor_membership.role, "viewer")
        self.client.post(
            self.url("membership_remove", self.case.pk, self.editor_membership.pk),
            {"confirm": "on"},
        )
        self.login(self.editor)
        self.assertEqual(self.client.get(self.url("case_detail")).status_code, 404)

    def test_revoke_requires_confirmation_and_invalidates_link(self):
        invitation, token = self.invite()
        url = self.url("invitation_revoke", self.case.pk, invitation.pk)
        self.assertEqual(self.client.post(url, {}).status_code, 200)
        invitation.refresh_from_db()
        self.assertIsNone(invitation.revoked_at)
        self.assertEqual(self.client.post(url, {"confirm": "on"}).status_code, 302)
        self.login(self.invitee)
        self.assertEqual(
            self.client.post(reverse("invitation_accept", args=(token,))).status_code,
            400,
        )
