"""Synthetic worst-case page: 25 independent runs with 500 file inputs each."""

import hashlib
from time import perf_counter
from unittest.mock import patch
from uuid import uuid4

from django.core.files.storage import FileSystemStorage
from django.db import connection
from django.http import QueryDict
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from core.analysis.catalogue import catalogue_page, project_result
from core.models import AnalysisResult, AnalysisRun
from core.testing import EvidenceTestMixin
from image.models import ImageEvidence


class CatalogueBoundsTests(EvidenceTestMixin, TestCase):
    def test_maximum_file_backed_page_has_bounded_queries_and_checks(self):
        checksum = hashlib.sha256(b"synthetic").hexdigest()
        (self.media_root / "catalogue-fixture.bin").write_bytes(b"synthetic")
        for _ in range(25):
            originals = ImageEvidence.objects.bulk_create(
                [
                    ImageEvidence(
                        case=self.case,
                        uploaded_by=self.owner,
                        title="Synthetic original",
                        file="catalogue-fixture.bin",
                        original_filename="catalogue-fixture.bin",
                        byte_size=9,
                        sha256=checksum,
                    )
                    for _ in range(500)
                ]
            )
            inputs = [
                {
                    "id": str(uuid4()),
                    "kind": "image",
                    "evidence_id": str(item.pk),
                    "sha256": checksum,
                    "attachment_id": None,
                    "result_id": None,
                }
                for item in originals
            ]
            run = AnalysisRun.objects.create(
                case=self.case,
                requested_by=self.owner,
                operation="image_metadata",
                method_version="1",
                status="complete",
                inputs=inputs,
            )
            result = AnalysisResult.objects.create(
                case=self.case,
                run=run,
                data={
                    "items": [
                        {"label": "Synthetic metadata", "source_ids": [inputs[0]["id"]]}
                    ],
                },
            )
            project_result(result)
        exists = FileSystemStorage.exists
        started = perf_counter()
        with (
            patch.object(
                FileSystemStorage, "exists", autospec=True, side_effect=exists
            ) as checks,
            CaptureQueriesContext(connection) as queries,
        ):
            page = catalogue_page(self.case, QueryDict())
        duration = perf_counter() - started
        self.assertEqual(len(page["rows"]), 25)
        self.assertTrue(all(not row["unavailable"] for row in page["rows"]))
        self.assertEqual(checks.call_count, 12_500)
        self.assertLessEqual(len(queries), 32)
        self.assertFalse(
            any('"core_analysisresult"."data"' in query["sql"] for query in queries)
        )
        print(
            f"Catalogue maximum page: {duration:.3f}s, {len(queries)} SQL queries, "
            f"{checks.call_count} local file checks"
        )
