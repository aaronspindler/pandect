from decimal import Decimal
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import Client, TestCase
from django.urls import reverse
from django.utils.html import escape

from cases.models import CaseMembership
from core.case_search.tests.fixtures import indexed
from geo.models import PointOfInterest
from geo.poi_forms import PointOfInterestForm
from geo.testing import MapCaseFixtures
from people.models import Person


class PersonPointOfInterestTests(MapCaseFixtures, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.other_person = Person.objects.create(
            case=cls.other, first_name="Private person"
        )
        cls.poi = PointOfInterest.objects.create(
            person=cls.person,
            created_by=cls.owner,
            kind="home",
            latitude=0,
            longitude=0,
            notes="Unconfirmed residence",
        )
        cls.other_poi = PointOfInterest.objects.create(
            person=cls.other_person,
            created_by=cls.outsider,
            kind="custom",
            custom_label="Secret place",
            latitude=1,
            longitude=1,
        )

    def poi_url(self, action, poi=None, case=None):
        args = [(case or self.case).pk]
        if action != "create":
            args.append((poi or self.poi).pk)
        return reverse(f"geo:poi_{action}", args=args)

    def data(self, **changes):
        return {
            "person": self.person.pk,
            "kind": "home",
            "custom_label": "",
            "latitude": "43.64",
            "longitude": "-79.38",
            "address": "Synthetic road",
            "source": "Synthetic citation",
            "notes": "Keep uncertainty",
            **changes,
        }

    def test_owner_and_editor_add_each_kind_with_attribution_and_private_details(self):
        for user in (self.owner, self.editor):
            self.client.force_login(user)
            for kind in ("home", "work", "custom"):
                with self.subTest(user=user, kind=kind):
                    response = self.client.post(
                        self.poi_url("create"),
                        self.data(
                            kind=kind,
                            custom_label="Gym",
                            case=self.other.pk,
                            created_by=self.outsider.pk,
                        ),
                    )
                    self.assertEqual(response.status_code, 302)
                    poi = PointOfInterest.objects.get(
                        pk=response.url.strip("/").split("/")[-1]
                    )
                    self.assertEqual(poi.person.case_id, self.case.pk)
                    self.assertEqual(poi.created_by, user)
                    self.assertEqual(poi.notes, "")
                    self.assertEqual(
                        poi.label, "Gym" if kind == "custom" else kind.title()
                    )
                    detail = self.client.get(response.url)
                    self.assertContains(detail, "Investigator annotation")
                    self.assertContains(detail, "Synthetic citation")
                    self.assertIn("no-store", detail["Cache-Control"])

    def test_invalid_labels_coordinates_and_person_preserve_draft(self):
        self.client.force_login(self.editor)
        for values, field in (
            ({"kind": "custom", "custom_label": "   "}, "custom_label"),
            ({"kind": "invalid"}, "kind"),
            ({"latitude": ""}, "latitude"),
            ({"latitude": "NaN"}, "latitude"),
            ({"latitude": "90.000001"}, "latitude"),
            ({"longitude": "-180.000001"}, "longitude"),
            ({"latitude": "1.1234567"}, "latitude"),
            ({"person": self.other_person.pk}, "person"),
            ({"person": uuid4()}, "person"),
        ):
            with self.subTest(values=values):
                response = self.client.post(self.poi_url("create"), self.data(**values))
                self.assertEqual(response.status_code, 400)
                self.assertIn(field, response.context["form"].errors)
                self.assertContains(response, "Synthetic citation", status_code=400)
        self.assertEqual(PointOfInterest.objects.count(), 2)

    def test_prefills_are_case_scoped_and_empty_case_has_next_action(self):
        self.client.force_login(self.owner)
        response = self.client.get(
            self.poi_url("create"),
            {
                "person": self.person.pk,
                "latitude": "0",
                "longitude": "-180",
            },
        )
        self.assertEqual(response.context["form"]["person"].value(), self.person.pk)
        self.assertEqual(response.context["form"]["latitude"].value(), "0")
        self.assertNotContains(response, "Private person")
        for value in (self.other_person.pk, "bad-uuid"):
            self.assertEqual(
                self.client.get(self.poi_url("create"), {"person": value}).status_code,
                404,
            )
        self.client.force_login(self.outsider)
        empty = self.other.__class__.objects.create(owner=self.outsider)
        self.assertContains(
            self.client.get(self.poi_url("create", case=empty)), "Add a person first"
        )

    def test_all_roles_read_map_person_and_detail_but_viewer_cannot_mutate(self):
        for user in (self.owner, self.editor, self.viewer):
            self.client.force_login(user)
            for url in (
                self.poi_url("detail"),
                reverse("people:detail", args=(self.case.pk, self.person.pk)),
                self.url_for_map(),
            ):
                response = self.client.get(url)
                self.assertContains(response, "Home")
                self.assertNotContains(response, "Secret place")
            self.assertEqual(self.client.post(self.poi_url("detail")).status_code, 405)
        self.assertNotContains(response, "Add POI")
        for action in ("create", "edit", "delete"):
            self.assertEqual(self.client.get(self.poi_url(action)).status_code, 403)
            self.assertEqual(
                self.client.post(
                    self.poi_url(action), self.data(confirm="on")
                ).status_code,
                403,
            )

    def url_for_map(self):
        return reverse("geo:case_map", args=(self.case.pk,))

    def test_poi_detail_returns_to_its_map_or_person_list_context(self):
        self.enterContext(indexed(self.case, [("record", "poi", self.poi.pk)]))
        self.client.force_login(self.viewer)
        filters = {
            "q": "Home",
            "page": "2",
            "poi_page": "3",
            "vehicle_id": str(self.vehicle.pk),
            "day": "2026-11-01",
            "gap_page": "2",
        }
        map_page = self.client.get(
            self.url_for_map(),
            filters
            | {"state": "all", "next": "https://example.test/", "from_person": "1"},
        )
        point_url = map_page.context["points"][0]["url"]
        self.assertContains(map_page, f'href="{escape(point_url)}"')
        self.assertEqual(
            parse_qs(urlsplit(point_url).query),
            {"from_map": ["1"]} | {key: [value] for key, value in filters.items()},
        )
        map_back_url = reverse(
            "geo:case_map",
            args=(self.case.pk,),
            query=filters,
            fragment="map-heading",
        )
        self.assertContains(
            self.client.get(point_url),
            f'href="{escape(map_back_url)}">Back to map',
        )

        person_url = reverse("people:detail", args=(self.case.pk, self.person.pk))
        person_filters = {"q": "Synthetic", "page": "2"}
        person_page = self.client.get(person_url, person_filters)
        detail_url = reverse(
            "geo:poi_detail",
            args=(self.case.pk, self.poi.pk),
            query={"from_person": self.person.pk} | person_filters,
        )
        self.assertContains(person_page, f'href="{escape(detail_url)}"')
        back_url = reverse(
            "people:detail",
            args=(self.case.pk, self.person.pk),
            query=person_filters,
            fragment="poi-list",
        )
        self.assertContains(
            self.client.get(detail_url + f"&person={self.other_person.pk}"),
            f'href="{escape(back_url)}">Back to person',
        )
        legacy_detail = reverse(
            "geo:poi_detail",
            args=(self.case.pk, self.poi.pk),
            query={"from_person": "1"} | person_filters,
        )
        self.assertContains(
            self.client.get(legacy_detail),
            f'href="{escape(back_url)}">Back to person',
        )

    def test_filtered_map_round_trip_through_invalid_add_save_and_delete(self):
        self.client.force_login(self.editor)
        filters = {"q": "Synthetic", "page": "2", "poi_page": "3"}
        origin = {"from_map": "1"} | filters
        back = reverse(
            "geo:case_map", args=(self.case.pk,), query=filters, fragment="map-heading"
        )
        map_page = self.client.get(self.url_for_map(), filters | {"state": "all"})
        create_url = reverse("geo:poi_create", args=(self.case.pk,), query=origin)
        self.assertContains(map_page, f'href="{escape(create_url)}">Add POI')
        self.assertContains(map_page, f'data-poi-create-url="{escape(create_url)}"')
        create_url += "&state=archived&latitude=43.64&longitude=-79.38"
        response = self.client.post(create_url, self.data(latitude=""))
        self.assertContains(response, f'href="{escape(back)}">Cancel', status_code=400)
        response = self.client.post(create_url, self.data())
        created = PointOfInterest.objects.exclude(
            pk__in=[self.poi.pk, self.other_poi.pk]
        ).get()
        detail_url = reverse(
            "geo:poi_detail", args=(self.case.pk, created.pk), query=origin
        )
        self.assertRedirects(response, detail_url, fetch_redirect_response=False)
        delete_url = reverse(
            "geo:poi_delete", args=(self.case.pk, created.pk), query=origin
        )
        self.assertContains(
            self.client.get(detail_url), f'href="{escape(delete_url)}">Delete POI'
        )
        self.assertContains(
            self.client.post(delete_url),
            f'href="{escape(detail_url)}">Cancel',
            status_code=400,
        )
        self.assertRedirects(
            self.client.post(delete_url, {"confirm": "on"}),
            back,
            fetch_redirect_response=False,
        )

    def test_person_return_survives_stale_edit_and_reassignment(self):
        self.client.force_login(self.editor)
        second_person = Person.objects.create(
            case=self.case, first_name="New association"
        )
        filters = {"q": "Synthetic", "page": "2", "poi_page": "3"}
        origin = {"from_person": self.person.pk} | filters
        detail_url = reverse(
            "geo:poi_detail", args=(self.case.pk, self.poi.pk), query=origin
        )
        edit_url = reverse(
            "geo:poi_edit", args=(self.case.pk, self.poi.pk), query=origin
        )
        back = reverse(
            "people:detail",
            args=(self.case.pk, self.person.pk),
            query=filters,
            fragment="poi-list",
        )
        self.assertContains(
            self.client.get(detail_url), f'href="{escape(edit_url)}">Edit POI'
        )
        payload = self.data(
            revision=PointOfInterestForm.revision_for(self.poi),
            person=second_person.pk,
        )
        self.poi.source = "Updated saved source"
        self.poi.save()
        stale = self.client.post(edit_url, payload)
        self.assertContains(
            stale, f'href="{escape(detail_url)}">Cancel', status_code=409
        )
        payload["revision"] = stale.context["form"]["revision"].value()
        self.assertRedirects(
            self.client.post(edit_url, payload),
            detail_url,
            fetch_redirect_response=False,
        )
        self.poi.refresh_from_db()
        self.assertEqual(self.poi.person_id, second_person.pk)
        detail = self.client.get(detail_url)
        self.assertContains(detail, "New association")
        self.assertContains(detail, f'href="{escape(back)}">Back to person')
        delete_url = reverse(
            "geo:poi_delete", args=(self.case.pk, self.poi.pk), query=origin
        )
        self.assertRedirects(
            self.client.post(delete_url, {"confirm": "on"}),
            back,
            fetch_redirect_response=False,
        )

    def test_foreign_or_malformed_person_origin_cannot_become_a_return_link(self):
        self.client.force_login(self.editor)
        for origin in (self.other_person.pk, "https://example.test/", uuid4()):
            with self.subTest(origin=origin):
                response = self.client.get(
                    self.poi_url("detail"),
                    {"from_person": origin, "next": "https://example.test/"},
                )
                self.assertContains(
                    response, f'href="{self.url_for_map()}#map-heading">Back to map'
                )
                self.assertNotContains(response, "Back to person")
                self.assertNotContains(response, str(origin))
                self.assertNotContains(response, "example.test")

    def test_outsiders_superusers_and_cross_case_ids_are_not_disclosed(self):
        for superuser in (False, True):
            self.outsider.is_superuser = superuser
            self.outsider.save()
            self.client.force_login(self.outsider)
            for action in ("create", "detail", "edit", "delete"):
                self.assertEqual(self.client.get(self.poi_url(action)).status_code, 404)
                if action != "detail":
                    self.assertEqual(
                        self.client.post(
                            self.poi_url(action), self.data(confirm="on")
                        ).status_code,
                        404,
                    )
        self.client.force_login(self.owner)
        for action in ("detail", "edit", "delete"):
            self.assertEqual(
                self.client.get(self.poi_url(action, self.other_poi)).status_code, 404
            )
            if action != "detail":
                self.assertEqual(
                    self.client.post(
                        self.poi_url(action, self.other_poi), self.data(confirm="on")
                    ).status_code,
                    404,
                )

    def test_authentication_email_verification_and_csrf(self):
        self.assertEqual(self.client.get(self.poi_url("create")).status_code, 302)
        self.owner.emailaddress_set.update(verified=False)
        self.client.force_login(self.owner)
        self.assertRedirects(
            self.client.get(self.poi_url("create")),
            reverse("account_email"),
            fetch_redirect_response=False,
        )
        self.owner.emailaddress_set.update(verified=True)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        for action in ("create", "edit", "delete"):
            self.assertEqual(
                client.post(self.poi_url(action), self.data(confirm="on")).status_code,
                403,
            )

    def test_edit_conflict_preserves_proposal_and_requires_review(self):
        self.client.force_login(self.editor)
        data = self.data(
            revision=PointOfInterestForm.revision_for(self.poi),
            source="My draft",
            notes="Forged notes",
        )
        self.poi.source = "Newer saved details"
        self.poi.save()
        response = self.client.post(self.poi_url("edit"), data)
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "Newer saved details", status_code=409)
        self.assertContains(response, "My draft", status_code=409)
        self.poi.refresh_from_db()
        self.assertEqual(self.poi.source, "Newer saved details")
        self.assertNotIn("notes", response.context["form"].fields)
        data["revision"] = response.context["form"]["revision"].value()
        self.assertEqual(self.client.post(self.poi_url("edit"), data).status_code, 302)
        self.poi.refresh_from_db()
        self.assertEqual(self.poi.source, "My draft")
        self.assertEqual(self.poi.notes, "Unconfirmed residence")
        self.assertContains(
            self.client.get(self.poi_url("detail")), "Unconfirmed residence"
        )
        self.assertEqual(self.poi.created_by, self.owner)
        for token in ("", "tampered", PointOfInterestForm.revision_for(self.other_poi)):
            self.assertEqual(
                self.client.post(
                    self.poi_url("edit"), self.data(revision=token)
                ).status_code,
                400,
            )

    def test_revoked_or_downgraded_access_blocks_open_forms(self):
        self.client.force_login(self.editor)
        data = self.data(revision=PointOfInterestForm.revision_for(self.poi))
        membership = CaseMembership.objects.get(case=self.case, user=self.editor)
        membership.role = "viewer"
        membership.save()
        for action in ("create", "edit", "delete"):
            self.assertEqual(
                self.client.post(self.poi_url(action), data).status_code, 403
            )
        membership.delete()
        for action in ("create", "detail", "edit", "delete"):
            self.assertEqual(self.client.get(self.poi_url(action)).status_code, 404)
        for action in ("create", "edit", "delete"):
            self.assertEqual(
                self.client.post(self.poi_url(action), data).status_code, 404
            )
        self.assertEqual(
            self.client.get(
                self.url_for_map(), HTTP_ACCEPT="application/json"
            ).status_code,
            404,
        )

    def test_editor_deletes_only_after_confirmation_and_stale_edit_cannot_recreate(
        self,
    ):
        self.client.force_login(self.editor)
        data = self.data(revision=PointOfInterestForm.revision_for(self.poi))
        edit_url = self.poi_url("edit")
        self.assertContains(self.client.get(self.poi_url("delete")), "Delete Home")
        self.assertEqual(self.client.post(self.poi_url("delete")).status_code, 400)
        self.assertTrue(PointOfInterest.objects.filter(pk=self.poi.pk).exists())
        self.assertEqual(
            self.client.post(self.poi_url("delete"), {"confirm": "on"}).status_code, 302
        )
        self.assertEqual(self.client.post(edit_url, data).status_code, 404)
        self.assertTrue(Person.objects.filter(pk=self.person.pk).exists())
        self.location.refresh_from_db()
        self.assertEqual(self.location.source, "Synthetic source")

    def test_person_and_case_deletion_cascade_only_annotations(self):
        person = Person.objects.create(case=self.case, first_name="Unlinked")
        poi = PointOfInterest.objects.create(
            person=person, created_by=self.owner, latitude=0, longitude=0
        )
        self.client.force_login(self.owner)
        case_deletion = self.client.get(
            reverse("cases:case_delete", args=(self.case.pk,))
        )
        self.assertEqual(case_deletion.context["poi_count"], 2)
        self.assertContains(case_deletion, "Person POIs")
        response = self.client.get(
            reverse("people:delete", args=(self.case.pk, person.pk))
        )
        self.assertContains(response, "1 POI")
        person.delete()
        self.assertFalse(PointOfInterest.objects.filter(pk=poi.pk).exists())
        self.assertTrue(PointOfInterest.objects.filter(pk=self.poi.pk).exists())
        self.case.delete()
        self.assertFalse(PointOfInterest.objects.filter(pk=self.poi.pk).exists())
        self.assertTrue(PointOfInterest.objects.filter(pk=self.other_poi.pk).exists())

    def test_database_constraints_reject_invalid_direct_writes(self):
        for values in (
            {"kind": "custom", "custom_label": " "},
            {"kind": "other"},
            {"latitude": 91},
            {"longitude": -181},
        ):
            with (
                self.subTest(values=values),
                self.assertRaises(IntegrityError),
                transaction.atomic(),
            ):
                PointOfInterest.objects.filter(pk=self.poi.pk).update(**values)
        for value in ("NaN", "Infinity", "-Infinity"):
            self.poi.latitude = Decimal(value)
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.poi.full_clean()
