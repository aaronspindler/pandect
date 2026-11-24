from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from cases.models import CaseMembership
from core.testing import EvidenceViewTestMixin
from people.models import Person
from text.models import TextEvidence


class CaseSearchViewTests(EvidenceViewTestMixin, TestCase):
    def route(self, name):
        return reverse(f"core:search:{name}", args=[self.case.pk])

    def test_current_case_access_applies_to_results_and_quick_matches(self):
        Person.objects.create(case=self.case, first_name="Boathouse")
        Person.objects.create(case=self.other_case, first_name="Boathouse elsewhere")
        for role in ("owner", "editor", "viewer"):
            self.login(role)
            response = self.client.get(
                self.route("results"), {"q": "Boathouse", "type": "person"}
            )
            self.assertContains(response, "<mark>Boathouse</mark>")
            self.assertContains(response, "1 keyword match")
            self.assertNotContains(response, "elsewhere")
            quick = self.client.get(self.route("suggestions"), {"q": "Boathouse"})
            self.assertEqual(
                [row["title"] for row in quick.json()["results"]], ["Boathouse"]
            )
            for result in (quick, response):
                self.assertIn("no-store", result["Cache-Control"])
        CaseMembership.objects.filter(
            case=self.case, user=self.users["viewer"]
        ).delete()
        for role in ("viewer", "outsider"):
            self.login(role)
            for route in ("suggestions", "results"):
                with self.subTest(role=role, route=route):
                    response = self.client.get(self.route(route), {"q": "Boathouse"})
                    self.assertEqual(response.status_code, 404)
                    self.assertNotContains(
                        response, "Boathouse elsewhere", status_code=404
                    )

    def test_empty_invalid_and_escaped_results(self):
        self.login("viewer")
        response = self.client.get(self.route("results"))
        self.assertContains(response, "Enter a name, identifier, or phrase")
        for params in (
            {"q": "one " * 17},
            {"q": "record", "start": "2026-03-14", "end": "2026-03-01"},
            {"q": "record", "type": "other"},
        ):
            self.assertContains(
                self.client.get(self.route("results"), params),
                "Check the details below",
                status_code=400,
            )
        Person.objects.create(
            case=self.case, first_name="<script>Boathouse</script>", notes="Boathouse"
        )
        response = self.client.get(
            self.route("results"), {"q": "Boathouse", "type": "person"}
        )
        self.assertContains(
            response, "&lt;script&gt;<mark>Boathouse</mark>&lt;/script&gt;"
        )
        self.assertNotContains(response, "<script>Boathouse</script>")
        self.assertEqual(
            self.client.get(self.route("suggestions"), {"q": "B"}).json()["results"], []
        )

    @patch("core.case_search.views.prepare_case_search", return_value=True)
    def test_retry_requires_post_and_current_edit_access(self, prepare):
        for role, code in (("viewer", 403), ("outsider", 404)):
            self.login(role)
            self.assertEqual(self.client.post(self.route("retry")).status_code, code)
        prepare.assert_not_called()
        self.login("editor")
        self.assertEqual(self.client.get(self.route("retry")).status_code, 405)
        response = self.client.post(self.route("retry"))
        prepare.assert_called_once_with(self.case, self.users["editor"], retry=True)
        self.assertEqual(
            response["Location"],
            reverse("core:analysis:processing", args=[self.case.pk])
            + "#case-search-index",
        )

    def test_revocation_during_retrieval_withholds_the_response(self):
        self.login("viewer")

        def revoke(*args, **kwargs):
            CaseMembership.objects.filter(
                case=self.case, user=self.users["viewer"]
            ).delete()
            return [], ""

        with patch("core.case_search.views.suggest", side_effect=revoke):
            self.assertEqual(
                self.client.get(self.route("suggestions"), {"q": "case"}).status_code,
                404,
            )
        CaseMembership.objects.create(
            case=self.case, user=self.users["viewer"], role="viewer"
        )
        with patch("core.case_search.views.related_results", side_effect=revoke):
            self.assertEqual(
                self.client.get(
                    self.route("results"), {"q": "case", "type": "text", "meaning": "1"}
                ).status_code,
                404,
            )

    def test_hosted_reranking_requires_a_post_bound_to_the_query_and_grant(self):
        self.login("viewer")
        scope = {"provider": "workers-ai", "revision": "synthetic"}
        url = self.route("results") + "?q=dispatch"
        with (
            patch("core.case_search.views.rerank_scope", return_value=scope),
            patch(
                "core.case_search.views.related_results", return_value=([], "")
            ) as retrieve,
        ):
            response = self.client.get(url)
            token = response.context["search_token"]
            retrieve.assert_not_called()
            self.client.post(url, {"meaning": "1", "search_token": token})
            self.assertEqual(retrieve.call_args.kwargs["rank_scope"], scope)
            self.client.post(
                self.route("results") + "?q=altered",
                {"meaning": "1", "search_token": token},
            )
            self.assertFalse(retrieve.call_args.kwargs["rank_scope"])
            self.client.get(url + "&meaning=1")
            self.assertFalse(retrieve.call_args.kwargs["rank_scope"])
            with patch(
                "core.case_search.views.rerank_scope",
                return_value={**scope, "revision": "rotated"},
            ):
                self.client.post(url, {"meaning": "1", "search_token": token})
                self.assertFalse(retrieve.call_args.kwargs["rank_scope"])

    def test_display_semantic_coverage_and_source_passages(self):
        self.login("viewer")
        item = TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Ferry departure",
            body="We cancelled the appointment.",
        )
        params = {"q": "called off the meeting", "type": "text"}
        row = {
            "title": item.title,
            "url": self.url("detail", item),
            "label": "Text",
            "excerpt": item.body,
            "badges": (),
        }
        with (
            patch(
                "core.case_search.views.related_results",
                return_value=([row], "Indexing this case."),
            ) as related,
            patch("core.case_search.views.profile", return_value="test-profile"),
        ):
            initial = self.client.get(self.route("results"), params)
            self.assertContains(initial, "0 keyword matches")
            self.assertContains(initial, "Search by meaning")
            self.assertNotContains(initial, 'id="related-passages"')
            related.assert_not_called()
            response = self.client.get(self.route("results"), params | {"meaning": "1"})
            related.assert_called_once()
        self.assertContains(response, "0 keyword matches")
        self.assertContains(response, "Indexing this case.")
        self.assertContains(response, "Related passages (1)")
        self.assertContains(response, item.body)
        self.assertNotContains(response, "Try another word")
        self.assertContains(response, self.url("detail", item))
        response = self.client.get(
            self.route("results"), {"q": "Ferry", "type": "text"}
        )
        self.assertContains(response, "1 keyword match")
        self.assertNotContains(response, "Search by meaning")
        self.assertNotContains(response, 'id="related-passages"')
        response = self.client.get(
            self.route("results"), {"q": '"Ferry departure"', "type": "text"}
        )
        self.assertContains(response, "1 keyword match")
        self.assertNotContains(response, "Indexing status")
