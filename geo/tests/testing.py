"""Synthetic map and location-import fixtures."""

import csv
import io

from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from cases.models import Case, CaseMembership
from geo.models import (
    LocationEvidence,
    PersonLocationEvidence,
    VehicleLocationEvidence,
)
from geo.vehicle_imports import HEADERS, iter_csv
from people.models import Person
from vehicle.models import Vehicle


def csv_content(*rows, **changes):
    """Synthetic fixtures only; never copy the user's asset/location identifiers."""
    row = {
        "Asset": "Synthetic van",
        "Serial Number": "000000000000123",
        "Address": "  10 Example Street, Example City, Ontario, CAN  ",
        "Speed": "0 km/h",
        "Heading": "NW",
        "Trigger": "Ignition On: parked time",
        "Time Summary": "51m",
        "Created Time": "2026-09-16, 2:57:22 PM",
        "Received Time": "2026-09-16, 2:57:25 PM",
    } | changes
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, HEADERS)
    writer.writeheader()
    writer.writerows(rows or [row])
    return output.getvalue().encode("utf-8")


def parse_csv(content, timezone_name):
    return list(iter_csv(io.BytesIO(content), timezone_name))


class MapCaseFixtures:
    @classmethod
    def setUpTestData(cls):
        for name in ("owner", "editor", "viewer", "outsider"):
            user = get_user_model().objects.create_user(
                username=name, email=f"{name}@example.test"
            )
            EmailAddress.objects.create(
                user=user, email=user.email, primary=True, verified=True
            )
            setattr(cls, name, user)
        cls.case = Case.objects.create(owner=cls.owner, name="Synthetic harbour")
        cls.other = Case.objects.create(owner=cls.outsider, name="Unrelated case")
        for role in ("viewer", "editor"):
            CaseMembership.objects.create(
                case=cls.case, user=getattr(cls, role), role=role
            )
        cls.person = Person.objects.create(case=cls.case, first_name="Synthetic person")
        cls.vehicle = Vehicle.objects.create(case=cls.case, make="Synthetic vehicle")
        common = {
            "case": cls.case,
            "uploaded_by": cls.owner,
            "latitude": "43.64",
            "longitude": "-79.38",
        }
        cls.location = LocationEvidence.objects.create(
            **common,
            title="Harbour observation",
            source="Synthetic source",
            observed_at=timezone.now(),
        )
        cls.person_location = PersonLocationEvidence.objects.create(
            **common, person=cls.person, title="Person observation"
        )
        cls.vehicle_location = VehicleLocationEvidence.objects.create(
            **common, vehicle=cls.vehicle, title="Vehicle observation"
        )
        cls.address = LocationEvidence.objects.create(
            case=cls.case,
            uploaded_by=cls.owner,
            title="Address observation",
            address="Synthetic road",
        )
        cls.archived = LocationEvidence.objects.create(
            **common,
            title="Archived observation",
            is_archived=True,
            archive_changed_by=cls.owner,
            archive_changed_at=timezone.now(),
        )
        LocationEvidence.objects.create(
            case=cls.other,
            uploaded_by=cls.outsider,
            title="Secret other location",
            latitude=0,
            longitude=0,
        )
        cls.url = reverse("geo:case_map", args=(cls.case.pk,))


LOCAL_GEOCODER = {
    "PANDECT_GEOCODER_URL": "http://geocoder.internal:8080",
    "PANDECT_GEOCODER_LOCAL": True,
    "PANDECT_GEOCODER_NAME": "Synthetic local reference",
    "PANDECT_GEOCODER_VERSION": "test-dataset-1",
    "PANDECT_GEOCODER_ATTRIBUTION": "Synthetic local attribution",
    "PANDECT_GEOCODER_USER_AGENT": "Pandect synthetic tests",
}
