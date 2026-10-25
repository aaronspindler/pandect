from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from cases.models import Case
from people.models import Person


class PersonModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = get_user_model().objects.create_user(username="owner")
        cls.case = Case.objects.create(owner=cls.owner)
        cls.other_case = Case.objects.create(owner=cls.owner)

    def test_case_is_required_by_validation_and_database(self):
        person = Person(first_name="Alex")
        with self.assertRaises(ValidationError) as error:
            person.full_clean()
        self.assertIn("case", error.exception.message_dict)
        with self.assertRaises(IntegrityError), transaction.atomic():
            person.save()

    def test_each_identifier_can_independently_identify_a_person(self):
        for field, value in (
            ("first_name", "Alex"),
            ("last_name", "Example"),
            ("phone_number", "+1 (202) 555-0100 ext. 42"),
            ("email", "alex@example.com"),
        ):
            with self.subTest(field=field):
                person = Person(case=self.case, **{field: value})
                person.full_clean()
                person.save()
                person.refresh_from_db()
                self.assertEqual(getattr(person, field), value)
                self.assertEqual(person.address, "")
                self.assertEqual(person.notes, "")

    def test_empty_and_whitespace_only_identities_are_rejected(self):
        for value in ("", " ", "\t\r\n"):
            with self.subTest(value=value):
                person = Person(
                    case=self.case,
                    first_name=value,
                    last_name=value,
                    phone_number=value,
                    address="123 Synthetic Street",
                    notes="An address and notes alone do not identify a person.",
                )
                with self.assertRaisesMessage(
                    ValidationError,
                    "Enter at least a first name, last name, phone number, or email.",
                ):
                    person.full_clean()
                with self.assertRaises(IntegrityError), transaction.atomic():
                    person.save()

    def test_database_constraint_also_rejects_whitespace_email_and_bulk_updates(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Person.objects.bulk_create([Person(case=self.case, email=" \t\n")])
        person = Person.objects.create(case=self.case, first_name="Alex")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Person.objects.filter(pk=person.pk).update(first_name="")
        person.refresh_from_db()
        self.assertEqual(person.first_name, "Alex")

    def test_email_and_field_lengths_are_validated(self):
        for field, value in (
            ("email", "invalid-email"),
            ("email", f"{'a' * 243}@example.com"),
            ("first_name", "a" * 151),
            ("last_name", "a" * 151),
            ("phone_number", "1" * 65),
        ):
            with self.subTest(field=field):
                person = Person(case=self.case, **{"first_name": "Alex", field: value})
                with self.assertRaises(ValidationError) as error:
                    person.full_clean()
                self.assertIn(field, error.exception.message_dict)

    def test_people_and_duplicate_contacts_remain_independent_within_each_case(self):
        values = {"first_name": "Alex", "email": "alex@example.com"}
        first = Person.objects.create(case=self.case, **values)
        second = Person.objects.create(case=self.case, **values)
        third = Person(case=self.other_case, **values)
        third.full_clean()
        third.save()
        self.assertCountEqual(self.case.people.all(), [first, second])
        self.assertEqual(self.other_case.people.get(), third)
        first.notes = "A case-specific assertion, not verified identity."
        first.save()
        second.refresh_from_db()
        third.refresh_from_db()
        self.assertEqual(second.notes, "")
        self.assertEqual(third.notes, "")

    def test_multiline_address_and_notes_are_preserved(self):
        person = Person.objects.create(
            case=self.case,
            first_name="Alex",
            address="123 Synthetic Street\nUnit 4\nOttawa, Canada",
            notes="Unconfirmed contact.\nReview the source before drawing conclusions.",
        )
        person.refresh_from_db()
        self.assertIn("\nUnit 4\n", person.address)
        self.assertIn("Unconfirmed contact.\nReview", person.notes)

    def test_timestamps_record_creation_and_later_saves(self):
        before = timezone.now()
        person = Person.objects.create(case=self.case, first_name="Alex")
        self.assertLessEqual(before, person.created_at)
        self.assertLessEqual(person.created_at, person.updated_at)
        created_at = person.created_at
        later = person.updated_at + timedelta(days=1)
        with patch("django.utils.timezone.now", return_value=later):
            person.notes = "Updated"
            person.save()
        person.refresh_from_db()
        self.assertEqual(person.created_at, created_at)
        self.assertEqual(person.updated_at, later)

    def test_display_name_falls_back_to_email_then_phone(self):
        for values, expected in (
            ({"first_name": "Alex", "last_name": "Example"}, "Alex Example"),
            ({"first_name": " Alex "}, "Alex"),
            ({"last_name": " Example "}, "Example"),
            ({"email": "alex@example.com", "phone_number": "555"}, "alex@example.com"),
            ({"phone_number": "+1 202-555-0100"}, "+1 202-555-0100"),
        ):
            with self.subTest(values=values):
                self.assertEqual(str(Person(case=self.case, **values)), expected)

    def test_case_deletion_removes_only_its_people_and_preserves_users(self):
        removed = Person.objects.create(case=self.case, first_name="Alex")
        retained = Person.objects.create(case=self.other_case, first_name="Morgan")
        self.case.delete()
        self.assertFalse(Person.objects.filter(pk=removed.pk).exists())
        self.assertEqual(Person.objects.get(), retained)
        self.assertTrue(get_user_model().objects.filter(pk=self.owner.pk).exists())
