"""A selected prior interpretation is pinned to its exact sources and members."""

from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from core.analysis.catalogue import project_result
from core.analysis.errors import AnalysisError
from core.analysis.services import start_run
from core.analysis.source_resolution import SourceResolutionCache, resolve_snapshots
from core.analysis.source_selection import snapshot_sources
from core.analysis.sources import source_id
from core.analysis.worker import process_run
from core.evidence.deletion import plan_deletion
from core.models import AnalysisResult, AnalysisRun
from core.testing import EvidenceViewTestMixin
from geo.journey_claim_inputs import derived_page, expand_input, validate_dependencies
from geo.models import VehicleLocationEvidence
from geo.trip_jobs import process_trips
from geo.trip_models import VehicleTripSource
from geo.trip_processing import (
    invalidate_vehicle_trips,
    prepare_case_trips,
    saved_trips,
)
from phone.models import TextMessageEvidence


class ComparisonInputTests(EvidenceViewTestMixin, TestCase):
    def locations(self, total):
        return [
            VehicleLocationEvidence.objects.create(
                case=self.case,
                vehicle=self.vehicle,
                uploaded_by=self.users["owner"],
                title=f"Journey sample {i}",
                address=f"{i + 1} Moving Street",
                observed_at=datetime(2026, 10, 1, tzinfo=UTC) + timedelta(minutes=i),
            )
            for i in range(total)
        ]

    def source_inputs(self, kind, records):
        return snapshot_sources(
            self.case, [{"kind": kind, "id": str(item.pk)} for item in records]
        )

    def result(self, operation, inputs, items):
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.users["owner"],
            operation=operation,
            method_version="1",
            status="complete",
            inputs=inputs,
        )
        result = AnalysisResult.objects.create(
            case=self.case, run=run, data={"items": items}
        )
        project_result(result)
        return result

    def comparison(self, expanded):
        inputs = snapshot_sources(self.case, expanded["selections"])
        group = expanded["group"]
        members = group["members"] if group else []
        return AnalysisRun(
            case=self.case,
            requested_by=self.users["owner"],
            operation="journey_claims",
            method_version="1",
            inputs=inputs,
            parameters={
                "claims": [expanded["claim"]] if expanded["claim"] else [],
                "observations": members,
                "groups": [group | {"members": list(range(len(members)))}]
                if group
                else [],
                "finding_refs": [expanded["finding_ref"]]
                if expanded["finding_ref"]
                else [],
            },
        )

    def check(self, run):
        cache = SourceResolutionCache(self.case, with_content=False)
        sources = resolve_snapshots(
            run.inputs, self.case, require_current_review=False, cache=cache
        )
        validate_dependencies(run, sources, self.case, cache=cache)

    def test_claim_preserves_multiple_spans_and_inactive_ancestor_source(self):
        records = [self.text(), self.text(), self.text()]
        inputs = self.source_inputs("text", records)
        members = [entry["id"] for entry in inputs[:2]]
        anchors = [{"source_id": member, "start": 2, "end": 7} for member in members]
        prior = self.result(
            "content_events",
            inputs,
            [
                {
                    "label": "cancellation",
                    "text": "The earlier appointment was cancelled.",
                    "source_ids": members,
                    "anchors": anchors,
                    "values": {"status": "cancelled"},
                },
                {
                    "label": "question_answer",
                    "text": "A different question.",
                    "source_ids": members,
                    "anchors": anchors,
                },
            ],
        )
        token = f"finding:{prior.pk}:0"
        self.assertEqual([key for key, _ in derived_page(self.case, "claim")], [token])
        self.assertFalse(derived_page(self.other_case, "claim").object_list)
        for invalid_case, invalid_token, role in (
            (self.other_case, token, "claim"),
            (self.case, token, "movement"),
            (self.case, f"finding:{prior.pk}:1", "claim"),
        ):
            with self.assertRaises(AnalysisError):
                expand_input(invalid_case, invalid_token, role)
        expanded = expand_input(self.case, token, "claim")
        self.assertEqual(len(expanded["selections"]), 3)
        self.assertEqual(expanded["claim"]["status"], "cancelled")
        self.assertEqual(expanded["claim"]["citations"], anchors[1:])
        run = self.comparison(expanded)
        self.check(run)
        run.parameters["claims"][0]["start"] += 1
        with self.assertRaisesMessage(AnalysisError, "source_changed"):
            self.check(run)
        run.parameters["claims"][0]["start"] -= 1
        run.inputs = run.inputs[:2]
        with self.assertRaisesMessage(AnalysisError, "source_changed"):
            self.check(run)

    def test_movement_findings_compare_only_members_and_reject_aggregate(self):
        rows = self.locations(3)
        inputs = self.source_inputs("vehicle-location", rows)
        member_ids = [entry["id"] for entry in inputs[:2]]
        for method, label, aggregate in (
            (
                "candidate_visits",
                "Candidate visit to place 3",
                "Place 3 visit frequency",
            ),
            ("candidate_trips", "Candidate journey", "Excluded sampling gap"),
        ):
            with self.subTest(method=method):
                prior = self.result(
                    method,
                    inputs,
                    [
                        {"label": label, "source_ids": member_ids},
                        {"label": aggregate, "source_ids": member_ids},
                    ],
                )
                token = f"finding:{prior.pk}:0"
                self.assertIn(token, dict(derived_page(self.case, "movement")))
                expanded = expand_input(self.case, token, "movement")
                self.assertEqual(expanded["group"]["members"], member_ids)
                self.assertEqual(len(expanded["selections"]), 3)
                run = self.comparison(expanded)
                self.check(run)
                run.parameters["groups"][0]["members"].reverse()
                with self.assertRaisesMessage(AnalysisError, "source_changed"):
                    self.check(run)
                with self.assertRaises(AnalysisError):
                    expand_input(self.case, f"finding:{prior.pk}:1", "movement")

    def test_trip_membership_survives_replacement_without_using_new_generation(self):
        def publish(generation):
            for _ in range(5):
                if (
                    process_trips(str(generation.pk), str(generation.token))
                    == "complete"
                ):
                    return
                generation.refresh_from_db()
            self.fail("A replacement generation did not finish.")

        previous_rows = self.locations(3)
        untimed = VehicleLocationEvidence.objects.create(
            case=self.case,
            vehicle=self.vehicle,
            uploaded_by=self.users["owner"],
            title="Unrelated untimed sample",
            address="500 Station Road",
        )
        generation = prepare_case_trips(self.users["owner"], self.case.pk)[0]
        publish(generation)
        trip = saved_trips(self.case).get()
        token = f"trip:{trip.pk}"
        self.assertIn(token, dict(derived_page(self.case, "movement")))
        with self.assertRaises(AnalysisError):
            expand_input(self.other_case, token, "movement")
        with (
            self.settings(PANDECT_ANALYSIS_MAX_SOURCES=2),
            self.assertRaisesMessage(ValidationError, "individual observations"),
        ):
            expand_input(self.case, token, "movement")
        expanded = expand_input(self.case, token, "movement")
        text = self.text()
        self.login("owner")
        create = reverse("core:analysis:create", args=(self.case.pk,))
        claim_token = f"text:{text.pk}"
        prepared = self.client.post(
            create,
            {
                "operation": "journey_claims",
                "action": "prepare",
                "claim": claim_token,
                "movement": token,
            },
        )
        self.assertContains(prepared, "Interpret a passage")
        choice = {"kind": "text", "id": str(text.pk)}
        parameters = self.comparison(expanded).parameters
        parameters["claims"] = [
            {"source_id": source_id(choice), "start": 0, "end": 8, "status": "proposed"}
        ]
        run, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "journey_claims",
            [choice, *expanded["selections"]],
            parameters=parameters,
        )
        process_run(run.pk)
        self.login("viewer")
        url = reverse("geo:plan_group", args=(self.case.pk, run.pk, 0))
        trip_url = reverse("geo:trip_detail", args=(self.case.pk, trip.pk))
        self.assertContains(self.client.get(url), trip_url)
        filters = {"q": "Trip note", "state": "all", "type": "text"}
        result = self.client.get(
            reverse("core:analysis:detail", args=(self.case.pk, run.pk)),
            {
                "return_evidence": urlencode(
                    {"kind": "text", "evidence": text.pk, "reader": "1"} | filters
                )
            },
        )
        group_link = next(
            link
            for _, link in result.context["items"][0]["comparison_links"]
            if link.startswith(url)
        )
        group = self.client.get(group_link)
        self.assertContains(group, trip_url)
        self.assertTrue(group.context["back_url"].endswith("#finding-1"))
        self.assertEqual(
            self.client.get(group.context["back_url"]).context["back_to_analysis"],
            reverse(
                "core:evidence:detail",
                args=(self.case.pk, "text", text.pk),
                query=filters,
                fragment="evidence-reader",
            ),
        )
        self.assertNotIn(
            str(untimed.pk), {entry["evidence_id"] for entry in run.inputs}
        )
        inventory = plan_deletion(untimed, "vehicle-location")
        self.assertFalse(inventory.blocker)
        self.assertIn(run, inventory.collector.data[AnalysisRun])
        new_row = VehicleLocationEvidence.objects.create(
            case=self.case,
            vehicle=self.vehicle,
            uploaded_by=self.users["owner"],
            title="Later sample",
            address="4 Moving Street",
            observed_at=previous_rows[-1].observed_at + timedelta(minutes=1),
        )
        replacement = invalidate_vehicle_trips(
            self.users["owner"], self.case, [self.vehicle.pk]
        )[0]
        self.assertFalse(saved_trips(self.case).exists())
        publish(replacement)
        current_trip = saved_trips(self.case).get()
        self.assertNotEqual(current_trip.pk, trip.pk)
        self.check(run)
        response = self.client.get(url)
        self.assertNotContains(response, trip_url)
        self.assertNotContains(response, str(current_trip.pk))
        self.assertNotContains(response, str(new_row.pk))
        self.login("owner")
        restarted = self.client.post(
            create,
            {
                "operation": "journey_claims",
                "action": "choose_again",
                "draft": prepared.context["draft"],
            },
        )
        self.assertContains(
            restarted, "Unavailable sources were removed from the selection."
        )
        self.assertContains(restarted, f'value="{claim_token}" checked')
        self.assertNotContains(restarted, f'value="{token}"')
        self.assertContains(restarted, f'value="trip:{current_trip.pk}"')
        self.assertNotContains(restarted, f'value="trip:{current_trip.pk}" checked')
        for member in expanded["selections"]:
            self.assertContains(
                response,
                reverse(
                    "core:evidence:detail",
                    args=(self.case.pk, member["kind"], member["id"]),
                ),
            )
        with self.assertRaises(AnalysisError):
            expand_input(self.case, token, "movement")
        group = run.parameters["groups"][0]
        group["revision"] += 1
        with self.assertRaisesMessage(AnalysisError, "source_changed"):
            self.check(run)
        group["revision"] -= 1
        VehicleTripSource.objects.filter(trip=trip).order_by("ordinal").first().delete()
        with self.assertRaisesMessage(AnalysisError, "source_changed"):
            self.check(run)

    def test_derived_claim_and_journey_publish_through_worker(self):
        source = TextMessageEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            sender=self.person,
            recipient=self.person,
            title="Synthetic dispatch",
            body="Please send the van to 1 Moving Street on October 1 at 00:00 UTC.",
        )
        inputs = self.source_inputs("text-message", [source])
        key = inputs[0]["id"]
        finding = self.result(
            "content_events",
            inputs,
            [
                {
                    "label": "plan",
                    "text": "A journey was proposed.",
                    "source_ids": [key],
                    "anchors": [
                        {"source_id": key, "start": 0, "end": len(source.body)}
                    ],
                    "values": {"status": "proposed"},
                }
            ],
        )
        claim = expand_input(self.case, f"finding:{finding.pk}:0", "claim")
        observations = self.locations(2)
        location_inputs = self.source_inputs("vehicle-location", observations)
        members = [row["id"] for row in location_inputs]
        previous = self.result(
            "candidate_trips",
            location_inputs,
            [
                {
                    "label": "Candidate journey",
                    "source_ids": members,
                    "anchors": [{"source_id": member} for member in members],
                }
            ],
        )
        group = expand_input(self.case, f"finding:{previous.pk}:0", "movement")
        interpretation = claim["claim"] | {
            "subjects": [f"vehicle:{self.vehicle.pk}"],
            "time_basis": "explicit",
            "time_start": observations[0].observed_at.isoformat(),
            "address": observations[0].address,
            "place_resolved": True,
        }
        run, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "journey_claims",
            claim["selections"] + group["selections"],
            parameters={
                "claims": [interpretation],
                "observations": members,
                "groups": [group["group"] | {"members": [0, 1]}],
                "finding_refs": [claim["finding_ref"], group["finding_ref"]],
            },
        )
        process_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "partial", run.error_code)
        self.assertEqual(
            [(item["label"], item["source_ids"]) for item in run.result.data["items"]],
            [
                ("Supporting observation", [key, members[0]]),
                ("Unresolved", [key, members[1]]),
            ],
        )
        token = f"finding:{finding.pk}:0"
        self.assertIn(token, dict(derived_page(self.case, "claim")))
        TextMessageEvidence.objects.filter(pk=source.pk).update(
            body=source.body.replace("1 Moving Street", "2 Moving Street")
        )
        self.assertNotIn(token, dict(derived_page(self.case, "claim")))
        with self.assertRaisesMessage(AnalysisError, "source_changed"):
            expand_input(self.case, token, "claim")
