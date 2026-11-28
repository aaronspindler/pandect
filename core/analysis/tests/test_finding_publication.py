"""Only findings enter the catalogue, while run details remain intact."""

import json

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from audio.models import AudioEvidence
from core.analysis.catalogue import project_result
from core.analysis.navigation import finding_url
from core.analysis.operations import get_operation
from core.analysis.projection import finding_values
from core.analysis.source_selection import snapshot_sources
from core.models import (
    AnalysisFinding,
    AnalysisFindingSource,
    AnalysisResult,
    AnalysisRun,
)
from core.tests.testing import EvidenceViewTestMixin
from phone.models import TextMessageEvidence


class FindingPublicationTests(EvidenceViewTestMixin, TestCase):
    def publish(self, operation, kind, evidence, entries, *, legacy=False):
        inputs = snapshot_sources(self.case, [{"kind": kind, "id": evidence.pk}])
        source = inputs[0]
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.users["owner"],
            operation=operation,
            method_version=get_operation(operation).version,
            inputs=inputs,
            status="complete",
        )
        items = [
            {"source_ids": [source["id"]], "anchors": [], **entry} for entry in entries
        ]
        result = AnalysisResult.objects.create(
            case=self.case, run=run, data={"items": items}
        )
        if legacy:
            findings = AnalysisFinding.objects.bulk_create(
                [
                    AnalysisFinding(
                        case=self.case,
                        result=result,
                        item_index=index,
                        **finding_values(operation, item),
                    )
                    for index, item in enumerate(items)
                ]
            )
            AnalysisFindingSource.objects.bulk_create(
                [
                    AnalysisFindingSource(
                        case=self.case,
                        finding=finding,
                        source_id=source["id"],
                        kind=source["kind"],
                        evidence_id=source["evidence_id"],
                        source_sha256=source["sha256"],
                    )
                    for finding in findings
                ]
            )
        else:
            project_result(result)
        return result

    def test_publication_retains_supporting_output_without_creating_findings(self):
        note = self.text()
        message = TextMessageEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Sample message",
            body="Please confirm delivery.",
            sender=self.person,
            recipient=self.person,
        )
        recording = AudioEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Sample recording",
            file=SimpleUploadedFile("recording.wav", b"synthetic recording"),
        )
        audio_source = snapshot_sources(
            self.case, [{"kind": "audio", "id": recording.pk}]
        )[0]["id"]
        cases = (
            (
                "comm_patterns",
                "text-message",
                message,
                [
                    {"label": "Selected message", "text": message.body},
                    {"label": "Observed session 1", "values": {"records": 1}},
                ],
                [],
            ),
            (
                "style_profiles",
                "text",
                note,
                [{"label": "Capitalization", "values": {"uppercase_letters": 1}}],
                [],
            ),
            (
                "style_change",
                "text",
                note,
                [
                    {
                        "label": "Mean message words",
                        "values": {"difference_b_minus_a": 2},
                    },
                    {"label": "Possible observed-style change interval"},
                    {"label": "Group A coverage"},
                    {"label": "Recurring wording comparison"},
                    {"label": "Earlier retained observation"},
                ],
                [4],
            ),
            (
                "transcription",
                "audio",
                recording,
                [
                    {
                        "label": "Interval without transcribed text",
                        "values": {"Coverage": "Unresolved"},
                    },
                    {
                        "label": "Transcript segment candidate",
                        "text": "Please confirm delivery.",
                        "values": {
                            "Model uncertainty": "No low-confidence flag supplied"
                        },
                    },
                    {"label": "Transcript segment candidate", "text": "  "},
                    {
                        "label": "Transcript segment candidate",
                        "text": "Delivery is on Thursday.",
                        "values": {"Model uncertainty": "Review uncertain speech"},
                        "anchors": [
                            {
                                "source_id": audio_source,
                                "start_seconds": 1,
                                "end_seconds": 2,
                            }
                        ],
                    },
                ],
                [3],
            ),
            (
                "content_events",
                "text",
                note,
                [
                    {
                        "label": "Context",
                        "text": "An unclear reference.",
                        "report_only": True,
                    },
                    {"label": "Commitment", "text": "Delivery is on Thursday."},
                ],
                [1],
            ),
            (
                "conversation_link",
                "text-message",
                message,
                [
                    {"label": "correction", "text": "The delivery time was corrected."},
                    {
                        "label": "explicit_reference",
                        "text": "Both messages name invoice A-194.",
                    },
                ],
                [1],
            ),
        )
        for operation, kind, evidence, entries, expected in cases:
            with self.subTest(operation=operation):
                result = self.publish(operation, kind, evidence, entries)
                self.assertEqual(
                    list(
                        result.findings.order_by("item_index").values_list(
                            "item_index", flat=True
                        )
                    ),
                    expected,
                )
                result.refresh_from_db()
                self.assertEqual(
                    [item["label"] for item in result.data["items"]],
                    [entry["label"] for entry in entries],
                )

    def test_comparison_correction_stays_available_at_its_saved_link(self):
        evidence = self.text()
        result = self.publish(
            "conversation_link",
            "text",
            evidence,
            [{"label": "correction", "text": "The delivery date was corrected."}],
            legacy=True,
        )
        self.login("viewer")
        catalogue_url = reverse("core:analysis:browse", args=[self.case.pk])
        self.assertEqual(
            self.client.get(catalogue_url, {"history": "all"}).context["rows"], []
        )
        response = self.client.get(finding_url(self.case.pk, result.run_id, 0))
        self.assertEqual(
            response.context["items"][0]["text"], "The delivery date was corrected."
        )
        self.assertEqual(result.findings.count(), 1)

    def test_topic_reports_stay_out_of_catalogue_and_keep_exact_links(self):
        evidence = self.text()
        entries = [
            {
                "label": "harbour",
                "text": "Frequent observed word; a candidate content label.",
                "values": {"occurrences": 2},
            },
            {
                "label": "Topic",
                "text": "A summary of recurring mentions of the harbour.",
                "values": {"status": "suggestion"},
            },
        ]
        new = self.publish("text_topics", "text", evidence, entries)
        self.assertFalse(new.findings.exists())
        legacy = self.publish("text_topics", "text", evidence, entries, legacy=True)

        self.login("viewer")
        browse_url = reverse("core:analysis:browse", args=[self.case.pk])
        catalogue = self.client.get(browse_url)
        self.assertEqual(catalogue.context["rows"], [])
        keyword_search = self.client.get(browse_url, {"q": "harbour", "history": "all"})
        self.assertEqual(keyword_search.context["rows"], [])
        self.assertEqual(legacy.findings.count(), 2)

        for result in (new, legacy):
            for index in range(2):
                exact = self.client.get(finding_url(self.case.pk, result.run_id, index))
                self.assertEqual(
                    [item["index"] for item in exact.context["items"]], [index]
                )
                self.assertContains(exact, "harbour")
            download = self.client.get(
                reverse("core:analysis:download", args=[self.case.pk, result.run_id])
            )
            self.assertEqual(json.loads(download.content)["result"], result.data)
