import json
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.conf import settings
from django.db import connection
from django.test import Client, RequestFactory, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from cases.models import CaseMembership
from core.analysis.services import process_run
from core.models import AnalysisResult, AnalysisReview, AnalysisRun, GatewayRequest
from geo.analysis.external.geocoding import Geocoder, GeocodingError
from geo.map_views import map_data, map_page
from geo.models import LocationEvidence, PersonLocationEvidence, VehicleLocationEvidence
from geo.testing import LOCAL_GEOCODER, MapCaseFixtures
from people.models import Person
from vehicle.models import Vehicle


@override_settings(**LOCAL_GEOCODER)
class LocalMapGeocodingTests(MapCaseFixtures, TestCase):
    """Map integration, using the same synthetic case/roles as its reader tests."""

    def locate(self, user=None, *, query=None, **body):
        self.client.force_login(user or self.owner)
        url = reverse("geo:map_locate", args=(self.case.pk,))
        snapshot = self.client.get(
            self.url + (query or ""), HTTP_ACCEPT="application/json"
        )
        if snapshot.status_code == 200:
            body.setdefault("page_key", snapshot.json()["page_key"])
        return self.client.post(
            url + (query or ""), body, HTTP_ACCEPT="application/json"
        )

    def run_lookup(self, candidates=None):
        response = self.locate()
        self.assertEqual(response.status_code, 200)
        run = AnalysisRun.objects.latest("created_at")
        with patch.object(
            Geocoder,
            "lookup",
            return_value=candidates
            or [
                {
                    "latitude": 43.65,
                    "longitude": -79.39,
                    "address": "Resolved synthetic road",
                    "granularity": "street",
                    "reference_id": "way:1",
                    "licence": "Synthetic licence",
                }
            ],
        ):
            process_run(str(run.pk))
            run.refresh_from_db()
            self.assertEqual(run.status, "complete")
        return run

    def test_local_background_results_map_without_changing_originals(self):
        original = (self.address.address, self.address.latitude, self.address.sha256)
        response = self.client.get(self.url)  # No actor and no GET side effects.
        self.assertEqual(AnalysisRun.objects.count(), 0)
        response = self.locate()
        self.assertTrue(response.json()["queued"])
        run = AnalysisRun.objects.get()
        self.assertFalse(run.external_processing)
        self.assertEqual(run.parameters["_execution"]["location"], "local")
        self.assertEqual(run.inputs[0]["evidence_id"], str(self.address.pk))
        self.assertFalse(self.locate().json()["queued"])
        self.assertEqual(AnalysisRun.objects.count(), 1)
        self.assertTrue(
            self.client.get(self.url, HTTP_ACCEPT="application/json").json()["pending"]
        )
        response_bytes = json.dumps(
            [
                {
                    "lat": "43.65",
                    "lon": "-79.39",
                    "display_name": "Synthetic result",
                    "osm_type": "way",
                    "osm_id": 1,
                    "type": "street",
                    "licence": "Synthetic licence",
                }
            ]
        ).encode()
        with patch("geo.analysis.external.geocoding.build_opener") as build:
            response = build.return_value.open.return_value.__enter__.return_value
            response.read.return_value = response_bytes
            process_run(str(run.pk))
            run.refresh_from_db()
            self.assertEqual(run.status, "complete")
            sent = build.return_value.open.call_args.args[0]
        endpoint = urlsplit(sent.full_url)
        self.assertEqual(
            (endpoint.scheme, endpoint.netloc, endpoint.path),
            ("http", "geocoder.internal:8080", "/search"),
        )
        self.assertEqual(
            parse_qs(endpoint.query),
            {
                "format": ["jsonv2"],
                "addressdetails": ["1"],
                "limit": ["5"],
                "q": [self.address.address],
            },
        )
        self.assertIsNone(sent.data)
        self.assertEqual(GatewayRequest.objects.count(), 0)
        data = self.client.get(self.url, HTTP_ACCEPT="application/json").json()
        point = next(point for point in data["points"] if point.get("derived"))
        self.assertEqual(point["label"], "Unreviewed address estimate")
        self.assertEqual(data["mapped_count"], 4)
        self.assertFalse(data["pending"])
        self.assertFalse(data["can_queue"])
        self.assertIn("Synthetic local attribution", point["attribution"])
        self.address.refresh_from_db()
        self.assertEqual(
            (self.address.address, self.address.latitude, self.address.sha256), original
        )
        result = AnalysisResult.objects.get(run=run)
        self.assertEqual(
            result.provenance["execution"]["url"], settings.PANDECT_GEOCODER_URL
        )

    def test_locality_missing_configuration_and_drift_never_send_addresses(self):
        for changes in (
            {
                "PANDECT_GEOCODER_LOCAL": False,
                "PANDECT_GEOCODER_URL": "https://external.example.test",
            },
            {"PANDECT_GEOCODER_URL": ""},
            {"PANDECT_GEOCODER_URL": "http://["},
            {"PANDECT_GEOCODER_URL": "https://nominatim.openstreetmap.org"},
            {"PANDECT_GEOCODER_NAME": ""},
        ):
            with (
                self.subTest(changes=changes),
                override_settings(**changes),
                patch.object(Geocoder, "lookup") as lookup,
            ):
                self.assertEqual(
                    self.locate(external_processing="true").status_code, 400
                )
                lookup.assert_not_called()
                self.assertEqual(AnalysisRun.objects.count(), 0)
        self.locate()
        run = AnalysisRun.objects.get()
        for changes in (
            {"PANDECT_GEOCODER_LOCAL": False},
            {"PANDECT_GEOCODER_URL": "http://different.internal"},
        ):
            with (
                self.subTest(changes=changes),
                override_settings(**changes),
                patch.object(Geocoder, "lookup") as lookup,
            ):
                self.assertEqual(process_run(str(run.pk)), "execution_changed")
                lookup.assert_not_called()
                self.assertFalse(AnalysisResult.objects.filter(run=run).exists())
            AnalysisRun.objects.filter(pk=run.pk).update(status="queued")

    def test_roles_revocation_and_cross_case_input(self):
        self.assertEqual(self.locate(self.viewer).status_code, 403)
        self.assertEqual(self.locate(self.outsider).status_code, 404)
        CaseMembership.objects.filter(case=self.case, user=self.editor).delete()
        self.assertEqual(self.locate(self.editor).status_code, 404)
        self.assertEqual(AnalysisRun.objects.count(), 0)
        self.locate(
            self.owner,
            selections=str(LocationEvidence.objects.filter(case=self.other).get().pk),
        )
        self.assertEqual(
            {entry["evidence_id"] for entry in AnalysisRun.objects.get().inputs},
            {str(self.address.pk)},
        )
        run = AnalysisRun.objects.get()
        with patch.object(
            Geocoder, "lookup", side_effect=lambda **kw: self._revoke_during_lookup()
        ):
            CaseMembership.objects.create(
                case=self.case, user=self.editor, role="editor"
            )
            # The run's requesting actor is immutable, so queue an editor retry.
            AnalysisRun.objects.filter(pk=run.pk).update(status="failed")
            self.locate(self.editor, retry="1")
            editor_run = AnalysisRun.objects.latest("created_at")
            self.assertEqual(process_run(str(editor_run.pk)), "access_removed")
            self.assertFalse(AnalysisResult.objects.filter(run=editor_run).exists())

    def _revoke_during_lookup(self):
        CaseMembership.objects.filter(case=self.case, user=self.editor).delete()
        return [{"latitude": 1, "longitude": 2}]

    def test_viewers_can_read_estimates_and_current_reviews_are_respected(self):
        run = self.run_lookup(
            candidates=[
                {"latitude": 1, "longitude": 2},
                {"latitude": 3, "longitude": 4},
            ]
        )
        result = AnalysisResult.objects.get(run=run)
        AnalysisReview.objects.create(
            case=self.case,
            result=result,
            reviewed_by=self.owner,
            decision="accepted",
            item_index=None,
        )
        self.client.force_login(self.owner)
        before = self.client.get(self.url, HTTP_ACCEPT="application/json").json()
        self.assertEqual(
            {point["label"] for point in before["points"] if point.get("derived")},
            {"Unreviewed address estimate"},
        )
        for index, decision in ((0, "accepted"), (1, "rejected")):
            AnalysisReview.objects.create(
                case=self.case,
                result=result,
                reviewed_by=self.owner,
                item_index=index,
                decision=decision,
            )
        self.client.force_login(self.viewer)
        data = self.client.get(self.url, HTTP_ACCEPT="application/json").json()
        derived = [point for point in data["points"] if point.get("derived")]
        self.assertEqual(len(derived), 1)
        self.assertEqual(derived[0]["label"], "Reviewed address estimate")
        self.assertFalse(data["can_queue"])
        AnalysisReview.objects.create(
            case=self.case,
            result=result,
            reviewed_by=self.owner,
            item_index=0,
            decision="rejected",
        )
        self.assertFalse(
            any(
                point.get("derived")
                for point in self.client.get(
                    self.url, HTTP_ACCEPT="application/json"
                ).json()["points"]
            )
        )
        self.assertEqual(AnalysisReview.objects.count(), 4)

    def test_latest_item_reviews_are_fetched_together_for_the_page(self):
        first = self.run_lookup()
        LocationEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Second address observation",
            address="Another synthetic road",
        )
        second = self.run_lookup()
        for run, decision in ((first, "rejected"), (second, "accepted")):
            AnalysisReview.objects.create(
                case=self.case,
                result=run.result,
                reviewed_by=self.owner,
                item_index=0,
                decision=decision,
            )
        with CaptureQueriesContext(connection) as queries:
            data = self.client.get(self.url, HTTP_ACCEPT="application/json").json()
        self.assertEqual(
            sum(AnalysisReview._meta.db_table in query["sql"] for query in queries), 1
        )
        self.assertEqual(
            [point["label"] for point in data["points"] if point.get("derived")],
            ["Reviewed address estimate"],
        )

    def test_lookup_history_is_resolved_only_after_paginating_observations(self):
        self.run_lookup()
        for index in range(102):
            LocationEvidence.objects.create(
                case=self.case,
                uploaded_by=self.owner,
                title=f"Address observation {index}",
                address=f"{index} Synthetic road",
            )
        request = RequestFactory().get(self.url, {"q": "Address observation"})
        with CaptureQueriesContext(connection) as queries:
            page, *_ = map_page(request, self.case)
        self.assertEqual(page.paginator.count, 103)
        self.assertEqual(len(page), 100)
        self.assertFalse(
            any(AnalysisRun._meta.db_table in query["sql"] for query in queries)
        )
        with CaptureQueriesContext(connection) as queries:
            data = map_data(self.case, page)
        history_queries = [
            query["sql"]
            for query in queries
            if AnalysisRun._meta.db_table in query["sql"]
        ]
        self.assertEqual(len(history_queries), 1)
        history_sql = history_queries[0].replace("-", "")
        for row in page:
            self.assertIn(row["id"].hex, history_sql)
        self.assertNotIn(self.address.pk.hex, history_sql)
        self.assertEqual(data["points"], [])
        self.assertTrue(data["new_addresses"])

        request = RequestFactory().get(
            self.url, {"q": "Address observation", "page": 2}
        )
        page, *_ = map_page(request, self.case)
        data = map_data(self.case, page)
        self.assertEqual(len(page), 3)
        self.assertEqual(data["points"][0]["number"], 103)
        self.assertEqual(data["points"][0]["title"], self.address.title)

    def test_latest_eligible_local_lookup_takes_precedence(self):
        older = self.run_lookup()
        common = {
            "case": self.case,
            "requested_by": self.owner,
            "operation": older.operation,
            "method_version": older.method_version,
            "parameters": older.parameters,
            "inputs": older.inputs,
        }
        latest = AnalysisRun.objects.create(**common)
        AnalysisRun.objects.create(**common, external_processing=True)
        AnalysisRun.objects.create(
            **(common | {"parameters": older.parameters | {"reverse": True}})
        )
        AnalysisRun.objects.create(
            **(common | {"inputs": [older.inputs[0] | {"sha256": "0" * 64}]})
        )
        data = self.client.get(self.url, HTTP_ACCEPT="application/json").json()
        self.assertTrue(data["pending"])
        self.assertFalse(any(point.get("derived") for point in data["points"]))
        address = next(location for location in data["locations"] if location["url"])
        self.assertIn(str(latest.pk), address["url"])

    def test_no_match_failure_and_explicit_retry(self):
        self.locate()
        run = AnalysisRun.objects.get()
        with patch.object(Geocoder, "lookup", return_value=[]):
            process_run(str(run.pk))
            run.refresh_from_db()
            self.assertEqual(run.status, "complete")
        data = self.client.get(self.url, HTTP_ACCEPT="application/json").json()
        self.assertFalse(data["can_queue"])
        self.assertIn(
            "No address match found",
            [location["label"] for location in data["locations"]],
        )
        self.assertFalse(self.locate().json()["queued"])
        self.assertTrue(self.locate(retry="1").json()["queued"])
        self.assertEqual(AnalysisRun.objects.count(), 2)
        self.assertEqual(AnalysisResult.objects.filter(run=run).count(), 1)

    def test_address_batches_are_bounded_and_same_case_associations_enforced(self):
        for index in range(30):
            LocationEvidence.objects.create(
                case=self.case,
                uploaded_by=self.owner,
                title=f"Address {index}",
                address=f"{index} Synthetic road",
            )
        duplicate = LocationEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Same address",
            address="29 Synthetic road",
        )
        self.locate()
        run = AnalysisRun.objects.get()
        addresses = {
            LocationEvidence.objects.get(pk=entry["evidence_id"]).address
            for entry in run.inputs
        }
        self.assertEqual(len(addresses), 25)
        self.assertIn(str(duplicate.pk), {entry["evidence_id"] for entry in run.inputs})
        self.assertTrue(self.locate().json()["queued"])
        self.assertFalse(self.locate().json()["queued"])
        self.assertEqual(AnalysisRun.objects.count(), 2)

    def test_post_csrf_and_native_form_return_to_filtered_page(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        self.assertEqual(
            client.post(reverse("geo:map_locate", args=(self.case.pk,))).status_code,
            403,
        )
        self.client.force_login(self.owner)
        key = self.client.get(
            self.url + "?q=Synthetic&archive=all", HTTP_ACCEPT="application/json"
        ).json()["page_key"]
        response = self.client.post(
            reverse("geo:map_locate", args=(self.case.pk,))
            + "?q=Synthetic&archive=all",
            {"retry": "1", "page_key": key},
        )
        self.assertRedirects(
            response,
            self.url + "?q=Synthetic&archive=all",
            fetch_redirect_response=False,
        )

    def test_editor_can_locate_people_and_vehicles_but_not_foreign_associations(self):
        foreign_person = Person.objects.create(case=self.other, first_name="Foreign")
        foreign_vehicle = Vehicle.objects.create(case=self.other, make="Foreign")
        for kind, model, link, valid, foreign in (
            (
                "person-location",
                PersonLocationEvidence,
                "person",
                self.person,
                foreign_person,
            ),
            (
                "vehicle-location",
                VehicleLocationEvidence,
                "vehicle",
                self.vehicle,
                foreign_vehicle,
            ),
        ):
            with self.subTest(kind=kind):
                good = model.objects.create(
                    case=self.case,
                    uploaded_by=self.owner,
                    title="Valid address",
                    address="Synthetic linked address",
                    **{link: valid},
                )
                bad = model.objects.create(
                    case=self.case,
                    uploaded_by=self.owner,
                    title="Invalid linked address",
                    address="Foreign reference address",
                    **{link: valid},
                )
                model.objects.filter(pk=bad.pk).update(**{link: foreign})
                response = self.locate(self.editor, query=f"?kind={kind}")
                self.assertEqual(response.status_code, 200)
                run = AnalysisRun.objects.latest("created_at")
                self.assertEqual(
                    {entry["evidence_id"] for entry in run.inputs}, {str(good.pk)}
                )
                self.assertNotContains(
                    self.client.get(self.url), "Invalid linked address"
                )

    def test_unavailable_lookup_does_not_automatically_retry(self):
        self.locate()
        run = AnalysisRun.objects.get()
        with patch.object(
            Geocoder, "lookup", side_effect=GeocodingError("geocoder_unavailable")
        ) as lookup:
            process_run(str(run.pk))
            self.assertEqual(lookup.call_count, 1)
        run.refresh_from_db()
        self.assertEqual(run.status, "partial")
        data = self.client.get(self.url, HTTP_ACCEPT="application/json").json()
        self.assertFalse(data["can_queue"])
        self.assertFalse(data["pending"])
        self.assertIn(
            "Address lookup unavailable; try again later",
            [item["label"] for item in data["locations"]],
        )
        self.assertFalse(self.locate().json()["queued"])

    def test_external_or_wrong_case_results_are_not_adopted(self):
        run = self.run_lookup()
        AnalysisRun.objects.filter(pk=run.pk).update(external_processing=True)
        data = self.client.get(self.url, HTTP_ACCEPT="application/json").json()
        self.assertFalse(any(point.get("derived") for point in data["points"]))
        AnalysisRun.objects.filter(pk=run.pk).update(external_processing=False)
        AnalysisResult.objects.filter(run=run).update(case=self.other)
        data = self.client.get(self.url, HTTP_ACCEPT="application/json").json()
        self.assertFalse(any(point.get("derived") for point in data["points"]))

    def test_stale_page_cannot_geocode_a_different_set_of_sources(self):
        self.client.force_login(self.owner)
        before = self.client.get(self.url, HTTP_ACCEPT="application/json").json()
        LocationEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="New arrival",
            address="New synthetic address",
        )
        response = self.locate(page_key=before["page_key"])
        self.assertEqual(response.status_code, 409)
        self.assertEqual(AnalysisRun.objects.count(), 0)

    @override_settings(PANDECT_ANALYSIS_MAX_SOURCES=1)
    def test_operator_source_limit_is_respected(self):
        LocationEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Another address",
            address="Second synthetic road",
        )
        self.assertTrue(self.locate().json()["queued"])
        self.assertEqual(len(AnalysisRun.objects.get().inputs), 1)
