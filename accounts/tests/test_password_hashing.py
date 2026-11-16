from allauth.account.models import EmailAddress
from django.conf import global_settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import get_hasher, identify_hasher
from django.test import TestCase, override_settings
from django.urls import reverse

from config import settings as production_settings


@override_settings(
    PASSWORD_HASHERS=getattr(
        production_settings, "PASSWORD_HASHERS", global_settings.PASSWORD_HASHERS
    )
)
class ProductionPasswordAuthenticationTests(TestCase):
    def test_production_password_hash_authenticates_and_rejects_wrong_password(self):
        password = "synthetic-production-hash-742!"
        user = get_user_model().objects.create_user(
            username="production-hash-user",
            email="production-hash@example.test",
            password=password,
        )
        EmailAddress.objects.create(
            user=user, email=user.email, verified=True, primary=True
        )
        self.assertEqual(
            identify_hasher(user.password).algorithm, get_hasher().algorithm
        )
        self.assertNotEqual(identify_hasher(user.password).algorithm, "md5")

        response = self.client.post(
            reverse("account_login"),
            {"login": user.email, "password": "incorrect-password"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

        response = self.client.post(
            reverse("account_login"), {"login": user.email, "password": password}
        )
        self.assertRedirects(response, reverse("cases:case_list"))
        self.assertEqual(self.client.session["_auth_user_id"], str(user.pk))
