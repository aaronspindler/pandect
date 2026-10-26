from uuid import uuid4

from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from cases.models import Case, CaseMembership
from people.models import Person


class PersonInterfaceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = cls.user("person-owner")
        cls.editor = cls.user("person-editor")
        cls.viewer = cls.user("person-viewer")
        cls.outsider = cls.user("person-outsider")
        cls.operator = cls.user("person-operator", is_staff=True, is_superuser=True)
        cls.case = Case.objects.create(owner=cls.owner)
        cls.other_case = Case.objects.create(owner=cls.outsider)
        CaseMembership.objects.create(
            case=cls.case, user=cls.editor, role=CaseMembership.Role.EDITOR
        )
        CaseMembership.objects.create(case=cls.case, user=cls.viewer)
        cls.person = Person.objects.create(
            case=cls.case,
            first_name="Alex",
            last_name="Example",
            email="synthetic-alex@example.com",
            notes="Unconfirmed public source contact.",
        )
        cls.other_person = Person.objects.create(
            case=cls.other_case, first_name="Hidden source", notes="Other case secret"
        )

    @staticmethod
    def user(username, **kwargs):
        user = get_user_model().objects.create_user(
            username=username, email=f"{username}@example.com", **kwargs
        )
        EmailAddress.objects.create(
            user=user, email=user.email, verified=True, primary=True
        )
        return user

    def url(self, name="list", person=None, case=None):
        kwargs = {"case_id": (case or self.case).pk}
        if person is not None:
            kwargs["person_id"] = person.pk
        return reverse(f"people:{name}", kwargs=kwargs)

    def edit_data(self, person=None, **changes):
        person = person or self.person
        response = self.client.get(self.url("edit", person=person))
        self.assertEqual(response.status_code, 200)
        return {
            "first_name": person.first_name,
            "last_name": person.last_name,
            "phone_number": person.phone_number,
            "email": person.email,
            "address": person.address,
            "notes": person.notes,
            "revision": response.context["form"]["revision"].value(),
            **changes,
        }

    def test_accounts_need_authentication_and_verified_email(self):
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)
        unverified = get_user_model().objects.create_user(
            username="person-unverified", email="unverified@example.com"
        )
        EmailAddress.objects.create(
            user=unverified, email=unverified.email, primary=True, verified=False
        )
        self.client.force_login(unverified)
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Person.objects.filter(first_name="Unexpected").exists())

    def test_all_case_roles_can_inspect_but_lists_never_leak_other_cases(self):
        for user in (self.owner, self.editor, self.viewer):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                response = self.client.get(self.url())
                self.assertContains(response, "Alex Example")
                self.assertNotContains(response, "Hidden source")
                self.assertNotContains(response, "Other case secret")
                self.assertContains(
                    self.client.get(self.url("detail", person=self.person)),
                    "Unconfirmed public source contact.",
                )

    def test_unrelated_users_and_superusers_cannot_inspect_or_write(self):
        for user in (self.outsider, self.operator):
            self.client.force_login(user)
            for name, person in (
                ("list", None),
                ("create", None),
                ("detail", self.person),
                ("edit", self.person),
                ("delete", self.person),
            ):
                with self.subTest(user=user.username, name=name):
                    url = self.url(name, person=person)
                    self.assertEqual(self.client.get(url).status_code, 404)
                    self.assertEqual(
                        self.client.post(url, {"first_name": "Forged"}).status_code,
                        405 if name in ("list", "detail") else 404,
                    )

    def test_parent_case_and_person_must_match_on_every_object_path(self):
        self.client.force_login(self.owner)
        for name in ("detail", "edit", "delete"):
            with self.subTest(name=name):
                url = self.url(name, person=self.other_person)
                self.assertEqual(self.client.get(url).status_code, 404)
                self.assertEqual(
                    self.client.post(url, {"confirm": "on"}).status_code,
                    405 if name == "detail" else 404,
                )
        self.assertTrue(Person.objects.filter(pk=self.other_person.pk).exists())

    def test_owner_and_editor_create_partial_identities_without_staff_permissions(self):
        for user, identity in (
            (self.owner, {"email": "partial@example.com"}),
            (self.editor, {"phone_number": "+1 (202) 555-0100 ext. 42"}),
        ):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                response = self.client.post(
                    self.url("create"), {**identity, "case": str(self.other_case.pk)}
                )
                self.assertEqual(response.status_code, 302)
                person = Person.objects.get(case=self.case, **identity)
                self.assertRedirects(
                    response,
                    self.url("detail", person=person),
                    fetch_redirect_response=False,
                )
                self.assertFalse(user.is_staff)

    def test_blank_identity_and_invalid_email_preserve_entered_values(self):
        self.client.force_login(self.editor)
        response = self.client.post(
            self.url("create"), {"address": "Synthetic Street", "notes": "Unknown"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "Synthetic Street", status_code=400)
        self.assertContains(response, "Enter at least", status_code=400)
        response = self.client.post(
            self.url("create"),
            {"first_name": "Proposed name", "email": "invalid", "notes": "Keep this"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.context["form"].data["notes"], "Keep this")
        self.assertIn("email", response.context["form"].errors)
        self.assertEqual(Person.objects.filter(case=self.case).count(), 1)

    def test_duplicate_contacts_are_not_merged(self):
        self.client.force_login(self.editor)
        for _ in range(2):
            self.assertEqual(
                self.client.post(
                    self.url("create"), {"email": "duplicate@example.com"}
                ).status_code,
                302,
            )
        self.assertEqual(
            Person.objects.filter(
                case=self.case, email="duplicate@example.com"
            ).count(),
            2,
        )

    def test_owner_and_editor_can_edit_without_moving_the_person(self):
        for user in (self.owner, self.editor):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                self.person.refresh_from_db()
                response = self.client.post(
                    self.url("edit", person=self.person),
                    self.edit_data(
                        notes=f"Updated by {user.username}",
                        case=str(self.other_case.pk),
                    ),
                )
                self.assertEqual(response.status_code, 302)
                self.person.refresh_from_db()
                self.assertEqual(self.person.case_id, self.case.pk)
                self.assertEqual(self.person.notes, f"Updated by {user.username}")

    def test_viewer_is_read_only_and_mutations_remain_denied_directly(self):
        self.client.force_login(self.viewer)
        response = self.client.get(self.url("detail", person=self.person))
        self.assertContains(response, "viewer access")
        self.assertNotContains(response, "Edit person")
        self.assertNotContains(response, "Delete person")
        for name, person in (
            ("create", None),
            ("edit", self.person),
            ("delete", self.person),
        ):
            with self.subTest(name=name):
                self.assertEqual(
                    self.client.get(self.url(name, person)).status_code, 403
                )
                self.assertEqual(
                    self.client.post(
                        self.url(name, person),
                        {"first_name": "Forged", "confirm": "on"},
                    ).status_code,
                    403,
                )
        self.person.refresh_from_db()
        self.assertEqual(self.person.first_name, "Alex")

    def test_stale_edit_shows_saved_details_and_preserves_proposal_for_review(self):
        self.client.force_login(self.editor)
        proposal = self.edit_data(notes="My proposed notes")
        self.person.notes = "Newer saved notes"
        self.person.save()
        response = self.client.post(self.url("edit", self.person), proposal)
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "Newer saved notes", status_code=409)
        self.assertContains(response, "My proposed notes", status_code=409)
        self.assertContains(response, "Save reviewed changes", status_code=409)
        self.person.refresh_from_db()
        self.assertEqual(self.person.notes, "Newer saved notes")
        proposal["revision"] = response.context["form"]["revision"].value()
        reviewed = self.client.post(self.url("edit", self.person), proposal)
        self.assertEqual(reviewed.status_code, 302)
        self.person.refresh_from_db()
        self.assertEqual(self.person.notes, "My proposed notes")

    def test_revision_cannot_be_missing_tampered_or_reused_for_another_person(self):
        self.client.force_login(self.owner)
        data = self.edit_data(notes="Forged notes")
        for token in ("", "tampered", f"{data['revision']}tampered"):
            with self.subTest(token=token):
                response = self.client.post(
                    self.url("edit", self.person), {**data, "revision": token}
                )
                self.assertEqual(response.status_code, 400)
        another = Person.objects.create(case=self.case, first_name="Another")
        response = self.client.post(self.url("edit", another), data)
        self.assertEqual(response.status_code, 400)
        self.person.refresh_from_db()
        self.assertEqual(self.person.notes, "Unconfirmed public source contact.")

    def test_revocation_and_role_downgrade_apply_to_an_already_open_form(self):
        self.client.force_login(self.editor)
        data = self.edit_data(notes="No longer allowed")
        membership = CaseMembership.objects.get(case=self.case, user=self.editor)
        membership.role = CaseMembership.Role.VIEWER
        membership.save()
        self.assertEqual(
            self.client.post(self.url("edit", self.person), data).status_code, 403
        )
        membership.delete()
        self.assertEqual(self.client.get(self.url()).status_code, 404)
        self.assertEqual(
            self.client.post(self.url("edit", self.person), data).status_code, 404
        )
        self.person.refresh_from_db()
        self.assertEqual(self.person.notes, "Unconfirmed public source contact.")

    def test_deleted_person_is_not_recreated_by_a_stale_form(self):
        self.client.force_login(self.editor)
        data = self.edit_data(notes="Stale changes")
        url = self.url("edit", self.person)
        person_id = self.person.pk
        self.person.delete()
        self.assertEqual(self.client.post(url, data).status_code, 404)
        self.assertFalse(Person.objects.filter(pk=person_id).exists())

    def test_only_owner_can_delete_after_explicit_confirmation(self):
        url = self.url("delete", self.person)
        self.client.force_login(self.editor)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, {"confirm": "on"}).status_code, 403)
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(url), "Delete Alex Example?")
        self.assertTrue(Person.objects.filter(pk=self.person.pk).exists())
        self.assertEqual(self.client.post(url).status_code, 400)
        self.assertTrue(Person.objects.filter(pk=self.person.pk).exists())
        response = self.client.post(url, {"confirm": "on"})
        self.assertRedirects(response, self.url(), fetch_redirect_response=False)
        self.assertFalse(Person.objects.filter(pk=self.person.pk).exists())
        self.assertTrue(Person.objects.filter(pk=self.other_person.pk).exists())
        self.assertTrue(Case.objects.filter(pk=self.case.pk).exists())
        self.assertTrue(get_user_model().objects.filter(pk=self.owner.pk).exists())

    def test_form_mutations_require_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        self.assertEqual(client.get(self.url("create")).status_code, 200)
        self.assertEqual(
            client.post(self.url("create"), {"first_name": "Without CSRF"}).status_code,
            403,
        )
        self.assertEqual(
            client.post(self.url("delete", self.person), {"confirm": "on"}).status_code,
            403,
        )

    def test_search_and_pagination_remain_case_scoped_and_keep_query_context(self):
        Person.objects.bulk_create(
            [
                Person(case=self.case, first_name=f"Paginated person {index:02d}")
                for index in range(30)
            ]
        )
        self.client.force_login(self.viewer)
        response = self.client.get(self.url(), {"q": "Paginated person", "page": 1})
        self.assertEqual(response.context["page_obj"].paginator.count, 30)
        self.assertEqual(len(response.context["page_obj"]), 25)
        self.assertContains(response, "q=Paginated+person&amp;page=2")
        self.assertNotContains(response, "Hidden source")
        second = self.client.get(self.url(), {"q": "Paginated person", "page": 2})
        self.assertEqual(len(second.context["page_obj"]), 5)
        response = self.client.get(self.url(), {"q": "Other case secret"})
        self.assertContains(response, "No matching people")
        detail = self.client.get(
            self.url("detail", self.person), {"q": "Alex", "page": 2}
        )
        self.assertContains(detail, "?q=Alex&amp;page=2")

    def test_notes_are_escaped_and_missing_details_are_clear(self):
        self.person.notes = '<script>alert("untrusted")</script>\nUncertain source'
        self.person.save()
        self.client.force_login(self.viewer)
        response = self.client.get(self.url("detail", self.person))
        self.assertNotContains(response, '<script>alert("untrusted")</script>')
        self.assertContains(response, "&lt;script&gt;")
        self.assertContains(response, "Not recorded")
        self.assertContains(response, "Uncertain source")

    def test_unknown_uuid_and_non_uuid_paths_are_not_found(self):
        self.client.force_login(self.owner)
        url = reverse(
            "people:detail", kwargs={"case_id": self.case.pk, "person_id": uuid4()}
        )
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(
            self.client.get(url.replace(str(self.case.pk), "123")).status_code, 404
        )
