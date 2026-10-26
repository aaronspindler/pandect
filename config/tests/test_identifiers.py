from uuid import UUID

from django.apps import apps
from django.db import connection, models
from django.test import SimpleTestCase, TestCase

PANDECT_APPS = ("accounts", "cases", "evidence", "people", "vehicles")


class IdentifierDefaultTests(SimpleTestCase):
    def test_application_models_generate_distinct_uuid_primary_keys(self):
        for app_label in PANDECT_APPS:
            for model in apps.get_app_config(app_label).get_models():
                with self.subTest(model=model._meta.label):
                    self.assertIsInstance(model._meta.pk, models.UUIDField)
                    self.assertFalse(model._meta.pk.editable)
                    first, second = model(), model()
                    self.assertIsInstance(first.pk, UUID)
                    self.assertEqual(first.pk.version, 4)
                    self.assertNotEqual(first.pk, second.pk)


class IdentifierSchemaTests(TestCase):
    def test_migrations_use_native_uuids_for_ids_and_referencing_columns(self):
        # Include upstream and automatic M2M tables referencing accounts.User.
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT table_name, column_name, data_type "
                "FROM information_schema.columns "
                "WHERE table_schema = current_schema()"
            )
            column_types = {(table, column): kind for table, column, kind in cursor}

        for model in apps.get_models(include_auto_created=True):
            for field in model._meta.local_fields:
                target = field.target_field if field.is_relation else field
                if isinstance(target, models.UUIDField):
                    with self.subTest(model=model._meta.label, field=field.name):
                        self.assertEqual(
                            column_types[(model._meta.db_table, field.column)], "uuid"
                        )
