from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase
from django.utils import timezone

from cases.models import Case, CaseMembership


class CaseModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = get_user_model().objects.create_user(username="owner")
        cls.viewer = get_user_model().objects.create_user(username="viewer")
        cls.editor = get_user_model().objects.create_user(username="editor")
        cls.case = Case.objects.create(owner=cls.owner)

    def test_owner_is_required_by_validation_and_database(self):
        with self.assertRaises(ValidationError) as error:
            Case().full_clean()
        self.assertIn("owner", error.exception.message_dict)
        with transaction.atomic(), self.assertRaises(IntegrityError):
            Case.objects.create()

    def test_creation_timestamp_is_set_once(self):
        before = timezone.now()
        case = Case.objects.create(owner=self.owner)
        after = timezone.now()
        self.assertLessEqual(before, case.created_at)
        self.assertLessEqual(case.created_at, after)
        created_at = case.created_at
        with patch("django.utils.timezone.now", return_value=after + timedelta(days=1)):
            case.save()
        case.refresh_from_db()
        self.assertEqual(case.created_at, created_at)
        self.assertEqual(str(case), f"Case #{case.pk}")

    def test_new_case_can_have_no_collaborators(self):
        self.case.full_clean()
        self.assertFalse(self.case.additional_users.exists())
        self.assertFalse(self.case.memberships.exists())

    def test_multiple_collaborators_and_both_roles(self):
        viewer = CaseMembership.objects.create(case=self.case, user=self.viewer)
        editor = CaseMembership.objects.create(
            case=self.case, user=self.editor, role=CaseMembership.Role.EDITOR
        )
        viewer.full_clean()
        editor.full_clean()
        self.assertEqual(viewer.role, CaseMembership.Role.VIEWER)
        self.assertCountEqual(
            self.case.additional_users.all(), [self.viewer, self.editor]
        )
        self.assertEqual(self.viewer.shared_cases.get(), self.case)
        self.assertEqual(self.owner.owned_cases.get(), self.case)

    def test_membership_case_and_user_are_required(self):
        for fields in ({"case": self.case}, {"user": self.viewer}):
            with self.subTest(fields=fields):
                with self.assertRaises(ValidationError):
                    CaseMembership(**fields).full_clean()
                with transaction.atomic(), self.assertRaises(IntegrityError):
                    CaseMembership.objects.create(**fields)

    def test_duplicate_memberships_are_rejected(self):
        CaseMembership.objects.create(case=self.case, user=self.viewer)
        duplicate = CaseMembership(case=self.case, user=self.viewer, role="editor")
        with self.assertRaises(ValidationError):
            duplicate.full_clean()
        with transaction.atomic(), self.assertRaises(IntegrityError):
            duplicate.save()
        other_case = Case.objects.create(owner=self.owner)
        CaseMembership.objects.create(case=other_case, user=self.viewer)

    def test_invalid_roles_are_rejected_by_validation_and_database(self):
        for role in ("owner", "", "VIEWER"):
            with self.subTest(role=role):
                membership = CaseMembership(case=self.case, user=self.viewer, role=role)
                with self.assertRaises(ValidationError):
                    membership.full_clean()
                with transaction.atomic(), self.assertRaises(IntegrityError):
                    membership.save()

    def test_owner_cannot_validate_as_additional_user(self):
        membership = CaseMembership(case=self.case, user=self.owner)
        with self.assertRaises(ValidationError) as error:
            membership.full_clean()
        self.assertIn("user", error.exception.message_dict)
        membership = CaseMembership.objects.create(case=self.case, user=self.viewer)
        membership.user = self.owner
        with self.assertRaises(ValidationError):
            membership.full_clean()

    def test_deleting_owner_is_protected(self):
        with self.assertRaises(ProtectedError):
            self.owner.delete()
        with self.assertRaises(ProtectedError):
            get_user_model().objects.filter(pk=self.owner.pk).delete()
        self.assertTrue(Case.objects.filter(pk=self.case.pk).exists())

    def test_deleting_collaborator_removes_membership_and_preserves_case(self):
        CaseMembership.objects.create(case=self.case, user=self.viewer)
        self.viewer.delete()
        self.assertFalse(self.case.memberships.exists())
        self.case.refresh_from_db()
        self.assertEqual(self.case.owner, self.owner)

    def test_deleting_case_removes_memberships_and_preserves_users(self):
        CaseMembership.objects.create(case=self.case, user=self.viewer)
        self.case.delete()
        self.assertFalse(CaseMembership.objects.exists())
        self.assertEqual(get_user_model().objects.count(), 3)

    def test_membership_changes_never_change_ownership(self):
        membership = CaseMembership.objects.create(case=self.case, user=self.viewer)
        membership.role = CaseMembership.Role.EDITOR
        membership.save()
        membership.delete()
        self.case.refresh_from_db()
        self.assertEqual(self.case.owner, self.owner)
