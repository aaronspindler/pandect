"""Exercise the populated additive migration graph, including empty runs."""

import hashlib
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class CatalogueMigrationTests(TransactionTestCase):
    def test_populated_pre_catalogue_database_migrates_without_source_access(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        before = [("core", "0002_caseanalysisbatch"), ("cases", "0003_case_ai_backend")]
        try:
            executor.migrate(before)
            old = executor.loader.project_state(before).apps
            Run = old.get_model("core", "AnalysisRun")
            Result = old.get_model("core", "AnalysisResult")
            user = get_user_model().objects.create_user(username="migration-fixture")
            case = old.get_model("cases", "Case").objects.create(
                owner_id=user.pk, name="Synthetic migration"
            )
            source = {
                "id": str(uuid4()),
                "kind": "document",
                "evidence_id": str(uuid4()),
                "sha256": hashlib.sha256(b"retained").hexdigest(),
            }
            populated = Run.objects.create(
                case_id=case.pk,
                requested_by_id=user.pk,
                operation="entities",
                method_version="1",
                status="complete",
                inputs=[source],
            )
            data = {
                "items": [
                    {
                        "label": "Email",
                        "text": "source@example.test",
                        "source_ids": [source["id"]],
                        "values": {"raw": "source@example.test"},
                    }
                ]
            }
            result = Result.objects.create(case_id=case.pk, run=populated, data=data)
            empty = Run.objects.create(
                case_id=case.pk,
                requested_by_id=user.pk,
                operation="entities",
                method_version="1",
                status="empty",
                inputs=[source],
            )
            Result.objects.create(case_id=case.pk, run=empty, data={"items": []})
            failed = Run.objects.create(
                case_id=case.pk,
                requested_by_id=user.pk,
                operation="entities",
                method_version="1",
                status="failed",
                inputs=[source],
            )
            after = [("core", "0004_backfill_finding_catalogue")]
            with (
                patch(
                    "django.core.files.storage.FileSystemStorage.open",
                    side_effect=AssertionError("No source reads"),
                ),
                patch(
                    "django.core.files.storage.FileSystemStorage.exists",
                    side_effect=AssertionError("No source checks"),
                ),
            ):
                executor = MigrationExecutor(connection)
                executor.migrate(after)
            migrated = executor.loader.project_state(after).apps
            Finding = migrated.get_model("core", "AnalysisFinding")
            Source = migrated.get_model("core", "AnalysisFindingSource")
            NewRun = migrated.get_model("core", "AnalysisRun")
            self.assertEqual(Finding.objects.filter(result_id=result.pk).count(), 1)
            self.assertEqual(Source.objects.get().source_sha256, source["sha256"])
            self.assertFalse(Finding.objects.exclude(result_id=result.pk).exists())
            self.assertTrue(
                all(
                    NewRun.objects.get(pk=run.pk).equivalence_key
                    for run in (populated, empty, failed)
                )
            )
            self.assertEqual(
                migrated.get_model("core", "AnalysisResult")
                .objects.get(pk=result.pk)
                .data,
                data,
            )
        finally:
            MigrationExecutor(connection).migrate(latest)
