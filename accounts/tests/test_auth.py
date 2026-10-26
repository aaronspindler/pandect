from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client, TestCase
from django.urls import reverse


class AuthenticationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.password = "synthetic-correct-horse-742!"
        cls.user = get_user_model().objects.create_user(
            username="investigator",
            email="investigator@example.com",
            password=cls.password,
        )
        EmailAddress.objects.create(
            user=cls.user, email=cls.user.email, primary=True, verified=True
        )

    def test_user_password_is_hashed_and_custom_model_is_active(self):
        self.assertEqual(self.user._meta.label, "accounts.User")
        self.assertNotEqual(self.user.password, self.password)
        self.assertTrue(self.user.check_password(self.password))

    def test_verified_user_can_sign_in_with_email(self):
        response = self.client.post(
            reverse("account_login"),
            {"login": self.user.email, "password": self.password},
        )
        self.assertRedirects(response, reverse("cases:case_list"))
        self.assertEqual(self.client.session["_auth_user_id"], str(self.user.pk))
        self.assertEqual(
            self.client.get(reverse("account_email")).wsgi_request.user, self.user
        )

    def test_incorrect_password_does_not_create_session(self):
        response = self.client.post(
            reverse("account_login"),
            {"login": self.user.username, "password": "incorrect-password"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_unverified_user_must_confirm_email_before_signing_in(self):
        EmailAddress.objects.filter(user=self.user).update(verified=False)
        response = self.client.post(
            reverse("account_login"),
            {"login": self.user.username, "password": self.password},
        )
        self.assertRedirects(response, reverse("account_email_verification_sent"))
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(mail.outbox[0].to, [self.user.email])

    def test_signup_creates_custom_user_and_requires_email_verification(self):
        response = self.client.post(
            reverse("account_signup"),
            {
                "username": "new-investigator",
                "email": "new@example.com",
                "password1": self.password,
                "password2": self.password,
            },
        )
        self.assertRedirects(response, reverse("account_email_verification_sent"))
        user = get_user_model().objects.get(username="new-investigator")
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertFalse(EmailAddress.objects.get(user=user).verified)
        self.assertEqual(mail.outbox[0].to, ["new@example.com"])

    def test_login_requires_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        response = client.post(
            reverse("account_login"),
            {"login": self.user.username, "password": self.password},
        )
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("_auth_user_id", client.session)

    def test_logout_requires_post(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("account_logout")).status_code, 200)
        self.assertIn("_auth_user_id", self.client.session)
        self.client.post(reverse("account_logout"))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_admin_requires_staff_access(self):
        response = self.client.get(reverse("admin:index"))
        self.assertRedirects(
            response, f"{reverse('admin:login')}?next={reverse('admin:index')}"
        )
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("admin:index")).status_code, 302)

    def test_superuser_can_access_admin(self):
        superuser = get_user_model().objects.create_superuser(
            username="operator", email="operator@example.com", password=self.password
        )
        self.client.force_login(superuser)
        self.assertEqual(self.client.get(reverse("admin:index")).status_code, 200)
