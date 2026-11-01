import json
from decimal import Decimal

from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from cases.models import Case, CaseMembership
from evidence.models import (
    LocationEvidence,
    PersonLocationEvidence,
    VehicleLocationEvidence,
)
from people.models import Person
from vehicles.models import Vehicle


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
        cls.url = reverse("cases:case_map", args=(cls.case.pk,))


@override_settings(PANDECT_MAP_TILE_URL="", PANDECT_MAP_TILE_ORIGIN="")
class InvestigationMapTests(MapCaseFixtures, TestCase):
    def test_current_case_roles_and_private_headers(self):
        for role in ("owner", "editor", "viewer"):
            with self.subTest(role=role):
                self.client.force_login(getattr(self, role))
                response = self.client.get(self.url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(response.context["points"]), 3)
                self.assertContains(response, "Harbour observation")
                self.assertNotContains(response, "Secret other location")
                self.assertNotContains(response, "Archived observation")
                self.assertContains(
                    response, "Add location", count=1 if role != "viewer" else 0
                )
                self.assertIn("no-store", response["Cache-Control"])
                self.assertEqual(response["Referrer-Policy"], "same-origin")
                self.assertIn(
                    "img-src 'self' data:;", response["Content-Security-Policy"]
                )
                self.assertNotContains(response, "tile.openstreetmap.org")
                self.assertEqual(self.client.post(self.url).status_code, 405)
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.outsider.is_superuser = True
        self.outsider.save()
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.client.force_login(self.viewer)
        CaseMembership.objects.filter(case=self.case, user=self.viewer).delete()
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_authentication_and_verification(self):
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.client.force_login(self.owner)
        EmailAddress.objects.filter(user=self.owner).update(verified=False)
        self.assertRedirects(
            self.client.get(self.url),
            reverse("account_email"),
            fetch_redirect_response=False,
        )

    def test_filters_source_links_and_unmapped_records(self):
        self.client.force_login(self.owner)
        response = self.client.get(self.url, {"kind": "vehicle-location"})
        self.assertEqual(response.context["page_obj"].paginator.count, 1)
        self.assertContains(
            response,
            reverse(
                "evidence:detail",
                args=(self.case.pk, "vehicle-location", self.vehicle_location.pk),
            ),
        )
        response = self.client.get(self.url, {"q": "Synthetic road"})
        self.assertEqual(response.context["points"], [])
        self.assertEqual(response.context["unmapped_count"], 1)
        self.assertContains(response, "Address only")
        response = self.client.get(self.url, {"q": "Synthetic source"})
        self.assertEqual(response.context["page_obj"].paginator.count, 1)
        response = self.client.get(self.url, {"archive": "archived"})
        self.assertEqual(response.context["page_obj"].paginator.count, 1)
        self.assertContains(response, "Archived observation")
        self.assertEqual(
            self.client.get(self.url, {"archive": "all"})
            .context["page_obj"]
            .paginator.count,
            5,
        )
        response = self.client.get(
            self.url, {"archive": "invalid", "kind": "image", "q": "absent"}
        )
        self.assertEqual(response.context["archive"], "active")
        self.assertEqual(response.context["kind"], "")
        self.assertContains(response, "No matching locations")

    def test_inconsistent_cross_case_associations_are_excluded(self):
        person = Person.objects.create(case=self.other, first_name="Private person")
        vehicle = Vehicle.objects.create(case=self.other, make="Private vehicle")
        PersonLocationEvidence.objects.filter(pk=self.person_location.pk).update(
            person=person
        )
        VehicleLocationEvidence.objects.filter(pk=self.vehicle_location.pk).update(
            vehicle=vehicle
        )
        self.client.force_login(self.owner)
        response = self.client.get(self.url)
        self.assertEqual(len(response.context["points"]), 1)
        self.assertNotContains(response, "Person observation")
        self.assertNotContains(response, "Vehicle observation")

    def test_zero_polar_and_untrusted_titles_preserve_originals(self):
        title = '</script><img src=x onerror="alert(1)">'
        origin = LocationEvidence.objects.create(
            case=self.case, uploaded_by=self.owner, title=title, latitude=0, longitude=0
        )
        pole = LocationEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Polar source",
            latitude=90,
            longitude=180,
        )
        original_checksum = pole.sha256
        self.client.force_login(self.owner)
        response = self.client.get(self.url)
        point = next(
            item for item in response.context["points"] if item["title"] == title
        )
        self.assertEqual((point["latitude"], point["longitude"]), (0.0, 0.0))
        self.assertNotContains(response, title)
        data = (
            response.content.decode()
            .split('<script id="case-map-points" type="application/json">')[1]
            .split("</script>")[0]
        )
        self.assertIn(title, [item["title"] for item in json.loads(data)])
        self.assertContains(response, "Outside map display range")
        pole.refresh_from_db()
        origin.refresh_from_db()
        self.assertEqual(pole.latitude, Decimal("90"))
        self.assertEqual(origin.latitude, Decimal("0"))
        self.assertEqual(pole.sha256, original_checksum)

    def test_bounded_pagination_across_location_types(self):
        for index in range(102):
            LocationEvidence.objects.create(
                case=self.case,
                uploaded_by=self.owner,
                title=f"Paged source {index}",
                latitude=0,
                longitude=0,
            )
        self.client.force_login(self.owner)
        response = self.client.get(self.url, {"q": "Paged source", "archive": "all"})
        self.assertEqual(len(response.context["points"]), 100)
        self.assertEqual(response.context["page_obj"].paginator.count, 102)
        self.assertContains(response, "q=Paged+source&amp;archive=all&amp;page=2")
        response = self.client.get(self.url, {"q": "Paged source", "page": 2})
        self.assertEqual(len(response.context["points"]), 2)
        self.assertEqual(response.context["points"][0]["number"], 101)

    @override_settings(
        PANDECT_MAP_TILE_URL="https://maps.example.test/{z}/{x}/{y}.png",
        PANDECT_MAP_TILE_ORIGIN="https://maps.example.test",
        PANDECT_MAP_TILE_ATTRIBUTION="Synthetic provider",
    )
    def test_tile_configuration_is_page_scoped(self):
        self.client.force_login(self.owner)
        response = self.client.get(self.url)
        self.assertContains(response, "https://maps.example.test/{z}/{x}/{y}.png")
        self.assertContains(response, "Synthetic provider")
        self.assertIn("https://maps.example.test", response["Content-Security-Policy"])
        response = self.client.get(reverse("cases:case_detail", args=(self.case.pk,)))
        self.assertNotIn(
            "https://maps.example.test", response["Content-Security-Policy"]
        )
        self.assertNotContains(response, "leaflet.js")

    @override_settings(
        PANDECT_MAP_TILE_URL="",
        PANDECT_MAP_TILE_ORIGIN="",
    )
    def test_no_basemap_keeps_source_markers_without_public_tiles(self):
        self.client.force_login(self.owner)
        response = self.client.get(self.url)
        self.assertContains(response, 'data-tile-url=""')
        self.assertEqual(len(response.context["points"]), 3)
        self.assertNotContains(response, "data-map-load")
        self.assertNotIn("https://", response["Content-Security-Policy"])

    @override_settings(
        PANDECT_MAP_TILE_URL="https://maps.example.test/{z}/{x}/{y}.png",
        PANDECT_MAP_TILE_ORIGIN="https://maps.example.test",
    )
    def test_private_tiles_use_the_same_automatic_map(self):
        self.client.force_login(self.owner)
        response = self.client.get(self.url)
        self.assertContains(
            response, 'data-tile-url="https://maps.example.test/{z}/{x}/{y}.png"'
        )
        self.assertNotContains(response, "data-map-load")


