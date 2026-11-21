"""Source and interpretation boundaries for comparing plans with location samples."""

from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from core.analysis.services import validate_output
from core.analysis.sources import Source
from geo.analysis.deterministic.journey_claims import compare_plans, validate_comparison
from geo.tests.test_analysis import CASE, PERSON, START, VEHICLE, source


class PlanComparisonTests(SimpleTestCase):
    def setUp(self):
        self.wording = "Meet me at 1 Main St, Toronto."
        body = f"🚐 {self.wording}\n{self.wording}"
        self.text = Source(
            "message",
            "text-message",
            SimpleNamespace(
                body=body,
                case_id=CASE,
                sender_id=PERSON,
            ),
        )
        self.passage = {
            "source_id": self.text.id,
            "start": body.rindex(self.wording),
            "end": len(body),
        }
        self.claim = self.passage | {
            "status": "proposed",
            "subjects": [f"person:{PERSON}"],
            "time_start": "2026-10-01T06:00:00-04:00",
            "time_basis": "explicit",
            "address": "1 Main Street, Toronto",
            "place_resolved": True,
        }

    def compare(self, samples, claims=None, *, extra_sources=(), **parameters):
        sources = [self.text, *extra_sources, *samples]
        result = compare_plans(
            sources,
            {
                "claims": claims if claims is not None else [self.claim],
                "observations": [sample.id for sample in samples],
                **parameters,
            },
        )
        return validate_output(result, sources)

    def test_comparison_opens_second_unicode_quote_and_exact_observation(self):
        sample = source(1)
        result = self.compare([sample])
        self.assertEqual(result["outcome"], "complete")
        (item,) = result["items"]
        self.assertEqual(item["label"], "Supporting observation")
        self.assertEqual(item["anchors"], [self.passage, sample.anchor()])
        self.assertEqual(item["source_ids"], [self.text.id, sample.id])
        self.assertEqual(item["values"]["Claim passage"], self.wording)
        self.assertEqual(item["values"]["Claim status"], "Proposed")
        self.assertEqual(item["values"]["Window start"], self.claim["time_start"])
        self.assertEqual(item["values"]["Observed at"], START.isoformat())

    def test_only_linked_cancellation_qualifies_earlier_plan(self):
        source_text = "Call off the meeting."
        cancel = Source(
            "cancel", "text", SimpleNamespace(body=source_text, case_id=CASE)
        )
        span = {"source_id": cancel.id, "start": 0, "end": len(source_text)}
        cancellation = span | {"status": "cancelled", "applies_to": self.passage}
        sample = source(1)
        result = self.compare(
            [sample], [self.claim, cancellation], extra_sources=[cancel]
        )
        plan, change, sighting = result["items"]
        self.assertEqual(plan["anchors"], [self.passage, span, sample.anchor()])
        self.assertEqual(change["label"], "Contrary evidence")
        self.assertEqual(change["anchors"], [span, self.passage])
        self.assertNotIn(sample.id, change["source_ids"])
        self.assertTrue(plan["report_only"] and sighting["report_only"])
        self.assertEqual(sighting["values"]["Claim status"], "Cancelled")
        standalone = self.compare(
            [sample],
            [self.claim, span | {"status": "cancelled"}],
            extra_sources=[cancel],
        )
        self.assertEqual(standalone["items"][0]["label"], "Supporting observation")

    def test_different_subject_vehicle_and_unlinked_observations_stay_contextual(self):
        for kind, values in (
            ("vehicle-location", {}),
            ("location", {}),
            ("person-location", {"person_id": VEHICLE}),
        ):
            with self.subTest(kind=kind):
                result = self.compare([source(1, kind=kind, **values)])
                (item,) = result["items"]
                self.assertEqual(item["label"], "Unresolved")
                self.assertTrue(item["report_only"])
        # Vehicle compatibility requires a vehicle explicitly stated by the claim.
        (item,) = self.compare(
            [source(1, kind="vehicle-location")],
            [self.claim | {"subjects": [f"vehicle:{VEHICLE}"]}],
        )["items"]
        self.assertEqual(item["label"], "Supporting observation")
        self.assertEqual(item["values"]["sender_id"], str(PERSON))

    def test_mismatching_places_only_conflict_for_a_resolved_instant(self):
        distant = source(1, address="22 Main St, Toronto")
        self.assertEqual(
            self.compare([distant])["items"][0]["label"], "Contrary evidence"
        )
        window = self.claim | {"time_end": "2026-10-01T10:05:00+00:00"}
        self.assertEqual(
            self.compare([distant], [window])["items"][0]["label"], "Unresolved"
        )
        for sample, claim in (
            (
                source(1, address="Central Station"),
                self.claim | {"address": "Central Station"},
            ),
            (source(1), self.claim | {"place_resolved": False}),
            (source(1, address="22 Main St, Springfield"), self.claim),
            (
                source(1),
                self.claim | {"latitude": 45, "longitude": -79, "radius_m": 100},
            ),
        ):
            with self.subTest(address=sample.item.address, claim=claim):
                self.assertEqual(
                    self.compare([sample], [claim])["items"][0]["label"], "Unresolved"
                )

    def test_claimed_coordinates_and_radius_are_retained_with_position_origin(self):
        addressless = source(1, address="", point=(43.5, -79.5))
        claim = self.claim | {
            "latitude": 43.5,
            "longitude": -79.5,
            "radius_m": 80,
            "address": "",
        }
        (item,) = self.compare([addressless], [claim])["items"]
        self.assertEqual(item["label"], "Supporting observation")
        self.assertEqual(item["values"]["Comparison radius (m)"], 80)
        self.assertEqual(item["values"]["Position origin"], "Reported coordinates")

    def test_relative_dates_need_a_reference_and_competing_readings_remain_open(self):
        sample = source(1)
        reference = Source(
            "reference",
            "text",
            SimpleNamespace(case_id=CASE, body="October 1, 2026 at 06:00 EDT"),
        )
        citation = {"source_id": reference.id, "start": 0, "end": len(reference.text)}
        relative = self.claim | {"time_basis": "anchored_relative"}
        with self.assertRaises(ValidationError):
            self.compare([sample], [relative])
        relative["reference"] = citation
        (item,) = self.compare([sample], [relative], extra_sources=[reference])["items"]
        self.assertEqual(item["anchors"], [self.passage, citation, sample.anchor()])
        self.assertEqual(item["label"], "Supporting observation")
        wrong_zone = self.claim | {"time_start": "2026-10-01T06:00:00+01:00"}
        readings = self.compare(
            [sample], [relative, wrong_zone], extra_sources=[reference]
        )["items"]
        self.assertEqual([i["label"] for i in readings], ["Unresolved"] * 2)
        self.assertNotEqual(
            readings[0]["values"]["Window start"], readings[1]["values"]["Window start"]
        )
        self.assertIn("competing interpretations", readings[0]["text"])

    def test_trips_only_contribute_individual_samples_and_no_implied_visit(self):
        samples = [source(1, minute=-15), source(2, minute=15), source(3, minute=None)]
        group = {"origin": "finding", "members": [0, 1], "label": "Candidate journey"}
        result = self.compare(samples, groups=[group])
        self.assertEqual(result["outcome"], "insufficient")
        self.assertEqual([i["group_indices"] for i in result["items"]], [[0], [0], []])
        for index, item in enumerate(result["items"]):
            self.assertTrue(item["report_only"])
            self.assertEqual(item["source_ids"], [self.text.id, samples[index].id])
            self.assertEqual(item["label"], "Unresolved")

    def test_unchecked_ancestor_input_cannot_contribute_and_unusable_roles_reject(self):
        sample = source(1)
        ancestor = source(2)
        result = self.compare([sample], extra_sources=[ancestor])
        self.assertEqual(len(result["items"]), 1)
        self.assertNotIn(ancestor.id, result["items"][0]["source_ids"])
        for claim in (
            self.claim | {"start": True},
            self.claim | {"source_id": sample.id},
            self.claim | {"time_start": "2026-10-01T06:00"},
            self.claim | {"time_end": "2026-09-30T06:00:00Z"},
            self.claim | {"latitude": 91, "longitude": 0},
        ):
            with self.subTest(claim=claim), self.assertRaises(ValidationError):
                self.compare([sample], [claim])
        with self.assertRaisesMessage(ValidationError, "1,000"):
            self.compare([sample, ancestor], [self.claim] * 501)
        with self.assertRaises(ValidationError):
            self.compare([sample, replace(sample, id="alternate")])
        with self.assertRaises(ValidationError):
            self.compare([sample], groups=[{"origin": "trip", "members": [1]}])

    def test_repeated_source_content_is_bounded_before_queueing(self):
        # The selections fit both the source and pair limits. JSON still has
        # to represent the long, Unicode address on each sampled observation.
        samples = [source(i, address="🚐" * 20000) for i in range(20)]
        with self.assertRaisesMessage(ValidationError, "4 MiB"):
            validate_comparison(
                [self.text, *samples],
                {"claims": [self.claim], "observations": [s.id for s in samples]},
                check_size=True,
            )

    def test_distinct_producers_of_one_reading_do_not_create_a_conflict(self):
        sample = source(1)
        with self.assertRaisesMessage(ValidationError, "only once"):
            self.compare([sample], [self.claim, self.claim])
        first = self.claim | {"result_id": "first"}
        second = self.claim | {"result_id": "second"}
        items = self.compare([sample], [first, second])["items"]
        self.assertEqual(
            [row["label"] for row in items], ["Supporting observation"] * 2
        )

    def test_three_joint_subjects_compare_independently_and_alternatives_stay_open(
        self,
    ):
        other_person = UUID(int=804)
        third_person = UUID(int=805)
        members = [f"person:{PERSON}", f"person:{other_person}", f"vehicle:{VEHICLE}"]
        joint = self.claim | {"subjects": members}
        samples = [
            source(1),
            source(2, person_id=other_person, address="9 Main St, Toronto"),
            source(3, kind="vehicle-location"),
            source(4, person_id=third_person),
        ]
        items = self.compare(samples, [joint])["items"]
        self.assertEqual(
            [item["label"] for item in items],
            [
                "Supporting observation",
                "Contrary evidence",
                "Supporting observation",
                "Unresolved",
            ],
        )
        for index, item in enumerate(items):
            self.assertEqual(item["values"]["Claimed subjects"], sorted(members))
            self.assertEqual(item["source_ids"], [self.text.id, samples[index].id])
        alternative_group = joint | {"subjects": members[:-1]}
        alternative_time = joint | {"time_start": "2026-10-01T10:00:00-04:00"}
        readings = self.compare(samples, [joint, alternative_group, alternative_time])[
            "items"
        ]
        self.assertEqual({row["claim_index"] for row in readings}, {0, 1, 2})
        self.assertTrue(
            all(row["label"] == "Unresolved" and row["report_only"] for row in readings)
        )

    def test_joint_subjects_are_unordered_and_require_unambiguous_fields(
        self,
    ):
        fields = self.claim
        group = [f"person:{PERSON}", f"vehicle:{VEHICLE}"]
        readings = self.compare(
            [source(1)],
            [
                fields | {"subjects": group, "result_id": "first"},
                fields | {"subjects": group[::-1], "result_id": "second"},
            ],
        )["items"]
        self.assertEqual(
            [row["label"] for row in readings], ["Supporting observation"] * 2
        )
        with self.assertRaisesMessage(ValidationError, "only once"):
            self.compare(
                [source(1)],
                [fields | {"subjects": group}, fields | {"subjects": group[::-1]}],
            )
        for bad in (
            fields | {"subjects": group + group},
            fields | {"subjects": group[0]},
            fields | {"subject": group[0]},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                self.compare([source(1)], [bad])
