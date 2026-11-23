"""Retain searchable local vectors across the embedding-width migration."""

from django.contrib.auth import get_user_model
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

from cases.models import Case
from core.case_search.tests.test_semantic import UNIT
from text.models import TextEvidence


class EmbeddingMigrationTests(TransactionTestCase):
    def test_populated_local_vector_keeps_profile_and_offsets(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        before = [("core", "0014_search_embedding_usage")]
        try:
            executor.migrate(before)
            old = executor.loader.project_state(before).apps
            user = get_user_model().objects.create_user(username="migration-fixture")
            case = Case.objects.create(owner=user)
            text = TextEvidence.objects.create(
                case=case, uploaded_by=user, title="Note", body="Fixture"
            )
            doc = old.get_model("core", "SearchDocument").objects.create(
                case_id=case.pk,
                text_id=text.pk,
                profile="a" * 64,
                offset=7,
                complete=True,
            )
            previous = old.get_model("core", "SearchPassage").objects.create(
                case_id=case.pk,
                document=doc,
                start=0,
                length=7,
                digest=text.sha256,
                embedding=UNIT,
            )
            after = [("core", "0015_search_embedding_dimensions")]
            executor = MigrationExecutor(connection)
            executor.migrate(after)
            migrated = executor.loader.project_state(after).apps
            row = migrated.get_model("core", "SearchPassage").objects.get(
                pk=previous.pk
            )
            self.assertEqual(
                (row.profile, row.start, row.length, row.digest),
                (doc.profile, 0, 7, text.sha256),
            )
            self.assertEqual(list(row.embedding), UNIT)
            self.assertTrue(row.document.complete)
            self.assertEqual(row.document.offset, 7)
        finally:
            MigrationExecutor(connection).migrate(latest)
