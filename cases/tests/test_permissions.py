from uuid import uuid4

from django.db import connection
from django.http import Http404
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import URLResolver, get_resolver

from cases.models import CaseMembership
from cases.permissions import get_case
from core.testing import EvidenceViewTestMixin


class CaseRouteTests(SimpleTestCase):
    def test_every_case_route_requires_a_verified_product_session(self):
        def case_views(patterns, prefix=""):
            for entry in patterns:
                path = prefix + str(entry.pattern)
                if not path.startswith("cases/"):
                    continue
                if isinstance(entry, URLResolver):
                    yield from case_views(entry.url_patterns, path)
                else:
                    yield path, entry.callback

        views = list(case_views(get_resolver().url_patterns))
        self.assertTrue(views)
        for path, view in views:
            with self.subTest(path=path):
                self.assertTrue(getattr(view, "product_login_required", False))


class CasePermissionTests(EvidenceViewTestMixin, TestCase):
    def test_locked_lookup_is_scoped_before_locking(self):
        viewer = self.users["viewer"]
        self.assertEqual(get_case(viewer, self.case.pk, lock=True), self.case)
        for case_id in (self.other_case.pk, uuid4()):
            with (
                self.subTest(case_id=case_id),
                CaptureQueriesContext(connection) as queries,
                self.assertRaisesMessage(Http404, "Case not found."),
            ):
                get_case(viewer, case_id, lock=True)
            self.assertEqual(len(queries), 1)
            self.assertIn(CaseMembership._meta.db_table, queries[0]["sql"])
