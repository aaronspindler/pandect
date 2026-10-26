from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from cases.models import Case
from vehicles.models import Vehicle


class VehicleModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = get_user_model().objects.create_user(username="vehicle-owner")
        cls.case = Case.objects.create(owner=cls.owner)
        cls.other_case = Case.objects.create(owner=cls.owner)

    def test_case_is_required_by_validation_and_database(self):
        vehicle = Vehicle(make="Example Motors")
        with self.assertRaises(ValidationError) as error:
            vehicle.full_clean()
        self.assertIn("case", error.exception.message_dict)
        with self.assertRaises(IntegrityError), transaction.atomic():
            vehicle.save()

    def test_partial_identifiers_preserve_source_formatting(self):
        for field, value in (
            ("make", "Example Motors"),
            ("model", "Hatchback"),
            ("license_plate", "SYN 123-AB"),
            ("vin", "Partial historical identifier"),
        ):
            with self.subTest(field=field):
                vehicle = Vehicle(case=self.case, **{field: value})
                vehicle.full_clean()
                vehicle.save()
                vehicle.refresh_from_db()
                self.assertEqual(getattr(vehicle, field), value)
                self.assertIsNone(vehicle.year)
                self.assertEqual(vehicle.notes, "")

    def test_year_color_and_notes_do_not_replace_an_identifier(self):
        for value in ("", " ", "\t\r\n"):
            with self.subTest(value=value):
                vehicle = Vehicle(
                    case=self.case,
                    make=value,
                    model=value,
                    license_plate=value,
                    vin=value,
                    year=2020,
                    color="Blue",
                    notes="Unconfirmed vehicle observation.",
                )
                with self.assertRaisesMessage(ValidationError, "Enter at least"):
                    vehicle.full_clean()
                with self.assertRaises(IntegrityError), transaction.atomic():
                    vehicle.save()

    def test_identity_constraint_covers_bulk_create_and_update(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Vehicle.objects.bulk_create([Vehicle(case=self.case, vin=" \t\n")])
        vehicle = Vehicle.objects.create(case=self.case, make="Example Motors")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Vehicle.objects.filter(pk=vehicle.pk).update(make="")
        vehicle.refresh_from_db()
        self.assertEqual(vehicle.make, "Example Motors")

    def test_year_bounds_are_validated_and_database_enforced(self):
        for year in (0, -1, 10000):
            with self.subTest(year=year):
                vehicle = Vehicle(case=self.case, make="Example Motors", year=year)
                with self.assertRaises(ValidationError) as error:
                    vehicle.full_clean()
                self.assertIn("year", error.exception.message_dict)
                with self.assertRaises(IntegrityError), transaction.atomic():
                    vehicle.save()
        for year in (None, 1, 2020, 9999):
            vehicle = Vehicle(case=self.case, make="Example Motors", year=year)
            vehicle.full_clean()
            vehicle.save()

    def test_field_lengths_are_validated(self):
        for field, length in (
            ("make", 150),
            ("model", 150),
            ("color", 100),
            ("license_plate", 64),
            ("vin", 64),
        ):
            with self.subTest(field=field):
                vehicle = Vehicle(
                    case=self.case,
                    **{"make": "Example Motors", field: "x" * (length + 1)},
                )
                with self.assertRaises(ValidationError) as error:
                    vehicle.full_clean()
                self.assertIn(field, error.exception.message_dict)

    def test_duplicate_identifiers_are_independent_within_and_across_cases(self):
        values = {"license_plate": "SYN-001", "vin": "SYNTHETIC-VIN"}
        first = Vehicle.objects.create(case=self.case, **values)
        second = Vehicle.objects.create(case=self.case, **values)
        third = Vehicle(case=self.other_case, **values)
        third.full_clean()
        third.save()
        self.assertCountEqual(self.case.vehicles.all(), [first, second])
        self.assertEqual(self.other_case.vehicles.get(), third)
        first.notes = "Unconfirmed source.\nConflicting observations remain unresolved."
        first.save()
        first.refresh_from_db()
        second.refresh_from_db()
        third.refresh_from_db()
        self.assertIn("\nConflicting", first.notes)
        self.assertEqual(second.notes, "")
        self.assertEqual(third.notes, "")

    def test_timestamps_preserve_creation_and_record_later_saves(self):
        before = timezone.now()
        vehicle = Vehicle.objects.create(case=self.case, make="Example Motors")
        self.assertLessEqual(before, vehicle.created_at)
        self.assertLessEqual(vehicle.created_at, vehicle.updated_at)
        created_at = vehicle.created_at
        later = vehicle.updated_at + timedelta(days=1)
        with patch("django.utils.timezone.now", return_value=later):
            vehicle.notes = "Updated"
            vehicle.save()
        vehicle.refresh_from_db()
        self.assertEqual(vehicle.created_at, created_at)
        self.assertEqual(vehicle.updated_at, later)

    def test_display_name_uses_year_make_model_then_plate_then_vin(self):
        for values, expected in (
            (
                {"year": 2020, "make": "Example Motors", "model": "Hatchback"},
                "2020 Example Motors Hatchback",
            ),
            ({"make": " Example Motors "}, "Example Motors"),
            ({"model": " Hatchback "}, "Hatchback"),
            ({"license_plate": "SYN-001", "vin": "SYNTHETIC-VIN"}, "SYN-001"),
            ({"vin": "SYNTHETIC-VIN"}, "SYNTHETIC-VIN"),
        ):
            with self.subTest(values=values):
                self.assertEqual(str(Vehicle(case=self.case, **values)), expected)

    def test_case_deletion_removes_only_its_vehicles_and_preserves_users(self):
        removed = Vehicle.objects.create(case=self.case, make="Example Motors")
        retained = Vehicle.objects.create(case=self.other_case, license_plate="SYN-002")
        self.case.delete()
        self.assertFalse(Vehicle.objects.filter(pk=removed.pk).exists())
        self.assertEqual(Vehicle.objects.get(), retained)
        self.assertTrue(get_user_model().objects.filter(pk=self.owner.pk).exists())
