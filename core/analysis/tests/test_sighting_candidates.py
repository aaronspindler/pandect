"""Exact retained comparisons contribute only their directly supported links."""

from uuid import UUID

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from core.analysis.catalogue import project_result
from core.analysis.operations import get_operation
from core.analysis.result_validation import validate_output
from core.analysis.sightings import (
    build_sighting_group,
    candidate_links,
    prepare_proposal,
)
from core.analysis.source_resolution import resolve_snapshots
from core.analysis.source_selection import snapshot_sources
from core.analysis.sources import output, source_item
from core.models import AnalysisResult, AnalysisRun
from core.testing import EvidenceTestMixin
from geo.analysis.deterministic.locations import spatial_groups
from geo.models import LocationEvidence
from image.models import ImageEvidence


class SightingCandidatesTests(EvidenceTestMixin, TestCase):
    def publish(self, operation, choices, produce, *, parameters=None):
        inputs = snapshot_sources(self.case, choices)
        sources = resolve_snapshots(inputs, self.case)
        data = validate_output(produce(sources), sources)
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.owner,
            inputs=inputs,
            operation=operation,
            method_version=get_operation(operation).version,
            parameters=parameters or {},
            status=data["outcome"],
        )
        result = AnalysisResult.objects.create(case=self.case, run=run, data=data)
        project_result(result)
        return result

    def plate_result(self, readings, *, content=b"synthetic original"):
        image = self.create_evidence(
            ImageEvidence,
            file=SimpleUploadedFile("source.jpg", content, content_type="image/jpeg"),
        )
        regions = (
            [0.1, 0.1, 0.4, 0.4],
            [0.5, 0.5, 0.8, 0.8],
        )

        def producer(sources):
            source = sources[0]
            return output(
                "Plate readings",
                items=[
                    source_item(
                        source,
                        "Possible plate",
                        text=reading,
                        anchors=[source.anchor(region=regions[region - 1])],
                        values={
                            "Plate region number": region,
                            "Weak plate region": weak,
                        },
                    )
                    for region, reading, weak in readings
                ],
                outcome="partial",
            )

        result = self.publish(
            "plate_ocr", [{"kind": "image", "id": image.pk}], producer
        )
        return f"image:{image.pk}", result

    def test_ambiguous_plate_uses_exact_direct_keys_and_retains_other_readings(self):
        first, first_result = self.plate_result(
            [(1, "KB 89", False), (1, "K8 89", False)]
        )
        second, second_result = self.plate_result([(1, "kb89", False)])
        lookalike, _ = self.plate_result([(1, "K8 B9", False), (2, "KB89", True)])
        pairs = candidate_links(self.case, first)
        self.assertEqual(len(pairs), 1)
        pair = pairs[0]
        self.assertEqual(pair["basis"], "Shared identifier")
        self.assertEqual(pair["endpoints"][1]["selection"], second)
        self.assertEqual(pair["endpoints"][0]["alternatives"], ["K8 89"])
        self.assertIn("Matching checksums", pair["facts"]["Originals"])
        self.assertFalse(candidate_links(self.case, lookalike))
        choices, parameters = prepare_proposal(
            self.case, first, [pair["token"]], [], [lookalike]
        )
        self.assertEqual(len(choices), 3)
        self.assertEqual(
            parameters["finding_refs"],
            [
                {"result_id": str(first_result.pk), "item_index": 0},
                {"result_id": str(second_result.pk), "item_index": 0},
            ],
        )
        sources = resolve_snapshots(snapshot_sources(self.case, choices), self.case)
        result = validate_output(build_sighting_group(sources, parameters), sources)
        item = result["items"][0]
        self.assertEqual(item["values"]["links"], pairs)
        self.assertEqual(item["values"]["contrary"][0]["selection"], lookalike)
        self.assertEqual(len(item["anchors"]), 3)

    def test_distinct_anchor_regions_require_separate_proposals(self):
        first, _ = self.plate_result([(1, "KB89", False), (2, "BC78", False)])
        self.plate_result([(1, "KB89", False)])
        self.plate_result([(1, "BC78", False)])
        links = [pair["token"] for pair in candidate_links(self.case, first)]
        self.assertEqual(len(links), 2)
        with self.assertRaisesMessage(ValidationError, "one exact anchor region"):
            prepare_proposal(self.case, first, links, [], [])

    def test_spatial_pairs_preserve_producer_anchor_and_exclude_dependency_members(
        self,
    ):
        places = [
            self.create_evidence(
                LocationEvidence,
                id=UUID(int=index),
                latitude=1,
                longitude=longitude,
                title=title,
            )
            for index, (longitude, title) in enumerate(
                [
                    ("0", "Anchor"),
                    ("0.0007", "First place"),
                    ("-0.0007", "Other place"),
                ],
                1,
            )
        ]
        options = [{"kind": "location", "id": place.pk} for place in places]
        result = self.publish(
            "spatial_groups", options, lambda sources: spatial_groups(sources, {})
        )
        anchor, first, other = (f"location:{place.pk}" for place in places)
        self.assertEqual(len(candidate_links(self.case, anchor)), 2)
        pairs = candidate_links(self.case, first)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["endpoints"][1]["selection"], anchor)

        choices, parameters = prepare_proposal(
            self.case, first, [pairs[0]["token"]], [], []
        )
        self.assertEqual(len(choices), 3)
        self.assertEqual(
            parameters["finding_refs"], [{"result_id": str(result.pk), "item_index": 0}]
        )
        sources = resolve_snapshots(snapshot_sources(self.case, choices), self.case)
        item = build_sighting_group(sources, parameters)["items"][0]
        self.assertEqual(len(item["source_ids"]), 2)
        self.assertNotIn(other, item["values"]["member_identities"])
        self.assertEqual(item["values"]["links"][0]["basis"], "Place proximity")
