from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.templatetags.static import static
from django.test import TestCase
from django.urls import reverse

from cases.models import Case, CaseMembership
from text.models import TextEvidence


class MarketingPageTests(TestCase):
    pages = ("home", "product", "trust", "roadmap", "hosting")

    @classmethod
    def setUpTestData(cls):
        cls.owner = get_user_model().objects.create_user(
            username="marketing-owner", email="marketing-owner@example.com"
        )
        EmailAddress.objects.create(
            user=cls.owner, email=cls.owner.email, verified=True, primary=True
        )
        cls.case = Case.objects.create(
            owner=cls.owner, name="Private case sentinel 97341"
        )
        cls.evidence = TextEvidence.objects.create(
            case=cls.case,
            uploaded_by=cls.owner,
            title="Private evidence sentinel 24973",
            body="Private source sentinel 54832",
        )

    def test_public_pages_and_navigation_work_without_an_account(self):
        for name in self.pages:
            with self.subTest(page=name):
                response = self.client.get(reverse(f"marketing:{name}"))
                self.assertEqual(response.status_code, 200)
                self.assertTemplateUsed(response, "marketing/base.html")
                self.assertContains(response, 'href="#main-content"')
                self.assertContains(response, reverse("account_login"))
                for destination in self.pages:
                    self.assertContains(
                        response, f'href="{reverse(f"marketing:{destination}")}"'
                    )
                self.assertEqual(
                    self.client.head(reverse(f"marketing:{name}")).status_code, 200
                )
                self.assertEqual(
                    self.client.post(reverse(f"marketing:{name}")).status_code, 405
                )

    def test_marketing_loads_its_styles_without_the_product_stylesheet(self):
        response = self.client.get(reverse("marketing:product"))
        self.assertContains(response, static("ui/primitives.css"))
        self.assertContains(response, static("marketing/marketing.css"))
        self.assertNotContains(response, static("ui/app.css"))
        self.assertNotContains(response, static("core/evidence.js"))

    def test_public_pages_never_include_private_case_material(self):
        users = [None, self.owner]
        for role in ("viewer", "editor", "unrelated", "revoked"):
            user = get_user_model().objects.create_user(username=f"marketing-{role}")
            if role != "unrelated":
                membership = CaseMembership.objects.create(
                    case=self.case,
                    user=user,
                    role="viewer" if role == "revoked" else role,
                )
                if role == "revoked":
                    membership.delete()
            users.append(user)
        for user in users:
            self.client.logout()
            if user:
                self.client.force_login(user)
            for name in self.pages if user is None else self.pages[1:]:
                with self.subTest(user=str(user), page=name):
                    response = self.client.get(reverse(f"marketing:{name}"))
                    for private_value in (
                        self.case.name,
                        str(self.case.pk),
                        self.evidence.title,
                        self.evidence.body,
                        str(self.evidence.pk),
                    ):
                        self.assertNotContains(response, private_value)

    def test_signed_in_visitors_keep_the_workspace_shortcut(self):
        self.client.force_login(self.owner)
        self.assertRedirects(
            self.client.get(reverse("marketing:home")), reverse("cases:case_list")
        )
        for name in self.pages[1:]:
            response = self.client.get(reverse(f"marketing:{name}"))
            header = (
                response.content.decode()
                .split("<header", 1)[1]
                .split("</header>", 1)[0]
            )
            self.assertIn("Open workspace", header)
            self.assertIn(reverse("cases:case_list"), header)
            self.assertNotIn(reverse("account_login"), header)

    def test_marketing_does_not_bypass_case_authentication_or_verification(self):
        case_url = reverse("cases:case_detail", args=(self.case.pk,))
        self.assertRedirects(
            self.client.get(case_url), reverse("account_login") + f"?next={case_url}"
        )
        EmailAddress.objects.filter(user=self.owner).update(verified=False)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("marketing:product")).status_code, 200)
        self.assertRedirects(self.client.get(case_url), reverse("account_email"))
        response = self.client.get(case_url, HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "verification_required")
