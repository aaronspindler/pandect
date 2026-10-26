from django.contrib.admin.models import ADDITION, CHANGE, DELETION, LogEntry
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client, TestCase
from django.urls import reverse

from cases.models import Case, CaseMembership
from evidence.models import PersonLocationEvidence
from people.models import Person


class PersonAdminTests(TestCase):
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
        CaseMembership.objects.create(case=cls.other_case, user=cls.editor)
        cls.person = Person.objects.create(
            case=cls.case,
            first_name="Alex",
            last_name="Example",
            email="alex@example.com",
            phone_number="+1 202-555-0100",
        )
        cls.other_person = Person.objects.create(
            case=cls.other_case, first_name="Hidden", email="hidden@example.com"
        )
        LogEntry.objects.log_actions(
            user_id=cls.owner.pk,
            queryset=Person.objects.filter(pk=cls.person.pk),
            action_flag=ADDITION,
            change_message="Synthetic person creation",
        )

    @staticmethod
    def staff(username):
        user = get_user_model().objects.create_user(username=username, is_staff=True)
        user.user_permissions.set(
            Permission.objects.filter(content_type__app_label__in=("people", "cases"))
        )
        return user

    @staticmethod
    def url(action="changelist", person=None):
        return reverse(
            f"admin:people_person_{action}", args=[person.pk] if person else None
        )

    def data(self, **overrides):
        return {
            "case": self.case.pk,
            "first_name": "Alex",
            "_save": "Save",
            **overrides,
        }

    def test_admin_requires_authenticated_active_staff(self):
        for user in (None, self.owner):
            if user:
                user.is_staff = False
                user.save()
                self.client.force_login(user)
            for action, person in (
                ("changelist", None),
                ("add", None),
                ("change", self.person),
            ):
                with self.subTest(user=user, action=action):
                    for method in (self.client.get, self.client.post):
                        self.assertEqual(
                            method(self.url(action, person)).status_code, 302
                        )
        self.owner.is_staff = True
        self.owner.is_active = False
        self.owner.save()
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(self.url()).status_code, 302)

    def test_retained_person_location_blocks_individual_admin_deletion(self):
        item = PersonLocationEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            person=self.person,
            title="Synthetic reported location",
            address="Synthetic harbour office",
        )
        self.client.force_login(self.owner)
        url = self.url("delete", self.person)
        confirmation = self.client.get(url)
        self.assertEqual(confirmation.status_code, 200)
        self.assertTrue(confirmation.context["protected"])
        response = self.client.post(url, {"post": "yes"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Person.objects.filter(pk=self.person.pk).exists())
        self.assertTrue(PersonLocationEvidence.objects.filter(pk=item.pk).exists())

    def test_staff_without_person_permissions_cannot_access_admin(self):
        self.owner.user_permissions.remove(
            *Permission.objects.filter(content_type__app_label="people")
        )
        self.client.force_login(self.owner)
        for action, person in (
            ("changelist", None),
            ("add", None),
            ("change", self.person),
            ("history", self.person),
            ("delete", self.person),
        ):
            with self.subTest(action=action):
                for method in (self.client.get, self.client.post):
                    self.assertEqual(method(self.url(action, person)).status_code, 403)

    def test_lists_and_searches_are_case_scoped_without_duplicates(self):
        for user, expected in (
            (self.owner, [self.person]),
            (self.viewer, [self.person]),
            (self.editor, [self.person, self.other_person]),
            (self.outsider, [self.other_person]),
            (self.superuser, []),
        ):
            with self.subTest(user=user):
                self.client.force_login(user)
                response = self.client.get(self.url())
                self.assertEqual(response.status_code, 200)
                self.assertCountEqual(response.context["cl"].queryset, expected)
                self.assertNotContains(response, 'name="action"')
                response = self.client.get(self.url(), {"q": "hidden@example.com"})
                self.assertCountEqual(
                    response.context["cl"].queryset,
                    [person for person in expected if person == self.other_person],
                )

    def test_search_supports_names_email_and_phone(self):
        self.client.force_login(self.owner)
        for query in ("Alex", "Example", "alex@example.com", "555-0100"):
            with self.subTest(query=query):
                response = self.client.get(self.url(), {"q": query})
                self.assertEqual(list(response.context["cl"].queryset), [self.person])

    def test_view_only_permission_allows_reading_but_not_writing(self):
        self.viewer.user_permissions.set(
            Permission.objects.filter(
                content_type__app_label="people", codename="view_person"
            )
        )
        self.client.force_login(self.viewer)
        response = self.client.get(self.url("change", self.person))
        self.assertContains(response, "alex@example.com")
        self.assertFalse(response.context["has_change_permission"])
        self.assertNotContains(response, 'name="_save"')
        response = self.client.get(self.url("history", self.person))
        self.assertContains(response, "Synthetic person creation")
        self.assertEqual(
            self.client.post(self.url("change", self.person), self.data()).status_code,
            403,
        )
        self.assertEqual(self.client.get(self.url("add")).status_code, 403)

    def test_viewer_cannot_write_even_with_all_model_permissions(self):
        self.client.force_login(self.viewer)
        for action, person in (
            ("add", None),
            ("change", self.person),
            ("delete", self.person),
        ):
            with self.subTest(action=action):
                self.assertEqual(
                    self.client.post(
                        self.url(action, person), self.data(post="yes")
                    ).status_code,
                    403,
                )
        self.person.refresh_from_db()
        self.assertEqual(self.person.email, "alex@example.com")

    def test_owner_and_editor_can_create_and_edit_with_audit_history(self):
        for user in (self.owner, self.editor):
            with self.subTest(user=user):
                self.client.force_login(user)
                response = self.client.post(
                    self.url("add"), self.data(email="new@example.com")
                )
                self.assertRedirects(response, self.url())
                person = Person.objects.latest("created_at")
                self.assertEqual(person.case, self.case)
                self.assertEqual(person.email, "new@example.com")
                response = self.client.post(
                    self.url("change", person),
                    self.data(notes="Unconfirmed observation"),
                )
                self.assertRedirects(response, self.url())
                person.refresh_from_db()
                self.assertEqual(person.notes, "Unconfirmed observation")
                self.assertCountEqual(
                    LogEntry.objects.filter(
                        object_id=str(person.pk), user=user
                    ).values_list("action_flag", flat=True),
                    [ADDITION, CHANGE],
                )

    def test_creation_case_choices_include_only_owned_or_editor_cases(self):
        for user in (self.owner, self.editor):
            with self.subTest(user=user):
                self.client.force_login(user)
                response = self.client.get(self.url("add"))
                self.assertEqual(response.status_code, 200)
                self.assertCountEqual(
                    response.context["adminform"].form.fields["case"].queryset,
                    [self.case],
                )
                response = self.client.post(
                    self.url("add"), self.data(case=self.other_case.pk)
                )
                self.assertEqual(response.status_code, 200)
                self.assertIn("case", response.context["adminform"].form.errors)
        self.assertEqual(Person.objects.count(), 2)

    def test_no_editable_cases_denies_creation_including_unrelated_superuser(self):
        for user in (self.viewer, self.superuser):
            with self.subTest(user=user):
                self.client.force_login(user)
                self.assertEqual(self.client.get(self.url("add")).status_code, 403)
                self.assertEqual(
                    self.client.post(self.url("add"), self.data()).status_code, 403
                )

    def test_case_and_timestamps_cannot_be_forged_after_creation(self):
        other_owned_case = Case.objects.create(owner=self.owner)
        original_created_at = self.person.created_at
        for user, target in (
            (self.owner, other_owned_case),
            (self.editor, self.other_case),
        ):
            with self.subTest(user=user):
                self.client.force_login(user)
                response = self.client.get(self.url("change", self.person))
                self.assertNotContains(response, 'name="case"')
                self.assertNotContains(response, 'name="created_at"')
                self.assertNotContains(response, 'name="updated_at"')
                response = self.client.post(
                    self.url("change", self.person),
                    self.data(
                        case=target.pk, created_at="2000-01-01", updated_at="2000-01-01"
                    ),
                )
                self.assertRedirects(response, self.url())
                self.person.refresh_from_db()
                self.assertEqual(self.person.case, self.case)
                self.assertEqual(self.person.created_at, original_created_at)
                self.assertGreater(self.person.updated_at.year, 2000)

    def test_admin_rejects_empty_identity_and_invalid_email(self):
        self.client.force_login(self.owner)
        for values, field in (
            ({"first_name": " \t"}, "__all__"),
            ({"email": "invalid"}, "email"),
        ):
            with self.subTest(values=values):
                response = self.client.post(self.url("add"), self.data(**values))
                self.assertEqual(response.status_code, 200)
                self.assertIn(field, response.context["adminform"].form.errors)
        self.assertEqual(Person.objects.count(), 2)

    def test_outsider_and_unrelated_superuser_cannot_access_direct_urls(self):
        for user in (self.outsider, self.superuser):
            self.client.force_login(user)
            for action in ("change", "history", "delete"):
                with self.subTest(user=user, action=action):
                    self.assertRedirects(
                        self.client.get(self.url(action, self.person)),
                        reverse("admin:index"),
                    )
                    self.assertRedirects(
                        self.client.post(
                            self.url(action, self.person), self.data(post="yes")
                        ),
                        reverse("admin:index"),
                    )
        self.assertTrue(Person.objects.filter(pk=self.person.pk).exists())

    def test_shared_superuser_obeys_viewer_and_editor_roles(self):
        membership = CaseMembership.objects.create(case=self.case, user=self.superuser)
        self.client.force_login(self.superuser)
        self.assertEqual(
            self.client.get(self.url("change", self.person)).status_code, 200
        )
        self.assertEqual(
            self.client.post(self.url("change", self.person), self.data()).status_code,
            403,
        )
        membership.role = CaseMembership.Role.EDITOR
        membership.save()
        self.assertRedirects(
            self.client.post(
                self.url("change", self.person), self.data(notes="Edited")
            ),
            self.url(),
        )
        self.assertEqual(
            self.client.post(
                self.url("delete", self.person), {"post": "yes"}
            ).status_code,
            403,
        )

    def test_only_owner_can_delete_and_deletion_is_logged(self):
        self.client.force_login(self.editor)
        for method in (self.client.get, self.client.post):
            self.assertEqual(
                method(self.url("delete", self.person), {"post": "yes"}).status_code,
                403,
            )
        self.client.force_login(self.owner)
        self.assertEqual(
            self.client.get(self.url("delete", self.person)).status_code, 200
        )
        response = self.client.post(self.url("delete", self.person), {"post": "yes"})
        self.assertRedirects(response, self.url())
        self.assertFalse(Person.objects.filter(pk=self.person.pk).exists())
        self.assertTrue(
            LogEntry.objects.filter(
                object_id=str(self.person.pk), action_flag=DELETION
            ).exists()
        )
        self.assertTrue(Person.objects.filter(pk=self.other_person.pk).exists())

    def test_model_permissions_are_required_for_each_write_operation(self):
        for denied, action, person in (
            ("add_person", "add", None),
            ("change_person", "change", self.person),
            ("delete_person", "delete", self.person),
        ):
            with self.subTest(denied=denied):
                self.owner.user_permissions.set(
                    Permission.objects.filter(content_type__app_label="people").exclude(
                        codename=denied
                    )
                )
                self.client.force_login(self.owner)
                response = self.client.post(
                    self.url(action, person), self.data(post="yes")
                )
                self.assertEqual(response.status_code, 403)
        self.assertEqual(Person.objects.count(), 2)

    def test_revoked_membership_takes_effect_in_an_existing_session(self):
        self.client.force_login(self.editor)
        self.assertEqual(
            self.client.get(self.url("change", self.person)).status_code, 200
        )
        self.assertEqual(self.client.get(self.url("add")).status_code, 200)
        self.editor_membership.delete()
        response = self.client.get(self.url())
        self.assertEqual(list(response.context["cl"].queryset), [self.other_person])
        self.assertEqual(
            self.client.post(self.url("add"), self.data()).status_code, 403
        )
        for action in ("change", "history", "delete"):
            with self.subTest(action=action):
                self.assertRedirects(
                    self.client.get(self.url(action, self.person)),
                    reverse("admin:index"),
                )
                self.assertRedirects(
                    self.client.post(
                        self.url(action, self.person), self.data(post="yes")
                    ),
                    reverse("admin:index"),
                )
        self.person.refresh_from_db()
        self.assertEqual(self.person.email, "alex@example.com")

    def test_editor_downgrade_between_get_and_post_prevents_writes(self):
        self.client.force_login(self.editor)
        self.assertEqual(self.client.get(self.url("add")).status_code, 200)
        self.assertEqual(
            self.client.get(self.url("change", self.person)).status_code, 200
        )
        self.editor_membership.role = CaseMembership.Role.VIEWER
        self.editor_membership.save()
        self.assertEqual(
            self.client.post(self.url("add"), self.data()).status_code, 403
        )
        self.assertEqual(
            self.client.post(self.url("change", self.person), self.data()).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(self.url("change", self.person)).status_code, 200
        )

    def test_bulk_deletion_is_disabled(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            self.url(),
            {
                "action": "delete_selected",
                "_selected_action": self.person.pk,
                "post": "yes",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Person.objects.filter(pk=self.person.pk).exists())

    def test_csrf_is_required_for_writes(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        for action, person in (
            ("add", None),
            ("change", self.person),
            ("delete", self.person),
        ):
            with self.subTest(action=action):
                self.assertEqual(
                    client.post(
                        self.url(action, person), self.data(post="yes")
                    ).status_code,
                    403,
                )

    def test_case_deletion_requires_person_delete_permission_and_cascades(self):
        permission = Permission.objects.get(
            content_type__app_label="people", codename="delete_person"
        )
        self.owner.user_permissions.remove(permission)
        self.client.force_login(self.owner)
        url = reverse("admin:cases_case_delete", args=[self.case.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("person", response.context["perms_lacking"])
        self.assertEqual(self.client.post(url, {"post": "yes"}).status_code, 403)
        self.assertTrue(Person.objects.filter(pk=self.person.pk).exists())
        self.owner.user_permissions.add(permission)
        self.client.force_login(self.owner)
        response = self.client.post(url, {"post": "yes"})
        self.assertRedirects(response, reverse("admin:cases_case_changelist"))
        self.assertFalse(Case.objects.filter(pk=self.case.pk).exists())
        self.assertFalse(Person.objects.filter(pk=self.person.pk).exists())
        self.assertTrue(Person.objects.filter(pk=self.other_person.pk).exists())
