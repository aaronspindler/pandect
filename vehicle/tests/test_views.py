from datetime import UTC, datetime
from uuid import uuid4

from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from cases.models import Case, CaseMembership
from core.case_search.tests.fixtures import indexed
from geo.models import VehicleLocationEvidence
from vehicle.models import Vehicle


class VehicleInterfaceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = cls.user("vehicle-owner")
        cls.editor = cls.user("vehicle-editor")
        cls.viewer = cls.user("vehicle-viewer")
        cls.outsider = cls.user("vehicle-outsider")
        cls.operator = cls.user("vehicle-operator", is_staff=True, is_superuser=True)
        cls.case = Case.objects.create(owner=cls.owner)
        cls.other_case = Case.objects.create(owner=cls.outsider)
        CaseMembership.objects.create(
            case=cls.case, user=cls.editor, role=CaseMembership.Role.EDITOR
        )
        CaseMembership.objects.create(case=cls.case, user=cls.viewer)
        cls.vehicle = Vehicle.objects.create(
            case=cls.case,
            make="Example Motors",
            model="Hatchback",
            license_plate="SYN-001",
            notes="Unconfirmed vehicle observation.",
        )
        cls.other_vehicle = Vehicle.objects.create(
            case=cls.other_case, make="Hidden source", notes="Other case secret"
        )

    @staticmethod
    def user(username, **kwargs):
        user = get_user_model().objects.create_user(
            username=username, email=f"{username}@example.com", **kwargs
        )
        EmailAddress.objects.create(
            user=user, email=user.email, verified=True, primary=True
        )
        return user

    def url(self, name="list", vehicle=None, case=None):
        kwargs = {"case_id": (case or self.case).pk}
        if vehicle is not None:
            kwargs["vehicle_id"] = vehicle.pk
        return reverse(f"vehicle:{name}", kwargs=kwargs)

    def edit_data(self, vehicle=None, **changes):
        vehicle = vehicle or self.vehicle
        response = self.client.get(self.url("edit", vehicle=vehicle))
        self.assertEqual(response.status_code, 200)
        return {
            "make": vehicle.make,
            "model": vehicle.model,
            "vin": vehicle.vin,
            "license_plate": vehicle.license_plate,
            "color": vehicle.color,
            "year": vehicle.year or "",
            "revision": response.context["form"]["revision"].value(),
            **changes,
        }

    def test_accounts_need_authentication_and_verified_email(self):
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)
        unverified = get_user_model().objects.create_user(
            username="vehicle-unverified", email="unverified@example.com"
        )
        EmailAddress.objects.create(
            user=unverified, email=unverified.email, primary=True, verified=False
        )
        self.client.force_login(unverified)
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Vehicle.objects.filter(make="Unexpected").exists())

    def test_all_case_roles_can_inspect_but_lists_never_leak_other_cases(self):
        for user in (self.owner, self.editor, self.viewer):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                response = self.client.get(self.url())
                self.assertContains(response, "Example Motors Hatchback")
                self.assertNotContains(response, "Hidden source")
                self.assertNotContains(response, "Other case secret")
                self.assertContains(
                    self.client.get(self.url("detail", vehicle=self.vehicle)),
                    "Unconfirmed vehicle observation.",
                )

    def test_unrelated_users_and_superusers_cannot_inspect_or_write(self):
        for user in (self.outsider, self.operator):
            self.client.force_login(user)
            for name, vehicle in (
                ("list", None),
                ("create", None),
                ("detail", self.vehicle),
                ("edit", self.vehicle),
                ("delete", self.vehicle),
            ):
                with self.subTest(user=user.username, name=name):
                    url = self.url(name, vehicle=vehicle)
                    self.assertEqual(self.client.get(url).status_code, 404)
                    self.assertEqual(
                        self.client.post(url, {"make": "Forged"}).status_code,
                        405 if name in ("list", "detail") else 404,
                    )

    def test_parent_case_and_vehicle_must_match_on_every_object_path(self):
        self.client.force_login(self.owner)
        for name in ("detail", "edit", "delete"):
            with self.subTest(name=name):
                url = self.url(name, vehicle=self.other_vehicle)
                self.assertEqual(self.client.get(url).status_code, 404)
                self.assertEqual(
                    self.client.post(url, {"confirm": "on"}).status_code,
                    405 if name == "detail" else 404,
                )
        self.assertTrue(Vehicle.objects.filter(pk=self.other_vehicle.pk).exists())

    def test_owner_and_editor_create_partial_identities_without_staff_permissions(self):
        for user, identity in (
            (self.owner, {"license_plate": "SYN-002"}),
            (self.editor, {"vin": "SYNTHETIC-PARTIAL"}),
        ):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                response = self.client.post(
                    self.url("create"),
                    {
                        **identity,
                        "case": str(self.other_case.pk),
                        "notes": "Forged notes",
                    },
                )
                self.assertEqual(response.status_code, 302)
                vehicle = Vehicle.objects.get(case=self.case, **identity)
                self.assertEqual(vehicle.notes, "")
                self.assertRedirects(
                    response,
                    self.url("detail", vehicle=vehicle),
                    fetch_redirect_response=False,
                )
                self.assertFalse(user.is_staff)

    def test_blank_identity_and_invalid_year_preserve_entered_values(self):
        self.client.force_login(self.editor)
        response = self.client.post(self.url("create"), {"color": "Blue"})
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "Blue", status_code=400)
        self.assertContains(response, "Enter at least", status_code=400)
        response = self.client.post(
            self.url("create"),
            {"make": "Proposed name", "year": "not-a-year"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "Proposed name", status_code=400)
        self.assertNotIn("notes", response.context["form"].fields)
        self.assertIn("year", response.context["form"].errors)
        self.assertEqual(Vehicle.objects.filter(case=self.case).count(), 1)

    def test_duplicate_identifiers_are_not_merged(self):
        self.client.force_login(self.editor)
        for _ in range(2):
            self.assertEqual(
                self.client.post(
                    self.url("create"), {"license_plate": "DUP-001"}
                ).status_code,
                302,
            )
        self.assertEqual(
            Vehicle.objects.filter(case=self.case, license_plate="DUP-001").count(),
            2,
        )

    def test_owner_and_editor_can_edit_without_moving_the_vehicle(self):
        for user in (self.owner, self.editor):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                self.vehicle.refresh_from_db()
                response = self.client.post(
                    self.url("edit", vehicle=self.vehicle),
                    self.edit_data(
                        color=f"Updated by {user.username}",
                        notes="Forged notes",
                        case=str(self.other_case.pk),
                    ),
                )
                self.assertEqual(response.status_code, 302)
                self.vehicle.refresh_from_db()
                self.assertEqual(self.vehicle.case_id, self.case.pk)
                self.assertEqual(self.vehicle.color, f"Updated by {user.username}")
                self.assertEqual(self.vehicle.notes, "Unconfirmed vehicle observation.")

    def test_viewer_is_read_only_and_mutations_remain_denied_directly(self):
        self.client.force_login(self.viewer)
        response = self.client.get(self.url("detail", vehicle=self.vehicle))
        self.assertContains(response, "viewer access")
        self.assertNotContains(response, "Edit vehicle")
        self.assertNotContains(response, "Delete vehicle")
        for name, vehicle in (
            ("create", None),
            ("edit", self.vehicle),
            ("delete", self.vehicle),
        ):
            with self.subTest(name=name):
                self.assertEqual(
                    self.client.get(self.url(name, vehicle)).status_code, 403
                )
                self.assertEqual(
                    self.client.post(
                        self.url(name, vehicle),
                        {"make": "Forged", "confirm": "on"},
                    ).status_code,
                    403,
                )
        self.vehicle.refresh_from_db()
        self.assertEqual(self.vehicle.make, "Example Motors")

    def test_stale_edit_shows_saved_details_and_preserves_proposal_for_review(self):
        self.client.force_login(self.editor)
        proposal = self.edit_data(color="My proposed value")
        self.vehicle.color = "Newer saved value"
        self.vehicle.save()
        response = self.client.post(self.url("edit", self.vehicle), proposal)
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "Newer saved value", status_code=409)
        self.assertContains(response, "My proposed value", status_code=409)
        self.assertContains(response, "Save reviewed changes", status_code=409)
        self.vehicle.refresh_from_db()
        self.assertEqual(self.vehicle.color, "Newer saved value")
        proposal["revision"] = response.context["form"]["revision"].value()
        reviewed = self.client.post(self.url("edit", self.vehicle), proposal)
        self.assertEqual(reviewed.status_code, 302)
        self.vehicle.refresh_from_db()
        self.assertEqual(self.vehicle.color, "My proposed value")

    def test_revision_cannot_be_missing_tampered_or_reused_for_another_vehicle(self):
        original = self.vehicle.color
        self.client.force_login(self.owner)
        data = self.edit_data(color="Forged change")
        for token in ("", "tampered", f"{data['revision']}tampered"):
            with self.subTest(token=token):
                response = self.client.post(
                    self.url("edit", self.vehicle), {**data, "revision": token}
                )
                self.assertEqual(response.status_code, 400)
        another = Vehicle.objects.create(case=self.case, make="Another")
        response = self.client.post(self.url("edit", another), data)
        self.assertEqual(response.status_code, 400)
        self.vehicle.refresh_from_db()
        self.assertEqual(self.vehicle.color, original)

    def test_revocation_and_role_downgrade_apply_to_an_already_open_form(self):
        original = self.vehicle.color
        self.client.force_login(self.editor)
        data = self.edit_data(color="No longer allowed")
        membership = CaseMembership.objects.get(case=self.case, user=self.editor)
        membership.role = CaseMembership.Role.VIEWER
        membership.save()
        self.assertEqual(
            self.client.post(self.url("edit", self.vehicle), data).status_code, 403
        )
        membership.delete()
        self.assertEqual(self.client.get(self.url()).status_code, 404)
        self.assertEqual(
            self.client.post(self.url("edit", self.vehicle), data).status_code, 404
        )
        self.vehicle.refresh_from_db()
        self.assertEqual(self.vehicle.color, original)

    def test_deleted_vehicle_is_not_recreated_by_a_stale_form(self):
        self.client.force_login(self.editor)
        data = self.edit_data(color="Stale changes")
        url = self.url("edit", self.vehicle)
        vehicle_id = self.vehicle.pk
        self.vehicle.delete()
        self.assertEqual(self.client.post(url, data).status_code, 404)
        self.assertFalse(Vehicle.objects.filter(pk=vehicle_id).exists())

    def test_only_owner_can_delete_after_explicit_confirmation(self):
        url = self.url("delete", self.vehicle)
        self.client.force_login(self.editor)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, {"confirm": "on"}).status_code, 403)
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(url), "Delete Example Motors Hatchback?")
        self.assertTrue(Vehicle.objects.filter(pk=self.vehicle.pk).exists())
        self.assertEqual(self.client.post(url).status_code, 400)
        self.assertTrue(Vehicle.objects.filter(pk=self.vehicle.pk).exists())
        response = self.client.post(url, {"confirm": "on"})
        self.assertRedirects(response, self.url(), fetch_redirect_response=False)
        self.assertFalse(Vehicle.objects.filter(pk=self.vehicle.pk).exists())
        self.assertTrue(Vehicle.objects.filter(pk=self.other_vehicle.pk).exists())
        self.assertTrue(Case.objects.filter(pk=self.case.pk).exists())
        self.assertTrue(get_user_model().objects.filter(pk=self.owner.pk).exists())

    def test_form_mutations_require_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        self.assertEqual(client.get(self.url("create")).status_code, 200)
        self.assertEqual(
            client.post(self.url("create"), {"make": "Without CSRF"}).status_code,
            403,
        )
        self.assertEqual(
            client.post(
                self.url("delete", self.vehicle), {"confirm": "on"}
            ).status_code,
            403,
        )

    def test_search_and_pagination_remain_case_scoped_and_keep_query_context(self):
        records = Vehicle.objects.bulk_create(
            [
                Vehicle(case=self.case, make=f"Paginated vehicle {index:02d}")
                for index in range(30)
            ]
        )
        self.enterContext(
            indexed(self.case, [("record", "vehicle", item.pk) for item in records])
        )
        self.client.force_login(self.viewer)
        response = self.client.get(self.url(), {"q": "Paginated vehicle", "page": 1})
        self.assertEqual(response.context["page_obj"].paginator.count, 30)
        self.assertEqual(len(response.context["page_obj"]), 25)
        self.assertContains(response, "q=Paginated+vehicle&amp;page=2")
        self.assertNotContains(response, "Hidden source")
        second = self.client.get(self.url(), {"q": "Paginated vehicle", "page": 2})
        self.assertEqual(len(second.context["page_obj"]), 5)
        response = self.client.get(self.url(), {"q": "Other case secret"})
        self.assertContains(response, "No matching vehicles")
        detail = self.client.get(
            self.url("detail", self.vehicle), {"q": "Example Motors", "page": 2}
        )
        self.assertContains(detail, "?q=Example+Motors&amp;page=2")

    def test_create_edit_and_delete_keep_search_and_page_context(self):
        self.client.force_login(self.owner)
        query = "?q=Example+%26+Motors&page=2"
        html_query = query.replace("&", "&amp;")
        collection = self.client.get(self.url() + query)
        self.assertContains(collection, self.url("create") + html_query)
        create = self.client.get(self.url("create") + query)
        self.assertContains(create, self.url() + html_query)
        response = self.client.post(self.url("create") + query, {"make": "New vehicle"})
        created = Vehicle.objects.get(case=self.case, make="New vehicle")
        self.assertRedirects(
            response, self.url("detail", created) + query, fetch_redirect_response=False
        )

        response = self.client.post(
            self.url("edit", self.vehicle) + query,
            self.edit_data(color="Blue"),
        )
        self.assertRedirects(
            response,
            self.url("detail", self.vehicle) + query,
            fetch_redirect_response=False,
        )
        detail = self.client.get(self.url("detail", created) + query)
        self.assertContains(detail, self.url("delete", created) + html_query)
        confirmation = self.client.get(self.url("delete", created) + query)
        self.assertContains(confirmation, self.url("detail", created) + html_query)
        response = self.client.post(
            self.url("delete", created) + query, {"confirm": "on"}
        )
        self.assertRedirects(
            response, self.url() + query, fetch_redirect_response=False
        )

    def test_notes_are_escaped_and_missing_details_are_clear(self):
        self.vehicle.notes = '<script>alert("untrusted")</script>\nUncertain source'
        self.vehicle.save()
        self.client.force_login(self.viewer)
        response = self.client.get(self.url("detail", self.vehicle))
        self.assertNotContains(response, '<script>alert("untrusted")</script>')
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, "Not recorded")
        self.assertContains(response, self.vehicle.license_plate)
        self.assertContains(response, "Uncertain source")

    def test_detail_leads_with_latest_dated_observation_and_links_to_trips(self):
        self.client.force_login(self.viewer)
        detail_url = self.url("detail", self.vehicle)
        observation = {
            "case": self.case,
            "uploaded_by": self.owner,
            "vehicle": self.vehicle,
        }
        undated = VehicleLocationEvidence.objects.create(
            **observation, title="Undated evidence", address="10 Example Street"
        )
        response = self.client.get(detail_url)
        self.assertContains(
            response, "The recorded locations have no observation times."
        )
        self.assertContains(response, "Undated evidence")
        self.assertIsNone(response.context["latest_observation"])
        VehicleLocationEvidence.objects.create(
            **observation,
            title="Dated archived evidence",
            address="20 Example Street",
            observed_at=datetime(2026, 9, 16, 18, 57, 22, tzinfo=UTC),
            is_archived=True,
            archive_changed_by=self.owner,
            archive_changed_at=datetime(2026, 9, 17, tzinfo=UTC),
        )
        dated = VehicleLocationEvidence.objects.create(
            **observation,
            title="Dated active evidence",
            address="15 Example Street",
            observed_at=datetime(2026, 9, 15, tzinfo=UTC),
        )
        response = self.client.get(detail_url)
        self.assertEqual(response.context["latest_observation"], dated)
        self.assertEqual(list(response.context["locations"]), [dated, undated])
        self.assertContains(response, "15 Example Street")
        self.assertNotContains(response, "20 Example Street")
        self.assertLess(
            response.content.index(b'id="locations-heading"'),
            response.content.index(b'id="trips-heading"'),
        )
        trips_url = reverse("geo:case_map", args=[self.case.pk])
        self.assertContains(
            response,
            f'href="{trips_url}?vehicle_id={self.vehicle.pk}#id_day">Jump to date</a>',
        )

    def test_search_includes_vehicle_details_and_bounds_numeric_queries(self):
        self.vehicle.year = 2020
        self.vehicle.color = "Blue"
        self.vehicle.vin = "SYNTHETIC-VIN-001"
        self.vehicle.save()
        self.enterContext(indexed(self.case, [("record", "vehicle", self.vehicle.pk)]))
        self.client.force_login(self.viewer)
        for query in (
            "Example Motors",
            "Hatchback",
            "2020",
            "Blue",
            "SYN-001",
            "VIN-001",
            "observation",
        ):
            with self.subTest(query=query):
                response = self.client.get(self.url(), {"q": query})
                self.assertEqual(list(response.context["page_obj"]), [self.vehicle])
        for query in ("9" * 200, "²", "10000"):
            with self.subTest(query=query):
                self.assertContains(
                    self.client.get(self.url(), {"q": query}), "No matching vehicles"
                )

    def test_private_pages_are_not_cached(self):
        self.client.force_login(self.owner)
        for name, vehicle in (
            ("list", None),
            ("detail", self.vehicle),
            ("edit", self.vehicle),
        ):
            response = self.client.get(self.url(name, vehicle))
            self.assertIn("no-store", response["Cache-Control"])
            self.assertIn("private", response["Cache-Control"])

    def test_case_deletion_includes_only_its_vehicles(self):
        self.client.force_login(self.owner)
        url = reverse("cases:case_delete", args=[self.case.pk])
        response = self.client.get(url)
        self.assertEqual(response.context["vehicles_count"], 1)
        self.assertContains(response, "Vehicles")
        response = self.client.post(url, {"confirm": "on"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Vehicle.objects.filter(pk=self.vehicle.pk).exists())
        self.assertTrue(Vehicle.objects.filter(pk=self.other_vehicle.pk).exists())

    def test_unknown_uuid_and_non_uuid_paths_are_not_found(self):
        self.client.force_login(self.owner)
        url = reverse(
            "vehicle:detail", kwargs={"case_id": self.case.pk, "vehicle_id": uuid4()}
        )
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(
            self.client.get(url.replace(str(self.case.pk), "123")).status_code, 404
        )