class PersonPOIMapTests(MapCaseFixtures, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from people.models import PointOfInterest

        cls.poi = PointOfInterest.objects.create(
            person=cls.person,
            created_by=cls.owner,
            kind="custom",
            custom_label='</script><img src=x onerror="alert(1)">',
            latitude=0,
            longitude=0,
            address="POI address",
            source="POI citation",
        )
        other_person = Person.objects.create(case=cls.other, first_name="Secret person")
        PointOfInterest.objects.create(
            person=other_person,
            created_by=cls.outsider,
            kind="work",
            latitude=1,
            longitude=1,
        )

    def test_points_lists_json_and_person_links_are_scoped_and_escaped(self):
        for user in (self.owner, self.editor, self.viewer):
            self.client.force_login(user)
            response = self.client.get(self.url)
            points = response.context["points"]
            point = next(point for point in points if point.get("poi"))
            self.assertEqual(point["number"], "P1")
            self.assertEqual((point["latitude"], point["longitude"]), (0, 0))
            self.assertEqual(
                point["url"],
                reverse("people:poi_detail", args=(self.case.pk, self.poi.pk)),
            )
            self.assertContains(response, "&lt;/script&gt;")
            self.assertNotContains(response, self.poi.custom_label)
            self.assertNotContains(response, "Secret person")
            data = self.client.get(self.url, HTTP_ACCEPT="application/json").json()
            self.assertEqual(data["points"], points)
            self.assertEqual(data["poi_key"], response.context["poi_key"])
            self.assertEqual(
                "data-poi-create-url=" in response.content.decode(), user != self.viewer
            )

    def test_shared_search_and_independent_evidence_filters(self):
        self.client.force_login(self.viewer)
        for query in ("Synthetic person", "POI address", "POI citation"):
            response = self.client.get(self.url, {"q": query})
            self.assertEqual(response.context["poi_page"].paginator.count, 1)
        response = self.client.get(
            self.url, {"kind": "vehicle-location", "archive": "archived"}
        )
        self.assertEqual(response.context["poi_page"].paginator.count, 1)
        self.assertEqual(response.context["page_obj"].paginator.count, 0)
        response = self.client.get(self.url, {"q": "Secret person"})
        self.assertEqual(response.context["poi_page"].paginator.count, 0)

    def test_bounded_pagination_and_polar_coordinates(self):
        from people.models import PointOfInterest

        PointOfInterest.objects.bulk_create(
            [
                PointOfInterest(
                    person=self.person,
                    created_by=self.owner,
                    kind="work",
                    latitude=90,
                    longitude=180,
                )
                for _ in range(102)
            ]
        )
        self.client.force_login(self.owner)
        response = self.client.get(self.url, {"q": "work"})
        self.assertEqual(len(response.context["poi_page"]), 100)
        self.assertEqual(response.context["poi_page"].paginator.count, 102)
        self.assertFalse(any(point.get("poi") for point in response.context["points"]))
        self.assertContains(response, "Outside map display range")
        self.assertContains(response, "q=work&amp;poi_page=2")
        second = self.client.get(self.url, {"q": "work", "poi_page": 2})
        self.assertEqual(len(second.context["poi_page"]), 2)
        self.assertEqual(second.context["poi_page"][0].number, "P101")

    def test_updates_and_person_changes_invalidate_async_snapshot(self):
        self.client.force_login(self.owner)

        def key():
            return self.client.get(self.url, HTTP_ACCEPT="application/json").json()[
                "poi_key"
            ]

        old = key()
        self.poi.notes = "Updated"
        self.poi.save()
        self.assertNotEqual(key(), old)
        old = key()
        self.person.first_name = "Changed person name"
        self.person.save()
        self.assertNotEqual(key(), old)
