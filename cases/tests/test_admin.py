from uuid import uuid4

from django.contrib import admin
from django.contrib.admin.models import ADDITION, LogEntry
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client, TestCase
from django.urls import reverse

from cases.models import Case, CaseMembership
from geo.models import PointOfInterest
from people.models import Person


class CaseAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = cls.staff("owner")
        cls.viewer = cls.staff("viewer")
        cls.editor = cls.staff("editor")
        cls.outsider = cls.staff("outsider")
        cls.superuser = get_user_model().objects.create_superuser(
            username="operator", email="operator@example.com", password=None
        )
        cls.case = Case.objects.create(owner=cls.owner)
        cls.other_case = Case.objects.create(owner=cls.outsider)
        cls.viewer_membership = CaseMembership.objects.create(
            case=cls.case, user=cls.viewer
        )
        cls.editor_membership = CaseMembership.objects.create(
            case=cls.case, user=cls.editor, role=CaseMembership.Role.EDITOR
        )
        cls.foreign_membership = CaseMembership.objects.create(
            case=cls.other_case, user=cls.editor
        )
        LogEntry.objects.log_actions(
            user_id=cls.owner.pk,
            queryset=Case.objects.filter(pk=cls.case.pk),
            action_flag=ADDITION,
            change_message="Synthetic case creation",
        )

    @staticmethod
    def staff(username, permissions=None):
        user = get_user_model().objects.create_user(username=username, is_staff=True)
        queryset = Permission.objects.filter(content_type__app_label="cases")
        if permissions is not None:
            queryset = queryset.filter(codename__in=permissions)
        user.user_permissions.set(queryset)
        return user

    @staticmethod
    def url(action="changelist", case=None):
        return reverse(f"admin:cases_case_{action}", args=[case.pk] if case else None)

    def membership_data(self, rows, initial=0):
        data = {
            "memberships-TOTAL_FORMS": len(rows),
            "memberships-INITIAL_FORMS": initial,
            "memberships-MIN_NUM_FORMS": 0,
            "memberships-MAX_NUM_FORMS": 1000,
            "_save": "Save",
        }
        for index, row in enumerate(rows):
            for key, value in row.items():
                data[f"memberships-{index}-{key}"] = value
        return data

    def test_creation_assigns_request_user_and_ignores_forged_fields(self):
        self.client.force_login(self.owner)
        response = self.client.get(self.url("add"))
        self.assertContains(response, "Save to create a case owned by you.")
        self.assertNotContains(response, 'name="owner"')
        self.assertFalse(response.context["inline_admin_formsets"])
        response = self.client.post(
            self.url("add"),
            {
                "owner": self.outsider.pk,
                "created_at": "2000-01-01T00:00:00Z",
                **self.membership_data([{"user": self.viewer.pk, "role": "editor"}]),
            },
        )
        self.assertRedirects(response, self.url())
        case = Case.objects.latest("created_at")
        self.assertEqual(case.owner, self.owner)
        self.assertGreater(case.created_at.year, 2000)
        self.assertFalse(case.memberships.exists())

    def test_add_only_staff_can_create_but_cannot_inspect_case(self):
        user = self.staff("creator", ["add_case"])
        self.client.force_login(user)
        response = self.client.post(self.url("add"), {"_save": "Save"})
        self.assertEqual(response.status_code, 302)
        case = Case.objects.get(owner=user)
        self.assertEqual(case.owner, user)
        self.assertEqual(self.client.get(self.url("change", case)).status_code, 403)

    def test_admin_requires_authenticated_staff(self):
        self.assertEqual(self.client.get(self.url()).status_code, 302)
        self.owner.is_staff = False
        self.owner.save()
        self.client.force_login(self.owner)
        for action, case in (
            ("changelist", None),
            ("add", None),
            ("change", self.case),
        ):
            with self.subTest(action=action):
                self.assertEqual(
                    self.client.get(self.url(action, case)).status_code, 302
                )
                self.assertEqual(
                    self.client.post(self.url(action, case)).status_code, 302
                )

    def test_staff_without_model_permissions_cannot_access_case_admin(self):
        self.owner.user_permissions.clear()
        self.client.force_login(self.owner)
        for action, case in (
            ("changelist", None),
            ("add", None),
            ("change", self.case),
            ("history", self.case),
            ("delete", self.case),
        ):
            with self.subTest(action=action):
                self.assertEqual(
                    self.client.get(self.url(action, case)).status_code, 403
                )
                self.assertEqual(
                    self.client.post(self.url(action, case)).status_code, 403
                )

    def test_case_lists_include_only_owned_and_shared_cases_without_duplicates(self):
        for user, expected in (
            (self.owner, [self.case]),
            (self.viewer, [self.case]),
            (self.editor, [self.case, self.other_case]),
            (self.outsider, [self.other_case]),
            (self.superuser, []),
        ):
            with self.subTest(user=user):
                self.client.force_login(user)
                response = self.client.get(self.url())
                self.assertEqual(response.status_code, 200)
                self.assertCountEqual(response.context["cl"].queryset, expected)
                self.assertNotContains(response, 'name="action"')

    def test_viewers_and_editors_can_only_inspect_metadata_and_history(self):
        for user in (self.viewer, self.editor):
            with self.subTest(user=user):
                self.client.force_login(user)
                response = self.client.get(self.url("change", self.case))
                self.assertContains(response, str(self.case))
                self.assertContains(response, self.owner.username)
                self.assertFalse(response.context["has_change_permission"])
                self.assertFalse(response.context["has_delete_permission"])
                self.assertFalse(response.context["inline_admin_formsets"])
                self.assertNotContains(response, 'name="_save"')
                response = self.client.get(self.url("history", self.case))
                self.assertContains(response, "Synthetic case creation")
                self.assertEqual(
                    self.client.get(self.url("delete", self.case)).status_code, 403
                )

    def test_view_permission_is_sufficient_to_inspect_shared_case(self):
        self.viewer.user_permissions.set(
            Permission.objects.filter(
                content_type__app_label="cases", codename="view_case"
            )
        )
        self.client.force_login(self.viewer)
        self.assertEqual(
            self.client.get(self.url("change", self.case)).status_code, 200
        )
        self.assertEqual(
            self.client.post(
                self.url("change", self.case), {"_save": "Save"}
            ).status_code,
            403,
        )
        self.assertEqual(self.client.get(self.url("add")).status_code, 403)

    def test_outsider_and_unrelated_superuser_cannot_access_direct_urls(self):
        for user in (self.outsider, self.superuser):
            self.client.force_login(user)
            for action in ("change", "history", "delete"):
                with self.subTest(user=user, action=action):
                    response = self.client.get(self.url(action, self.case))
                    self.assertRedirects(response, reverse("admin:index"))
                    response = self.client.post(
                        self.url(action, self.case), {"post": "yes", "_save": "Save"}
                    )
                    self.assertRedirects(response, reverse("admin:index"))
        self.assertTrue(Case.objects.filter(pk=self.case.pk).exists())

    def test_shared_superuser_has_no_ownership_privileges(self):
        CaseMembership.objects.create(
            case=self.case, user=self.superuser, role=CaseMembership.Role.EDITOR
        )
        self.client.force_login(self.superuser)
        response = self.client.get(self.url("change", self.case))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["inline_admin_formsets"])
        self.assertEqual(
            self.client.post(self.url("change", self.case)).status_code, 403
        )
        self.assertEqual(
            self.client.post(self.url("delete", self.case)).status_code, 403
        )

    def test_owner_sees_readonly_metadata_and_membership_inline(self):
        self.client.force_login(self.owner)
        response = self.client.get(self.url("change", self.case))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="owner"')
        self.assertNotContains(response, 'name="created_at"')
        formset = response.context["inline_admin_formsets"][0].formset
        self.assertCountEqual(
            formset.queryset, [self.viewer_membership, self.editor_membership]
        )
        self.assertContains(response, 'name="memberships-0-role"')

    def test_owner_can_add_change_and_remove_collaborators(self):
        case = Case.objects.create(owner=self.owner)
        self.client.force_login(self.owner)
        response = self.client.post(
            self.url("change", case),
            self.membership_data([{"user": self.viewer.pk, "role": "viewer"}]),
        )
        self.assertRedirects(response, self.url())
        membership = case.memberships.get()
        response = self.client.post(
            self.url("change", case),
            self.membership_data(
                [{"id": membership.pk, "user": self.viewer.pk, "role": "editor"}],
                initial=1,
            ),
        )
        self.assertRedirects(response, self.url())
        membership.refresh_from_db()
        self.assertEqual(membership.role, "editor")
        response = self.client.post(
            self.url("change", case),
            self.membership_data(
                [
                    {
                        "id": membership.pk,
                        "user": self.viewer.pk,
                        "role": "editor",
                        "DELETE": "on",
                    }
                ],
                initial=1,
            ),
        )
        self.assertRedirects(response, self.url())
        self.assertFalse(case.memberships.exists())
        case.refresh_from_db()
        self.assertEqual(case.owner, self.owner)

    def test_members_cannot_post_forged_ownership_or_membership_changes(self):
        for user in (self.viewer, self.editor):
            with self.subTest(user=user):
                self.client.force_login(user)
                response = self.client.post(
                    self.url("change", self.case),
                    {
                        "owner": user.pk,
                        **self.membership_data(
                            [
                                {
                                    "id": self.viewer_membership.pk,
                                    "user": self.outsider.pk,
                                    "role": "editor",
                                    "DELETE": "on",
                                }
                            ],
                            initial=1,
                        ),
                    },
                )
                self.assertEqual(response.status_code, 403)
                self.assertEqual(
                    self.client.post(
                        self.url("delete", self.case), {"post": "yes"}
                    ).status_code,
                    403,
                )
        self.case.refresh_from_db()
        self.viewer_membership.refresh_from_db()
        self.assertEqual(self.case.owner, self.owner)
        self.assertEqual(self.viewer_membership.user, self.viewer)
        self.assertEqual(self.viewer_membership.role, "viewer")

    def test_forged_owner_and_timestamp_are_ignored_on_change(self):
        self.client.force_login(self.owner)
        case = Case.objects.create(owner=self.owner)
        created_at = case.created_at
        response = self.client.post(
            self.url("change", case),
            {
                "owner": self.outsider.pk,
                "created_at": "2000-01-01T00:00:00Z",
                **self.membership_data([]),
            },
        )
        self.assertRedirects(response, self.url())
        case.refresh_from_db()
        self.assertEqual(case.owner, self.owner)
        self.assertEqual(case.created_at, created_at)

    def test_admin_rejects_owner_invalid_role_and_duplicate_collaborators(self):
        self.client.force_login(self.owner)
        for rows, error_field in (
            ([{"user": self.owner.pk, "role": "viewer"}], "user"),
            ([{"user": self.viewer.pk, "role": "owner"}], "role"),
            (
                [
                    {"user": self.viewer.pk, "role": "viewer"},
                    {"user": self.viewer.pk, "role": "editor"},
                ],
                "__all__",
            ),
        ):
            with self.subTest(rows=rows):
                case = Case.objects.create(owner=self.owner)
                response = self.client.post(
                    self.url("change", case), self.membership_data(rows)
                )
                self.assertEqual(response.status_code, 200)
                formset = response.context["inline_admin_formsets"][0].formset
                self.assertTrue(any(error_field in form.errors for form in formset))
                self.assertFalse(case.memberships.exists())

    def test_forged_membership_ids_cannot_change_another_case(self):
        # Even another case belonging to this owner must remain isolated.
        self.other_case.owner = self.owner
        self.other_case.save()
        self.client.force_login(self.owner)
        for membership_id in (self.foreign_membership.pk, uuid4(), "invalid-uuid"):
            with self.subTest(membership_id=membership_id):
                response = self.client.post(
                    self.url("change", self.case),
                    self.membership_data(
                        [
                            {
                                "id": membership_id,
                                "user": self.outsider.pk,
                                "role": "editor",
                            }
                        ],
                        initial=1,
                    ),
                )
                self.assertEqual(response.status_code, 200)
                formset = response.context["inline_admin_formsets"][0].formset
                self.assertIn("id", formset.forms[0].errors)
        self.foreign_membership.refresh_from_db()
        self.assertEqual(self.foreign_membership.case, self.other_case)
        self.assertEqual(self.foreign_membership.user, self.editor)
        self.assertEqual(self.foreign_membership.role, "viewer")

    def test_forged_membership_id_cannot_delete_from_another_case(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            self.url("change", self.case),
            self.membership_data(
                [
                    {
                        "id": self.foreign_membership.pk,
                        "user": self.editor.pk,
                        "role": "viewer",
                        "DELETE": "on",
                    }
                ],
                initial=1,
            ),
        )
        self.assertEqual(response.status_code, 302)
        self.foreign_membership.refresh_from_db()
        self.assertEqual(self.foreign_membership.case, self.other_case)
        self.assertEqual(self.case.memberships.count(), 2)

    def test_forged_parent_case_is_rejected(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            self.url("change", self.case),
            self.membership_data(
                [
                    {
                        "id": self.viewer_membership.pk,
                        "case": self.other_case.pk,
                        "user": self.viewer.pk,
                        "role": "editor",
                    }
                ],
                initial=1,
            ),
        )
        self.assertEqual(response.status_code, 200)
        formset = response.context["inline_admin_formsets"][0].formset
        self.assertIn("case", formset.forms[0].errors)
        self.viewer_membership.refresh_from_db()
        self.assertEqual(self.viewer_membership.case, self.case)
        self.assertEqual(self.viewer_membership.role, "viewer")

    def test_membership_permissions_are_required_for_each_operation(self):
        for denied, row, initial in (
            ("add_casemembership", {"user": self.outsider.pk, "role": "viewer"}, 0),
            (
                "change_casemembership",
                {
                    "id": self.viewer_membership.pk,
                    "user": self.viewer.pk,
                    "role": "editor",
                },
                1,
            ),
            (
                "delete_casemembership",
                {
                    "id": self.viewer_membership.pk,
                    "user": self.viewer.pk,
                    "role": "viewer",
                    "DELETE": "on",
                },
                1,
            ),
        ):
            with self.subTest(denied=denied):
                self.owner.user_permissions.set(
                    Permission.objects.filter(content_type__app_label="cases").exclude(
                        codename=denied
                    )
                )
                self.client.force_login(self.owner)
                self.client.post(
                    self.url("change", self.case),
                    self.membership_data([row], initial=initial),
                )
                self.assertFalse(
                    self.case.memberships.filter(user=self.outsider).exists()
                )
                self.viewer_membership.refresh_from_db()
                self.assertEqual(self.viewer_membership.role, "viewer")
                self.assertEqual(self.viewer_membership.user, self.viewer)

    def test_owner_still_needs_case_change_and_delete_permissions(self):
        self.owner.user_permissions.remove(
            *Permission.objects.filter(
                content_type__app_label="cases",
                codename__in=("change_case", "delete_case"),
            )
        )
        self.client.force_login(self.owner)
        self.assertEqual(
            self.client.post(self.url("change", self.case)).status_code, 403
        )
        for method in (self.client.get, self.client.post):
            self.assertEqual(method(self.url("delete", self.case)).status_code, 403)

    def test_owner_can_delete_case_and_memberships(self):
        self.client.force_login(self.owner)
        response = self.client.get(self.url("delete", self.case))
        self.assertEqual(response.status_code, 200)
        response = self.client.post(self.url("delete", self.case), {"post": "yes"})
        self.assertRedirects(response, self.url())
        self.assertFalse(Case.objects.filter(pk=self.case.pk).exists())
        self.assertFalse(CaseMembership.objects.filter(case_id=self.case.pk).exists())
        self.assertTrue(get_user_model().objects.filter(pk=self.owner.pk).exists())

    def test_admin_bulk_deletion_is_disabled(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            self.url(),
            {
                "action": "delete_selected",
                "_selected_action": self.case.pk,
                "post": "yes",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Case.objects.filter(pk=self.case.pk).exists())

    def test_revocation_takes_effect_on_next_requests_in_existing_session(self):
        self.client.force_login(self.viewer)
        self.assertEqual(
            self.client.get(self.url("change", self.case)).status_code, 200
        )
        owner_client = Client()
        owner_client.force_login(self.owner)
        response = owner_client.post(
            self.url("change", self.case),
            self.membership_data(
                [
                    {
                        "id": self.viewer_membership.pk,
                        "user": self.viewer.pk,
                        "role": "viewer",
                        "DELETE": "on",
                    }
                ],
                initial=1,
            ),
        )
        self.assertRedirects(response, self.url())
        self.assertFalse(self.client.get(self.url()).context["cl"].queryset.exists())
        for action in ("change", "history", "delete"):
            with self.subTest(action=action):
                self.assertRedirects(
                    self.client.get(self.url(action, self.case)), reverse("admin:index")
                )
                self.assertRedirects(
                    self.client.post(self.url(action, self.case)),
                    reverse("admin:index"),
                )

    def test_case_admin_posts_require_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        for action, case in (
            ("add", None),
            ("change", self.case),
            ("delete", self.case),
        ):
            with self.subTest(action=action):
                self.assertEqual(client.post(self.url(action, case)).status_code, 403)

    def test_owner_user_deletion_is_protected_in_admin(self):
        self.client.force_login(self.superuser)
        url = reverse("admin:accounts_user_delete", args=[self.owner.pk])
        response = self.client.post(url, {"post": "yes"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["protected"])
        self.assertTrue(get_user_model().objects.filter(pk=self.owner.pk).exists())
        self.assertTrue(Case.objects.filter(pk=self.case.pk).exists())

    def test_account_delete_keeps_protected_case_labels_within_current_access(self):
        person = Person.objects.create(case=self.case, first_name="Private person")
        poi = PointOfInterest.objects.create(
            person=person,
            kind="custom",
            custom_label="Private placement",
            created_by=self.editor,
            latitude=1,
            longitude=2,
        )
        self.client.force_login(self.superuser)
        url = reverse("admin:accounts_user_delete", args=[self.editor.pk])
        for response in (self.client.get(url), self.client.post(url, {"post": "yes"})):
            self.assertContains(
                response, "This account is required by retained case records."
            )
            self.assertNotContains(response, str(poi))
        self.assertTrue(get_user_model().objects.filter(pk=self.editor.pk).exists())
        self.assertTrue(PointOfInterest.objects.filter(pk=poi.pk).exists())

        CaseMembership.objects.create(case=self.case, user=self.superuser)
        self.assertContains(self.client.get(url), str(poi))

    def test_account_delete_summarizes_only_the_account_and_removes_memberships(self):
        self.client.force_login(self.superuser)
        url = reverse("admin:accounts_user_delete", args=[self.editor.pk])
        response = self.client.get(url)
        self.assertContains(response, self.editor.username)
        self.assertEqual(response.context["deleted_objects"], [self.editor.username])
        self.assertEqual(dict(response.context["model_count"]), {"users": 1})
        self.assertNotContains(response, str(self.editor_membership))
        self.assertNotContains(response, str(self.foreign_membership))

        response = self.client.post(url, {"post": "yes"})
        self.assertRedirects(response, reverse("admin:accounts_user_changelist"))
        self.assertFalse(get_user_model().objects.filter(pk=self.editor.pk).exists())
        self.assertFalse(CaseMembership.objects.filter(user_id=self.editor.pk).exists())
        self.assertTrue(Case.objects.filter(pk=self.case.pk).exists())
        self.assertTrue(Case.objects.filter(pk=self.other_case.pk).exists())

    def test_memberships_have_no_standalone_admin(self):
        self.assertFalse(admin.site.is_registered(CaseMembership))
