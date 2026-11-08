from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from cases.models import Case, CaseMembership
from people.models import Person
from socialmedia.models import SocialAccount


class SocialAccountTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        for role in ("owner", "editor", "viewer", "outsider"):
            user = get_user_model().objects.create_user(
                username=f"social-{role}", email=f"social-{role}@example.com"
            )
            EmailAddress.objects.create(
                user=user, email=user.email, primary=True, verified=True
            )
            setattr(cls, role, user)
        cls.case = Case.objects.create(owner=cls.owner, name="Social accounts case")
        cls.other_case = Case.objects.create(owner=cls.outsider, name="Private case")
        cls.editor_membership = CaseMembership.objects.create(
            case=cls.case, user=cls.editor, role=CaseMembership.Role.EDITOR
        )
        CaseMembership.objects.create(case=cls.case, user=cls.viewer)
        cls.person = Person.objects.create(case=cls.case, first_name="Alex Example")
        cls.another_person = Person.objects.create(
            case=cls.case, first_name="Sam Example"
        )
        cls.other_person = Person.objects.create(
            case=cls.other_case, first_name="Private Example"
        )
        cls.account = SocialAccount.objects.create(
            person=cls.person, platform="Example Social", username="@Alex_Example"
        )
        cls.other_account = SocialAccount.objects.create(
            person=cls.other_person, platform="Example Social", username="@Private"
        )

    def account_url(self, action, *, person=None, account=None):
        args = [self.case.pk, (person or self.person).pk]
        if action != "create":
            args.append((account or self.account).pk)
        return reverse(f"socialmedia:social_account_{action}", args=args)

    def detail_url(self):
        return reverse("people:detail", args=(self.case.pk, self.person.pk))

    def test_owner_adds_and_editor_corrects_and_removes_account(self):
        self.client.force_login(self.owner)
        data = {
            "platform": "Example Social",
            "username": "@Alex_Alternate",
            "source": "Synthetic source citation",
            "person": self.other_person.pk,
        }
        response = self.client.post(self.account_url("create"), data, follow=True)
        self.assertContains(response, data["username"])
        self.assertContains(response, data["source"])
        self.assertContains(response, self.account.username)
        self.assertNotContains(response, self.other_account.username)
        account = SocialAccount.objects.get(
            person=self.person, username=data["username"]
        )

        self.client.force_login(self.editor)
        edit_url = self.account_url("edit", account=account)
        data["revision"] = self.client.get(edit_url).context["form"]["revision"].value()
        data["username"] = "@Corrected_Name"
        self.assertEqual(self.client.post(edit_url, data).status_code, 302)
        account.refresh_from_db()
        self.assertEqual(account.username, data["username"])
        self.assertEqual(account.person_id, self.person.pk)

        delete_url = self.account_url("delete", account=account)
        response = self.client.get(delete_url)
        self.assertContains(response, data["username"])
        revision = response.context["form"]["revision"].value()
        self.assertEqual(
            self.client.post(delete_url, {"revision": revision}).status_code, 400
        )
        self.assertTrue(SocialAccount.objects.filter(pk=account.pk).exists())
        self.assertEqual(
            self.client.post(
                delete_url, {"revision": revision, "confirm": "on"}
            ).status_code,
            302,
        )
        self.assertFalse(SocialAccount.objects.filter(pk=account.pk).exists())
        self.assertEqual(self.client.post(edit_url, data).status_code, 404)
        self.assertTrue(SocialAccount.objects.filter(pk=self.account.pk).exists())

    def test_form_rejects_invalid_and_exact_duplicate_accounts(self):
        self.client.force_login(self.editor)
        url = self.account_url("create")
        values = {"platform": self.account.platform, "username": self.account.username}
        for changes, field in (
            ({"platform": " "}, "platform"),
            ({"username": "a" * 151}, "username"),
        ):
            with self.subTest(field=field):
                response = self.client.post(url, {**values, **changes})
                self.assertEqual(response.status_code, 400)
                self.assertIn(field, response.context["form"].errors)
        self.assertEqual(self.client.post(url, values).status_code, 400)
        self.assertEqual(self.person.social_accounts.count(), 1)

        self.assertEqual(
            self.client.post(url, {**values, "username": "@alex_example"}).status_code,
            302,
        )
        self.assertEqual(
            self.client.post(
                self.account_url("create", person=self.another_person), values
            ).status_code,
            302,
        )
        other_spelling = self.person.social_accounts.get(username="@alex_example")
        edit_url = self.account_url("edit", account=other_spelling)
        revision = self.client.get(edit_url).context["form"]["revision"].value()
        self.assertEqual(
            self.client.post(edit_url, {**values, "revision": revision}).status_code,
            400,
        )
        other_spelling.refresh_from_db()
        self.assertEqual(other_spelling.username, "@alex_example")

        with self.assertRaises(IntegrityError), transaction.atomic():
            SocialAccount.objects.create(person=self.person, **values)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.person.social_accounts.filter(pk=self.account.pk).update(username=" ")

    def test_viewer_can_read_and_search_with_one_result_per_person(self):
        SocialAccount.objects.create(
            person=self.person, platform="Example Social", username="@Alex_Second"
        )
        self.client.force_login(self.viewer)
        response = self.client.get(self.detail_url())
        self.assertContains(response, self.account.platform)
        self.assertContains(response, self.account.username)
        self.assertNotContains(response, self.other_account.username)
        for action in ("create", "edit", "delete"):
            url = self.account_url(action)
            self.assertNotContains(response, url)
            self.assertEqual(self.client.get(url).status_code, 403)
            self.assertEqual(self.client.post(url, {"confirm": "on"}).status_code, 403)

        list_url = reverse("people:list", args=(self.case.pk,))
        for query, people in (
            ("example social", [self.person]),
            ("@alex_", [self.person]),
            ("@private", []),
        ):
            with self.subTest(query=query):
                response = self.client.get(list_url, {"q": query})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(list(response.context["page_obj"]), people)

    def test_account_routes_reject_outsiders_and_mismatched_parent_ids(self):
        self.client.force_login(self.outsider)
        for action in ("create", "edit", "delete"):
            url = self.account_url(action)
            self.assertEqual(self.client.get(url).status_code, 404)
            self.assertEqual(self.client.post(url, {"confirm": "on"}).status_code, 404)

        self.client.force_login(self.owner)
        urls = [self.account_url("create", person=self.other_person)]
        for action in ("edit", "delete"):
            urls.extend(
                (
                    self.account_url(action, person=self.another_person),
                    self.account_url(action, account=self.other_account),
                )
            )
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)
                self.assertEqual(
                    self.client.post(url, {"confirm": "on"}).status_code, 404
                )
        self.assertTrue(SocialAccount.objects.filter(pk=self.other_account.pk).exists())

    def test_open_form_cannot_save_after_downgrade_or_revocation(self):
        self.client.force_login(self.editor)
        url = self.account_url("edit")
        data = {
            "platform": self.account.platform,
            "username": "@Blocked",
            "revision": self.client.get(url).context["form"]["revision"].value(),
        }
        self.editor_membership.role = CaseMembership.Role.VIEWER
        self.editor_membership.save(update_fields=["role"])
        self.assertEqual(self.client.post(url, data).status_code, 403)
        self.editor_membership.delete()
        self.assertEqual(self.client.post(url, data).status_code, 404)
        self.assertEqual(self.client.get(self.detail_url()).status_code, 404)
        self.account.refresh_from_db()
        self.assertEqual(self.account.username, "@Alex_Example")

    def test_stale_edit_preserves_draft_for_review(self):
        self.client.force_login(self.editor)
        url = self.account_url("edit")
        proposal = {
            "platform": self.account.platform,
            "username": "@Proposed_Name",
            "revision": self.client.get(url).context["form"]["revision"].value(),
        }
        self.account.username = "@Newer_Saved_Name"
        self.account.save()

        response = self.client.post(url, proposal)
        self.assertContains(response, "@Newer_Saved_Name", status_code=409)
        self.assertContains(response, "@Proposed_Name", status_code=409)
        self.account.refresh_from_db()
        self.assertEqual(self.account.username, "@Newer_Saved_Name")

        proposal["revision"] = response.context["form"]["revision"].value()
        self.assertEqual(self.client.post(url, proposal).status_code, 302)
        self.account.refresh_from_db()
        self.assertEqual(self.account.username, "@Proposed_Name")

    def test_removal_requires_review_of_updated_account(self):
        self.client.force_login(self.editor)
        url = self.account_url("delete")
        revision = self.client.get(url).context["form"]["revision"].value()
        self.account.username = "@Updated_Name"
        self.account.save()

        response = self.client.post(url, {"confirm": "on", "revision": revision})
        self.assertContains(response, self.account.username, status_code=409)
        self.assertFalse(response.context["form"]["confirm"].value())
        self.assertTrue(SocialAccount.objects.filter(pk=self.account.pk).exists())

        revision = response.context["form"]["revision"].value()
        self.assertEqual(
            self.client.post(url, {"confirm": "on", "revision": revision}).status_code,
            302,
        )
        self.assertFalse(SocialAccount.objects.filter(pk=self.account.pk).exists())

    def test_deleting_person_only_removes_their_accounts(self):
        self.client.force_login(self.owner)
        url = reverse("people:delete", args=(self.case.pk, self.person.pk))
        self.assertContains(self.client.get(url), "1 social account")
        self.assertEqual(self.client.post(url, {"confirm": "on"}).status_code, 302)
        self.assertFalse(SocialAccount.objects.filter(pk=self.account.pk).exists())
        self.assertTrue(SocialAccount.objects.filter(pk=self.other_account.pk).exists())

    def test_case_deletion_counts_and_removes_only_its_social_accounts(self):
        second_account = SocialAccount.objects.create(
            person=self.another_person, platform="Other Social", username="@Sam"
        )
        self.client.force_login(self.owner)
        url = reverse("cases:case_delete", args=(self.case.pk,))
        response = self.client.get(url)
        self.assertContains(response, "Person social accounts")
        self.assertEqual(response.context["social_account_count"], 2)
        self.assertEqual(self.client.post(url, {"confirm": "on"}).status_code, 302)
        self.assertFalse(
            SocialAccount.objects.filter(
                pk__in=(self.account.pk, second_account.pk)
            ).exists()
        )
        self.assertTrue(SocialAccount.objects.filter(pk=self.other_account.pk).exists())
