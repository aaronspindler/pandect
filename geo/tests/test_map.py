"""Saved-trip readers keep whole trips, bounded payloads and case access."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from allauth.account.models import EmailAddress
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from cases.models import CaseMembership
from core.analysis.catalogue import project_result
from core.analysis.operations import get_operation
from core.analysis.services import snapshot_sources
from core.models import AnalysisResult, AnalysisRun
from geo.models import PointOfInterest, VehicleLocationEvidence
from geo.poi_forms import PointOfInterestForm
from geo.testing import MapCaseFixtures
from geo.trip_jobs import _snapshot
from geo.trip_models import (
    VehicleTrip,
    VehicleTripGeneration,
    VehicleTripInput,
    VehicleTripSource,
    VehicleTripState,
)
from geo.trip_processing import METHOD_VERSION, observation_trip_url
from people.models import Person
from vehicle.models import Vehicle

INSTANT = datetime(2026, 11, 1, 12, tzinfo=UTC)


class SavedTripFixtures(MapCaseFixtures):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.state = VehicleTripState.objects.create(case=cls.case, vehicle=cls.vehicle)
        cls.generation = VehicleTripGeneration.objects.create(
            case=cls.case,
            vehicle=cls.vehicle,
            state=cls.state,
            revision=0,
            method_version=METHOD_VERSION,
            requested_by=cls.owner,
            status="complete",
            geocode_status="complete",
        )
        cls.state.current_generation = cls.generation
        cls.state.save(update_fields=("current_generation",))
        cls.trip, cls.observations = cls.add_trip()
        cls.detail_url = reverse("geo:trip_detail", args=(cls.case.pk, cls.trip.pk))

    @classmethod
    def add_trip(cls, count=3, *, minute=0, addresses=False, state="active", speeds=()):
        records = []
        for index in range(count):
            item = VehicleLocationEvidence(
                case=cls.case,
                uploaded_by=cls.owner,
                vehicle=cls.vehicle,
                title=f"Trip source {minute}-{index}",
                address=f"{index} Synthetic road" if addresses else "",
                latitude=None if addresses else "43.64",
                longitude=None
                if addresses
                else str(Decimal("-79.38") + Decimal(index) / 100000),
                observed_at=INSTANT + timedelta(minutes=minute, seconds=index),
                speed_kmh=speeds[index] if speeds else None,
                is_archived=state == "archived",
                archive_changed_by=cls.owner if state == "archived" else None,
                archive_changed_at=INSTANT if state == "archived" else None,
            )
            item._prepare_original()
            records.append(item)
        VehicleLocationEvidence.objects.bulk_create(records)
        base = cls.generation.inputs.count()
        inputs = [
            VehicleTripInput(
                case=cls.case,
                generation=cls.generation,
                observation=item,
                ordinal=base + index + 1,
                sha256=item.sha256,
                snapshot=_snapshot(item),
            )
            for index, item in enumerate(records)
        ]
        VehicleTripInput.objects.bulk_create(inputs)
        VehicleTripGeneration.objects.filter(pk=cls.generation.pk).update(
            total_sources=base + count, processed_sources=base + count
        )
        trip = VehicleTrip.objects.create(
            case=cls.case,
            vehicle=cls.vehicle,
            generation=cls.generation,
            evidence_state=state,
            ordinal=cls.generation.trips.count() + 1,
            start_at=records[0].observed_at,
            end_at=records[-1].observed_at,
            source_count=count,
            basis="Reported addresses" if addresses else "Reported coordinates",
        )
        VehicleTripSource.objects.bulk_create(
            [
                VehicleTripSource(
                    case=cls.case, trip=trip, input=item, ordinal=item.ordinal
                )
                for item in inputs
            ]
        )
        return trip, records


@override_settings(PANDECT_MAP_TILE_URL="", PANDECT_MAP_TILE_ORIGIN="")
class InvestigationMapTests(SavedTripFixtures, TestCase):
    def test_poi_actions_keep_the_selected_trip_and_its_filters(self):
        self.client.force_login(self.editor)
        filters = {
            "q": "Synthetic",
            "page": "2",
            "poi_page": "1",
            "vehicle_id": str(self.vehicle.pk),
            "day": "2026-11-01",
            "gap_page": "2",
            "sources_page": "2",
            "findings_page": "3",
            "view": "estimates",
        }
        origin = {"from_trip": self.trip.pk} | filters
        trip_url = reverse(
            "geo:trip_detail",
            args=(self.case.pk, self.trip.pk),
            query=filters,
            fragment="map-heading",
        )
        selected_trip = self.client.get(
            self.detail_url, filters | {"next": "https://example.test/"}
        )
        self.assertEqual(selected_trip.status_code, 200)
        create_url = selected_trip.context["poi_create_url"]
        self.assertEqual(
            create_url, reverse("geo:poi_create", args=(self.case.pk,), query=origin)
        )
        form = self.client.get(create_url)
        self.assertEqual(form.context["back_url"], trip_url)
        self.assertContains(form, "Back to trip")
        data = {
            "person": self.person.pk,
            "kind": "home",
            "latitude": "43.64",
            "longitude": "-79.38",
        }
        invalid = self.client.post(create_url, data | {"latitude": ""})
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.context["back_url"], trip_url)
        created = self.client.post(create_url, data)
        poi = PointOfInterest.objects.get(person=self.person)
        poi_url = reverse("geo:poi_detail", args=(self.case.pk, poi.pk), query=origin)
        self.assertRedirects(created, poi_url)
        selected_trip = self.client.get(trip_url)
        self.assertEqual(selected_trip.context["points"][0]["url"], poi_url)
        detail = self.client.get(poi_url)
        self.assertEqual(detail.context["back_url"], trip_url)

        edit_url = detail.context["edit_url"]
        self.assertEqual(self.client.get(edit_url).context["back_url"], poi_url)
        edited = self.client.post(
            edit_url,
            data | {"kind": "work", "revision": PointOfInterestForm.revision_for(poi)},
        )
        self.assertRedirects(edited, poi_url)
        poi.refresh_from_db()
        self.assertEqual(poi.kind, "work")

        delete_url = self.client.get(poi_url).context["delete_url"]
        self.assertEqual(self.client.get(delete_url).context["back_url"], poi_url)
        self.assertRedirects(self.client.post(delete_url, {"confirm": "on"}), trip_url)
        self.assertFalse(PointOfInterest.objects.filter(pk=poi.pk).exists())

    def test_unavailable_trip_origin_returns_to_filtered_map(self):
        self.state.current_generation = None
        self.state.save(update_fields=("current_generation",))
        for case, user in ((self.case, self.owner), (self.other, self.outsider)):
            with self.subTest(case=case.pk):
                self.client.force_login(user)
                person = Person.objects.create(case=case, first_name="Synthetic person")
                poi = PointOfInterest.objects.create(
                    person=person, created_by=user, kind="home", latitude=0, longitude=0
                )
                filters = {"q": "Synthetic"}
                map_url = reverse(
                    "geo:case_map",
                    args=(case.pk,),
                    query=filters,
                    fragment="map-heading",
                )
                poi_url = reverse(
                    "geo:poi_detail",
                    args=(case.pk, poi.pk),
                    query={"from_trip": self.trip.pk, "view": "estimates"} | filters,
                )
                detail = self.client.get(poi_url)
                self.assertEqual(detail.context["back_url"], map_url)
                self.assertContains(detail, "Back to map")
                delete_url = detail.context["delete_url"]
                self.assertEqual(
                    delete_url,
                    reverse(
                        "geo:poi_delete",
                        args=(case.pk, poi.pk),
                        query={"from_map": "1"} | filters,
                    ),
                )
                self.assertRedirects(
                    self.client.post(delete_url, {"confirm": "on"}), map_url
                )

    def test_legacy_archive_filters_and_trip_links_do_not_expose_archived_data(self):
        VehicleTripGeneration.objects.filter(pk=self.generation.pk).update(
            method_version="vehicle-trips.v1"
        )
        archived, _ = self.add_trip(minute=60, state="archived")
        self.client.force_login(self.owner)
        response = self.client.get(self.url, {"state": "all"})
        self.assertEqual(
            [item["id"] for item in response.context["trips"]], [str(self.trip.pk)]
        )
        self.assertNotContains(response, 'name="state"')
        self.assertEqual(
            self.client.get(
                reverse("geo:trip_detail", args=(self.case.pk, archived.pk))
            ).status_code,
            404,
        )

    def test_summary_page_does_not_repeat_history_checks_for_every_trip(self):
        for minute in range(1, 26):
            self.add_trip(minute=minute)
        self.client.force_login(self.owner)
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.url)
        self.assertEqual(len(response.context["trips"]), 25)
        history_checks = [
            query
            for query in queries
            if '"geo_vehicletripinput"' in query["sql"] and "EXISTS(" in query["sql"]
        ]
        # Complete-generation validation belongs at reader boundaries; adding
        # summaries must not multiply full-history scans by the page size.
        self.assertLess(len(history_checks), 10)
        endpoint_queries = [
            query for query in queries if 'FROM "geo_vehicletripsource"' in query["sql"]
        ]
        self.assertEqual(len(endpoint_queries), 1)
        self.assertContains(
            response, f'href="{response.context["trips"][0]["trip_url"]}"', count=1
        )

    def test_empty_map_links_to_vehicle_intake_for_editors(self):
        self.state.current_generation = None
        self.state.save(update_fields=("current_generation",))
        for user in (self.viewer, self.editor):
            self.client.force_login(user)
            response = self.client.get(self.url)
            self.assertContains(
                response, "Import vehicle locations", count=int(user == self.editor)
            )
            self.assertContains(
                response, "Add a vehicle location", count=int(user == self.editor)
            )

    def test_roles_read_saved_trips_without_raw_locations_or_get_side_effects(self):
        for role in ("owner", "editor", "viewer"):
            with self.subTest(role=role):
                self.client.force_login(getattr(self, role))
                with patch("geo.map_views.prepare_case_trips") as prepare:
                    response = self.client.get(self.url)
                prepare.assert_not_called()
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["selected_trip"]["source_count"], 3)
                self.assertEqual(response.context["points"], [])
                self.assertNotContains(response, "Harbour observation")
                self.assertNotContains(response, "Add location")
                self.assertNotContains(response, "Secret other location")
                self.assertNotContains(response, "Prepare trips")
                self.assertNotContains(response, "data-auto-prepare")
                self.assertIn("no-store", response["Cache-Control"])
                self.assertEqual(response["Referrer-Policy"], "same-origin")
                self.assertEqual(self.client.post(self.url).status_code, 405)
                detail = self.client.get(self.detail_url)
                self.assertContains(detail, "Possible trip")
                self.assertContains(detail, "First observation")
                self.assertContains(detail, "Last observation")
                for observation in [self.observations[0], self.observations[-1]]:
                    original = reverse(
                        "core:evidence:detail",
                        args=(self.case.pk, "vehicle-location", observation.pk),
                    )
                    self.assertContains(detail, f'href="{original}"', count=2)
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(self.detail_url).status_code, 404)
        self.outsider.is_superuser = True
        self.outsider.save()
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.client.force_login(self.viewer)
        CaseMembership.objects.filter(case=self.case, user=self.viewer).delete()
        self.assertEqual(
            self.client.get(
                self.detail_url, HTTP_ACCEPT="application/json"
            ).status_code,
            404,
        )

    def test_authentication_verification_and_prepare_permissions(self):
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.client.force_login(self.owner)
        EmailAddress.objects.filter(user=self.owner).update(verified=False)
        self.assertRedirects(
            self.client.get(self.url),
            reverse("account_email"),
            fetch_redirect_response=False,
        )
        self.client.force_login(self.viewer)
        prepare_url = reverse("geo:prepare_trips", args=(self.case.pk,))
        self.assertEqual(self.client.post(prepare_url).status_code, 403)
        self.client.force_login(self.editor)
        with patch("geo.map_views.prepare_case_trips", return_value=[]) as prepare:
            self.assertEqual(
                self.client.post(
                    prepare_url, {"vehicle_id": self.vehicle.pk}
                ).status_code,
                302,
            )
        self.assertEqual(
            prepare.call_args.kwargs["vehicle_ids"], [str(self.vehicle.pk)]
        )
        self.assertEqual(self.client.post(self.url + "locate/").status_code, 404)

    def test_more_than_500_observations_load_completely_in_bounded_chunks(self):
        trip, records = self.add_trip(count=1005, minute=60)
        url = reverse("geo:trip_detail", args=(self.case.pk, trip.pk))
        self.client.force_login(self.viewer)
        response = self.client.get(self.url)
        self.assertEqual(response.context["selected_trip"]["source_count"], 1005)
        self.assertNotContains(response, records[500].title)
        cursor, total, revision = 0, 0, None
        while cursor is not None:
            params = {"cursor": cursor}
            if revision:
                params["revision"] = revision
            response = self.client.get(url, params, HTTP_ACCEPT="application/json")
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertFalse(data["can_reuse"])
            self.assertLessEqual(len(data["positions"]), 500)
            self.assertTrue(
                all(row["position"] is not None for row in data["positions"])
            )
            total += len(data["positions"])
            cursor, revision = data["next_cursor"], data["revision"]
        self.assertEqual(total, 1005)
        detail = self.client.get(url, {"sources_page": 2})
        self.assertEqual(len(detail.context["source_rows"]), 50)
        self.assertEqual(detail.context["source_page"].paginator.count, 1005)
        self.assertContains(
            detail, records[50].title, count=0
        )  # Display places, not raw titles.
        self.assertEqual(detail.context["source_rows"][0]["id"], str(records[50].pk))

    def test_reported_geometry_can_recheck_without_loading_positions(self):
        self.client.force_login(self.viewer)
        data = self.client.get(self.detail_url, HTTP_ACCEPT="application/json").json()
        self.assertTrue(data["can_reuse"])
        params = {"revision": data["revision"], "recheck": "1"}
        with patch("geo.map_views.source_positions") as positions:
            response = self.client.get(
                self.detail_url, params, HTTP_ACCEPT="application/json"
            )
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")
        positions.assert_not_called()
        CaseMembership.objects.filter(case=self.case, user=self.viewer).delete()
        self.assertEqual(
            self.client.get(
                self.detail_url, params, HTTP_ACCEPT="application/json"
            ).status_code,
            404,
        )

    def test_geometry_checks_requested_memberships_without_full_generation_scan(self):
        trip, _ = self.add_trip(count=505, minute=60)
        url = reverse("geo:trip_detail", args=(self.case.pk, trip.pk))
        self.client.force_login(self.viewer)
        with patch(
            "geo.trip_processing.current_generations",
            side_effect=AssertionError("Geometry scans only the requested slice"),
        ):
            first = self.client.get(url, HTTP_ACCEPT="application/json").json()
            self.assertEqual(len(first["positions"]), 500)
            next_cursor = first["next_cursor"]
            trip.sources.filter(ordinal=next_cursor + 1).delete()
            self.assertEqual(
                self.client.get(
                    url,
                    {"cursor": next_cursor, "revision": first["revision"]},
                    HTTP_ACCEPT="application/json",
                ).status_code,
                404,
            )

    def test_findings_match_exact_citations_across_all_trip_observation_pages(self):
        trip, records = self.add_trip(count=53, minute=30)
        other = self.observations[0]
        inputs = snapshot_sources(
            self.case,
            [
                {"kind": "vehicle-location", "id": item.pk}
                for item in (records[0], records[51], other)
            ],
        )
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.owner,
            operation="geocode_locations",
            method_version=get_operation("geocode_locations").version,
            inputs=inputs,
            status="complete",
        )
        items = [
            {
                "label": f"Reading {i + 1}",
                "source_ids": [inputs[1]["id"]],
            }
            for i in range(25)
        ]
        items.extend(
            [
                {"label": "Shared finding", "source_ids": [s["id"] for s in inputs]},
                {"label": "Unrelated observation", "source_ids": [inputs[2]["id"]]},
            ]
        )
        result = AnalysisResult.objects.create(
            case=self.case, run=run, data={"items": items}
        )
        project_result(result)
        url = reverse("geo:trip_detail", args=(self.case.pk, trip.pk))
        self.client.force_login(self.viewer)
        first = self.client.get(url)
        self.assertEqual(first.context["source_page"].number, 1)
        self.assertEqual(first.context["findings_page"].paginator.count, 26)
        self.assertContains(first, "Reading 25")
        self.assertNotContains(first, "Unrelated observation")
        second = self.client.get(url, {"findings_page": 2, "sources_page": 2})
        self.assertEqual(
            [row["label"] for row in second.context["findings_page"]],
            ["Shared finding"],
        )
        self.assertNotContains(second, "Unrelated observation")
        self.assertEqual(second.context["source_page"].number, 2)
        # A mismatched retained hash cannot associate a finding with this trip.
        result.findings.get(label="Reading 1").sources.update(source_sha256="b" * 64)
        self.assertEqual(
            self.client.get(url).context["findings_page"].paginator.count, 25
        )

    def test_map_geometry_and_trip_detail_preserve_reported_speeds(self):
        trip, records = self.add_trip(minute=60, speeds=("27.125", 0, None))
        url = reverse("geo:trip_detail", args=(self.case.pk, trip.pk))
        self.client.force_login(self.viewer)

        response = self.client.get(url, HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [row["speed_kmh"] for row in response.json()["positions"]],
            [27.125, 0, None],
        )

        detail = self.client.get(url)
        self.assertContains(detail, "Reported speed 27.125 km/h")
        self.assertContains(detail, "Reported speed 0 km/h")
        self.assertContains(detail, "Reported speed unavailable")
        for record in records:
            source_url = reverse(
                "core:evidence:detail",
                args=(self.case.pk, "vehicle-location", record.pk),
            )
            self.assertContains(detail, f'href="{source_url}"')

    def test_day_jump_opens_the_target_page_and_keeps_whole_trips(self):
        for index in range(26):
            self.add_trip(minute=1440 + index * 120)
        self.client.force_login(self.viewer)
        self.client.cookies["pandect_timezone"] = "UTC"
        response = self.client.get(
            self.url, {"vehicle_id": self.vehicle.pk, "day": "2026-11-01"}
        )
        self.assertEqual(response.context["page_obj"].number, 2)
        self.assertEqual(response.context["page_obj"].paginator.count, 27)
        self.assertEqual(len(response.context["trips"]), 2)
        self.assertEqual(response.context["selected_trip"]["id"], str(self.trip.pk))
        self.assertEqual(response.context["selected_trip"]["source_count"], 3)
        self.assertEqual(response.context["geometry_url"], self.detail_url)
        self.assertContains(response, 'id="trip-day-result"')
        self.assertNotContains(response, 'name="start"')
        self.assertNotContains(response, 'name="end"')

    def test_case_map_rejects_foreign_vehicle_filter(self):
        self.client.force_login(self.viewer)
        other_vehicle = Vehicle.objects.create(case=self.other, make="Secret")
        response = self.client.get(self.url, {"vehicle_id": other_vehicle.pk})
        self.assertEqual(response.context["trips"], [])
        self.assertIn("vehicle_id", response.context["form"].errors)
        self.assertNotContains(response, "Secret")

    def test_stale_cross_case_checksum_and_archive_changes_withhold_trip(self):
        self.client.force_login(self.viewer)
        record = self.observations[0]
        evidence_url = reverse(
            "core:evidence:detail", args=(self.case.pk, "vehicle-location", record.pk)
        )
        self.assertEqual(observation_trip_url(self.case, record), self.detail_url)
        self.assertEqual(observation_trip_url(self.other, record), "")
        self.assertContains(self.client.get(evidence_url), f'href="{self.detail_url}"')
        for change in (
            {"sha256": "b" * 64},
            {
                "is_archived": True,
                "archive_changed_by_id": self.owner.pk,
                "archive_changed_at": INSTANT,
            },
            {"vehicle_id": Vehicle.objects.create(case=self.other, make="Private").pk},
        ):
            original = {field: getattr(record, field) for field in change}
            VehicleLocationEvidence.objects.filter(pk=record.pk).update(**change)
            self.assertEqual(self.client.get(self.detail_url).status_code, 404)
            self.assertEqual(observation_trip_url(self.case, record), "")
            self.assertEqual(
                self.client.get(self.url).context["page_obj"].paginator.count, 0
            )
            VehicleLocationEvidence.objects.filter(pk=record.pk).update(**original)
        self.state.current_generation = None
        self.state.save(update_fields=("current_generation",))
        self.assertEqual(self.client.get(self.detail_url).status_code, 404)
        self.assertEqual(observation_trip_url(self.case, record), "")
        self.assertNotContains(
            self.client.get(evidence_url), f'href="{self.detail_url}"'
        )

    @override_settings(
        PANDECT_MAP_TILE_URL="https://maps.example.test/{z}/{x}/{y}.png",
        PANDECT_MAP_TILE_ORIGIN="https://maps.example.test",
        PANDECT_MAP_TILE_ATTRIBUTION="Synthetic provider",
    )
    def test_tiles_are_private_map_scoped_and_disabled_basemaps_need_no_provider(self):
        self.client.force_login(self.owner)
        for url in (self.url, self.detail_url):
            response = self.client.get(url)
            self.assertContains(
                response, 'data-tile-url="https://maps.example.test/{z}/{x}/{y}.png"'
            )
            self.assertIn(
                "https://maps.example.test", response["Content-Security-Policy"]
            )
        response = self.client.get(reverse("core:evidence:list", args=(self.case.pk,)))
        self.assertNotIn(
            "https://maps.example.test", response["Content-Security-Policy"]
        )


class PersonPOIMapTests(MapCaseFixtures, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
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
        person = Person.objects.create(case=cls.other, first_name="Secret person")
        PointOfInterest.objects.create(
            person=person, created_by=cls.outsider, kind="work", latitude=1, longitude=1
        )

    def test_pois_remain_case_scoped_escaped_and_accessible_for_viewers(self):
        for user in (self.owner, self.editor, self.viewer):
            self.client.force_login(user)
            response = self.client.get(self.url)
            self.assertEqual(len(response.context["points"]), 1)
            self.assertEqual(response.context["points"][0]["number"], "P1")
            self.assertContains(response, "&lt;/script&gt;")
            self.assertNotContains(response, self.poi.custom_label)
            self.assertNotContains(response, "Secret person")
            self.assertEqual(
                "data-poi-create-url=" in response.content.decode(), user != self.viewer
            )
        for query in ("Synthetic person", "POI address", "POI citation"):
            self.assertEqual(
                self.client.get(self.url, {"q": query})
                .context["poi_page"]
                .paginator.count,
                1,
            )
        self.assertEqual(
            self.client.get(self.url, {"q": "Secret person"})
            .context["poi_page"]
            .paginator.count,
            0,
        )

    def test_poi_pagination_and_polar_coordinates_remain_explicit(self):
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
        self.client.force_login(self.viewer)
        response = self.client.get(self.url, {"q": "work"})
        self.assertEqual(len(response.context["poi_page"]), 100)
        self.assertEqual(response.context["poi_page"].paginator.count, 102)
        self.assertEqual(response.context["points"], [])
        self.assertContains(response, "Outside map display range")
        second = self.client.get(self.url, {"q": "work", "poi_page": 2})
        self.assertEqual(len(second.context["poi_page"]), 2)
        self.assertEqual(second.context["poi_page"][0].number, "P101")

    def test_initial_preparation_is_explicit_and_hidden_once_work_is_queued(self):
        self.client.force_login(self.owner)
        with patch("geo.map_views.prepare_case_trips") as prepare:
            initial = self.client.get(self.url)
        prepare.assert_not_called()
        self.assertTrue(initial.context["needs_prepare"])
        self.assertContains(initial, "data-auto-prepare")
        state = VehicleTripState.objects.create(case=self.case, vehicle=self.vehicle)
        VehicleTripGeneration.objects.create(
            case=self.case,
            vehicle=self.vehicle,
            state=state,
            revision=state.revision,
            method_version=METHOD_VERSION,
            requested_by=self.owner,
            status="queued",
        )
        queued = self.client.get(self.url)
        self.assertTrue(queued.context["pending"])
        self.assertFalse(queued.context["needs_prepare"])
        self.assertNotContains(queued, "data-auto-prepare")
