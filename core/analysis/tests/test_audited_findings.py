"""A catalogue visibility decision preserves the saved result and its sources."""

import json
from urllib.parse import urlencode

from django.test import TestCase
from django.urls import reverse

from core.analysis.catalogue import project_result
from core.analysis.navigation import finding_url
from core.analysis.operations import get_operation
from core.analysis.services import snapshot_sources
from core.models import AnalysisResult, AnalysisRun
from core.testing import EvidenceViewTestMixin
from text.models import TextEvidence


class AuditedFindingTests(EvidenceViewTestMixin, TestCase):
    def test_hidden_finding_remains_in_saved_result_and_download(self):
        quotes = ("I will bring the invoice.", "I will send the delivery receipt.")
        evidence = TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic delivery correspondence",
            body=" ".join(quotes),
        )
        inputs = snapshot_sources(self.case, [{"kind": "text", "id": evidence.pk}])
        source_id = inputs[0]["id"]
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.users["owner"],
            operation="content_events",
            method_version=get_operation("content_events").version,
            inputs=inputs,
            status="complete",
        )
        statements = (
            "The writer said they would bring the invoice.",
            "The writer said they would send the delivery receipt.",
        )
        items = [
            {
                "label": "commitment",
                "text": statement,
                "source_ids": [source_id],
                "anchors": [
                    {
                        "source_id": source_id,
                        "start": evidence.body.index(quote),
                        "end": evidence.body.index(quote) + len(quote),
                    }
                ],
                "values": {"status": "reported commitment"},
            }
            for statement, quote in zip(statements, quotes, strict=True)
        ]
        result = AnalysisResult.objects.create(
            case=self.case, run=run, data={"items": items}
        )
        project_result(result)
        self.assertEqual(
            result.findings.filter(item_index=0).update(hidden_from_catalogue=True),
            1,
        )

        self.login("viewer")
        catalogue_url = reverse("core:analysis:browse", args=[self.case.pk])
        for query in ({}, {"history": "all"}):
            with self.subTest(query=query):
                catalogue = self.client.get(catalogue_url, query)
                self.assertEqual(
                    [row["url"] for row in catalogue.context["rows"]],
                    [
                        finding_url(
                            self.case.pk,
                            run.pk,
                            1,
                            history={"return_findings": urlencode(query)},
                        )
                    ],
                )

        case_home = self.client.get(reverse("core:analysis:index", args=[self.case.pk]))
        reader = self.client.get(self.url("detail", evidence))
        home_findings = [
            row
            for row in case_home.context["latest_records"]
            if row["record_type"] == "Finding"
        ]
        for rows in (home_findings, reader.context["findings_page"]):
            self.assertEqual([row["summary_text"] for row in rows], [statements[1]])

        saved_link = finding_url(self.case.pk, run.pk, 0)
        detail = self.client.get(saved_link)
        self.assertEqual([item["index"] for item in detail.context["items"]], [0])
        self.assertContains(detail, statements[0])
        self.assertContains(detail, quotes[0])
        download_url = reverse("core:analysis:download", args=[self.case.pk, run.pk])
        download = self.client.get(download_url)
        self.assertEqual(json.loads(download.content)["result"]["items"], items)

        self.login("outsider")
        self.assertEqual(self.client.get(saved_link).status_code, 404)
        self.assertEqual(self.client.get(download_url).status_code, 404)
