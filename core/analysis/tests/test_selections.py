import json
import re
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from cases.models import CaseMembership
from core.analysis.selections import (
    append_member,
    build_brief,
    finding_member,
    resolve_selection,
    revision_token,
    selection_deletion_impacts,
    source_member,
)
from core.analysis.services import (
    process_run,
    resolve_inputs,
    resolve_snapshots,
    review_result,
    start_run,
)
from core.models import AnalysisResult, AnalysisRun, SavedSelection
from core.testing import EvidenceViewTestMixin
from text.analysis.synthesis import finding_text
from text.models import TextEvidence


class SharedSelectionTests(EvidenceViewTestMixin, TestCase):
    def selection(self, members=(), **changes):
        return SavedSelection.objects.create(
            **(
                {
                    "case": self.case,
                    "name": "Synthetic selection",
                    "members": list(members),
                    "created_by": self.users["owner"],
                    "updated_by": self.users["owner"],
                }
                | changes
            )
        )

    def route(self, name, selection=None, member_index=None, case=None):
        args = [(case or self.case).pk]
        if selection:
            args.append(selection.pk)
        if member_index is not None:
            args.append(member_index)
        return reverse(f"core:analysis:selection_{name}", args=args)

    def source_ref(self, source):
        return source_member(self.case, {"kind": "text", "id": str(source.pk)})

    def finding_ref(self, source):
        run, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "entities",
            [{"kind": "text", "id": str(source.pk)}],
        )
        process_run(run.pk)
        return finding_member(self.case, run.result.pk, 0), run

    def test_shared_roles_can_read_export_but_only_editors_mutate(self):
        source = self.text()
        selection = self.selection([self.source_ref(source)])
        for role in ("owner", "editor", "viewer"):
            self.login(role)
            self.assertEqual(self.client.get(self.route("list")).status_code, 200)
            self.assertEqual(
                self.client.get(self.route("detail", selection)).status_code, 200
            )
            brief = self.client.get(self.route("brief", selection))
            self.assertEqual(brief.status_code, 200)
            self.assertIn("no-store", brief["Cache-Control"])
        self.login("viewer")
        for route in ("edit", "add", "delete", "analyze"):
            self.assertEqual(
                self.client.get(self.route(route, selection)).status_code, 403
            )
        self.assertEqual(self.client.get(self.route("create")).status_code, 403)
        CaseMembership.objects.filter(
            case=self.case, user=self.users["viewer"]
        ).delete()
        self.assertEqual(
            self.client.get(self.route("detail", selection)).status_code, 404
        )
        self.assertEqual(
            self.client.get(self.route("brief", selection)).status_code, 404
        )

    def test_empty_create_and_stale_rename_preserve_current_members(self):
        self.login("editor")
        response = self.client.post(
            self.route("create"), {"name": "New shared selection"}
        )
        self.assertEqual(response.status_code, 302)
        selection = SavedSelection.objects.get(name="New shared selection")
        token = revision_token(selection)
        source = self.text()
        selection.members = [self.source_ref(source)]
        selection.revision += 1
        selection.save()
        response = self.client.post(
            self.route("edit", selection), {"name": "Draft name", "revision": token}
        )
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "Draft name", status_code=409)
        selection.refresh_from_db()
        self.assertEqual(selection.name, "New shared selection")
        self.assertEqual(len(selection.members), 1)
        response = self.client.post(
            self.route("edit", selection),
            {
                "name": "Draft name",
                "revision": response.context["form"]["revision"].value(),
            },
        )
        self.assertEqual(response.status_code, 302)

    def test_add_signed_member_deduplicates_and_rejects_foreign_reference(self):
        source = self.text()
        selection = self.selection()
        self.login("owner")
        params = {"kind": "text", "evidence": source.pk}
        response = self.client.get(self.route("add", selection), params)
        data = {
            name: response.context["form"][name].value()
            for name in ("revision", "member")
        }
        self.assertEqual(
            self.client.post(self.route("add", selection), data).status_code, 302
        )
        selection.refresh_from_db()
        self.assertEqual(selection.members, [self.source_ref(source)])
        response = self.client.get(self.route("add", selection), params)
        data = {
            name: response.context["form"][name].value()
            for name in ("revision", "member")
        }
        self.assertEqual(
            self.client.post(self.route("add", selection), data).status_code, 302
        )
        selection.refresh_from_db()
        self.assertEqual(len(selection.members), 1)
        self.assertEqual(
            self.client.get(
                self.route("add", selection), {"result": uuid4(), "item": 0}
            ).status_code,
            404,
        )
        foreign = self.selection(case=self.other_case)
        self.assertEqual(
            self.client.get(self.route("detail", foreign)).status_code, 404
        )
        self.assertEqual(
            self.client.post(self.route("add", foreign), data).status_code, 404
        )
        data["member"] += "forged"
        self.assertEqual(
            self.client.post(self.route("add", selection), data).status_code, 409
        )

    def test_brief_embeds_exact_manifest_and_review_reasons_safely(self):
        source = self.text()
        TextEvidence.objects.filter(pk=source.pk).update(
            is_archived=True,
            archive_changed_at=timezone.now(),
            archive_changed_by=self.users["owner"],
        )
        source.refresh_from_db()
        member, run = self.finding_ref(
            TextEvidence.objects.create(
                case=self.case,
                uploaded_by=self.users["owner"],
                title="HTML <script>title</script>",
                body="Contact synthetic@example.test. </script><script>bad()</script>",
            )
        )
        review_result(
            self.users["owner"],
            self.case.pk,
            run.result.pk,
            "rejected",
            item_index=0,
            comment="Unconfirmed <reason>",
        )
        selection = self.selection([self.source_ref(source), member])
        html = build_brief(selection, self.case)
        manifest = json.loads(
            re.search(
                r'<script id="provenance-manifest" type="application/json">'
                r"(.*?)</script>",
                html,
                re.S,
            ).group(1)
        )
        self.assertEqual(manifest["selection"]["revision"], selection.revision)
        self.assertEqual(
            manifest["members"][0]["sources"][0]["reference"]["sha256"], source.sha256
        )
        self.assertTrue(manifest["members"][0]["sources"][0]["archived"])
        self.assertEqual(
            manifest["members"][1]["finding_review"]["decision"], "rejected"
        )
        self.assertEqual(
            manifest["members"][1]["finding_review"]["reason"], "Unconfirmed <reason>"
        )
        self.assertEqual(
            manifest["members"][1]["analysis"]["result_id"], str(run.result.pk)
        )
        self.assertNotIn("<script>title</script>", html)
        self.assertIn("Unconfirmed &lt;reason&gt;", html)
        self.assertIn("new Blob([content]", html)

    def test_exact_extraction_version_and_missing_member_never_substituted(self):
        source = self.text()
        # An exact retained extraction snapshot remains bound after another result.
        runs = []
        for value in ("Old retained wording", "New retained wording"):
            run = AnalysisRun.objects.create(
                case=self.case,
                requested_by=self.users["owner"],
                operation="native_text",
                method_version="synthetic.v1",
                status="complete",
                inputs=[self.source_ref(source)["source"]],
            )
            result = AnalysisResult.objects.create(
                case=self.case,
                run=run,
                data={
                    "items": [
                        {
                            "text": value,
                            "source_ids": [run.inputs[0]["id"]],
                            "label": "Extraction",
                            "anchors": [],
                        }
                    ]
                },
            )
            runs.append((run, result))
        member = source_member(
            self.case,
            {"kind": "text", "id": str(source.pk), "result_id": str(runs[0][1].pk)},
        )
        selection = self.selection([member])
        resolved = resolve_selection(selection, self.case)
        self.assertEqual(resolved["members"][0]["text"], "Old retained wording")
        source.delete()
        self.assertEqual(len(resolve_selection(selection, self.case)["unavailable"]), 1)
        with self.assertRaises(ValidationError):
            build_brief(selection, self.case)

    def test_selected_finding_is_unavailable_when_another_contributing_source_changes(
        self,
    ):
        first = self.text()
        other = TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Other",
            body="other@example.test",
        )
        run, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "entities",
            [
                {"kind": "text", "id": str(first.pk)},
                {"kind": "text", "id": str(other.pk)},
            ],
        )
        process_run(run.pk)
        selected = self.selection([finding_member(self.case, run.result.pk, 0)])
        TextEvidence.objects.filter(pk=first.pk).update(body="Changed without checksum")
        self.assertEqual(len(resolve_selection(selected, self.case)["unavailable"]), 1)

    def test_bounds_excerpts_and_deletion_inventory_impacts(self):
        source = TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Long original",
            body="x" * 5000,
        )
        member = self.source_ref(source)
        selection = self.selection([member])
        html = build_brief(selection, self.case)
        self.assertIn("0–4000 of 5000", html)
        self.assertIn("Excerpt truncated", html)
        self.assertFalse(append_member(selection, member))
        selection.members = [
            {"type": "finding", "result_id": str(uuid4()), "item_index": 0}
            for _ in range(500)
        ]
        with self.assertRaises(ValidationError):
            append_member(selection, member)
        with (
            patch("core.analysis.selections.MAX_BRIEF_BYTES", 1),
            self.assertRaises(ValidationError),
        ):
            build_brief(self.selection([member]), self.case)
        impacts = selection_deletion_impacts(self.case.pk, {("text", source.pk)}, set())
        self.assertEqual(
            {row["id"] for row in impacts},
            {
                str(selection.pk),
                str(SavedSelection.objects.exclude(pk=selection.pk).get().pk),
            },
        )
        self.assertEqual(impacts[0]["members"][0]["reference"], member)

    def test_source_selection_analysis_preserves_references_and_blocks_findings(self):
        source = TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Identifier source",
            body="source@example.test",
        )
        saved = self.selection([self.source_ref(source)])
        self.login("owner")
        response = self.client.get(
            self.route("analyze", saved), {"operation": "entities"}
        )
        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        response = self.client.post(
            self.route("analyze", saved),
            {
                "operation": "entities",
                "submission_id": form["submission_id"].value(),
                "selection_revision": form["selection_revision"].value(),
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            AnalysisRun.objects.latest("created_at").inputs,
            [saved.members[0]["source"]],
        )
        member, _ = self.finding_ref(source)
        saved.members = [member]
        saved.save()
        response = self.client.get(
            self.route("analyze", saved), {"operation": "entities"}
        )
        self.assertContains(response, "Use question synthesis")
        self.assertIsNone(response.context["form"])

    def test_remove_and_delete_only_remove_references_and_check_revision(self):
        source = self.text()
        saved = self.selection([self.source_ref(source)])
        self.login("editor")
        response = self.client.get(self.route("remove", saved, 0))
        old_revision = response.context["form"]["revision"].value()
        saved.revision += 1
        saved.save()
        self.assertEqual(
            self.client.post(
                self.route("remove", saved, 0), {"revision": old_revision}
            ).status_code,
            409,
        )
        self.assertEqual(
            self.client.post(
                self.route("remove", saved, 0), {"revision": revision_token(saved)}
            ).status_code,
            302,
        )
        saved.refresh_from_db()
        self.assertEqual(saved.members, [])
        self.assertTrue(TextEvidence.objects.filter(pk=source.pk).exists())
        self.assertEqual(
            self.client.post(
                self.route("delete", saved),
                {"revision": revision_token(saved), "confirm": "on"},
            ).status_code,
            302,
        )
        self.assertFalse(SavedSelection.objects.filter(pk=saved.pk).exists())
        self.assertTrue(TextEvidence.objects.filter(pk=source.pk).exists())

    def test_historical_location_approval_is_exported_but_new_analysis_requires_current(
        self,
    ):
        from geo.models import LocationEvidence

        location = LocationEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Reported address",
            address="Synthetic office",
        )
        original = source_member(
            self.case, {"kind": "location", "id": str(location.pk)}
        )["source"]
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.users["owner"],
            operation="geocode_locations",
            method_version="synthetic-geocoder.v1",
            status="complete",
            inputs=[original],
        )
        result = AnalysisResult.objects.create(
            case=self.case,
            run=run,
            data={
                "items": [
                    {
                        "label": "Location estimate",
                        "text": "Synthetic candidate",
                        "source_ids": [original["id"]],
                        "anchors": [],
                        "values": {"latitude": 43.0, "longitude": -79.0},
                    }
                ]
            },
        )
        review_result(
            self.users["owner"],
            self.case.pk,
            result.pk,
            "accepted",
            item_index=0,
            comment="Reviewed source context",
        )
        member = source_member(
            self.case,
            {
                "kind": "location",
                "id": str(location.pk),
                "result_id": str(result.pk),
                "geocode_candidate": 0,
            },
        )
        selection = self.selection([member])
        review_result(
            self.users["owner"],
            self.case.pk,
            result.pk,
            "rejected",
            item_index=0,
            comment="Later contradictory material",
        )
        self.assertEqual(resolve_selection(selection, self.case)["unavailable"], [])
        self.assertEqual(
            len(
                resolve_selection(selection, self.case, require_current_review=True)[
                    "unavailable"
                ]
            ),
            1,
        )
        html = build_brief(selection, self.case)
        self.assertIn("newer review", html)
        self.assertIn("Reviewed source context", html)
        self.assertIn(member["source"]["review_id"], html)

    def test_deletion_inventory_rechecks_selection_revision_and_retains_placeholders(
        self,
    ):
        source = TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Original subject to deletion",
            body="deleted@example.test",
        )
        finding, run = self.finding_ref(source)
        saved = self.selection([self.source_ref(source), finding])
        owner = self.users["owner"]
        owner.is_superuser = True
        owner.save(update_fields=["is_superuser"])
        self.login("owner")
        delete_url = self.url("delete", source)
        first = self.client.get(delete_url)
        self.assertEqual(first.status_code, 200)
        impacts = [
            entry
            for entry in first.context["deletion_plan"].entries
            if entry["type"] == "Shared selection"
        ]
        self.assertEqual(len(impacts), 1)
        self.assertEqual(impacts[0]["id"], str(saved.pk))
        self.assertEqual(impacts[0]["action"], "Retain unavailable references")
        self.assertEqual(impacts[0]["revision"], saved.revision)
        self.assertEqual(
            [member["reference"] for member in impacts[0]["members"]], saved.members
        )
        old_confirmation = {
            "confirm": "on",
            "revision": first.context["deletion_revision"],
        }
        old_fingerprint = first.context["deletion_plan"].fingerprint

        retained = TextEvidence.objects.create(
            case=self.case,
            uploaded_by=owner,
            title="Unaffected retained source",
            body="Unrelated original remains retained.",
        )
        saved.members.append(self.source_ref(retained))
        saved.revision += 1
        saved.save()
        rejected = self.client.post(delete_url, old_confirmation)
        self.assertContains(
            rejected, "What will be removed has changed", status_code=409
        )
        self.assertNotEqual(
            rejected.context["deletion_plan"].fingerprint, old_fingerprint
        )
        self.assertTrue(TextEvidence.objects.filter(pk=source.pk).exists())
        self.assertTrue(AnalysisResult.objects.filter(run=run).exists())
        self.assertFalse(rejected.context["form"]["confirm"].value())
        retained_members = list(saved.members)
        with self.captureOnCommitCallbacks(execute=True):
            deleted = self.client.post(
                delete_url,
                {
                    "confirm": "on",
                    "revision": rejected.context["deletion_revision"],
                },
            )
        self.assertEqual(deleted.status_code, 302)
        self.assertFalse(TextEvidence.objects.filter(pk=source.pk).exists())
        self.assertFalse(AnalysisRun.objects.filter(pk=run.pk).exists())
        self.assertTrue(TextEvidence.objects.filter(pk=retained.pk).exists())
        saved.refresh_from_db()
        self.assertEqual(saved.members, retained_members)
        resolved = resolve_selection(saved, self.case)
        self.assertEqual([row["index"] for row in resolved["unavailable"]], [0, 1])
        self.assertTrue(resolved["members"][2]["available"])
        self.login("viewer")
        detail = self.client.get(self.route("detail", saved))
        self.assertContains(detail, "Unavailable item 1")
        self.assertContains(detail, "Unavailable item 2")
        self.assertContains(detail, retained.title)
        self.assertNotContains(detail, source.body)
        self.assertEqual(self.client.get(self.route("brief", saved)).status_code, 409)

    def test_synthesis_brief_preserves_prior_finding_quotes_and_original_attribution(
        self,
    ):
        source = TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Retained contact wording",
            body="Contact person@example.test.",
            source="Synthetic interview transcript",
            source_url="https://example.test/citation",
        )
        prior_member, prior_run = self.finding_ref(source)
        review_result(
            self.users["owner"],
            self.case.pk,
            prior_run.result.pk,
            "unresolved",
            item_index=0,
            comment="Candidate remains unverified.",
        )
        parameters = {
            "question": "Which contact candidate was extracted?",
            "text_source_ids": [],
            "finding_refs": [{"result_id": prior_member["result_id"], "item_index": 0}],
        }
        run, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "question_synthesis",
            [{"kind": "text", "id": str(source.pk)}],
            parameters=parameters,
        )
        quote = "person@example.test"
        surface = finding_text(prior_run.result.data["items"][0])
        answer = {
            "items": [
                {
                    "section": "supported",
                    "text": "A prior finding extracted a possible contact.",
                    "citations": [
                        {
                            "record_id": f"finding:{prior_run.result.pk}:0",
                            "quote": quote,
                            "start": surface.index(quote),
                        }
                    ],
                }
            ],
            "unknowns": ["The contact's identity is not established."],
        }
        with patch(
            "core.analysis.local.LocalClient",
            return_value=SimpleNamespace(
                infer=Mock(return_value=answer),
                usage={},
            ),
        ):
            process_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "complete", run.error_code)
        retained_finding = run.result.data["items"][0]
        saved = self.selection([finding_member(self.case, run.result.pk, 0)])
        html = build_brief(saved, self.case)
        manifest = json.loads(
            re.search(
                r'<script id="provenance-manifest" type="application/json">'
                r"(.*?)</script>",
                html,
                re.S,
            ).group(1)
        )
        exported = manifest["members"][0]
        self.assertEqual(
            exported["finding_citations"], retained_finding["finding_citations"]
        )
        self.assertEqual(exported["anchors"], [])
        citation = exported["finding_citations"][0]
        self.assertEqual(citation["result_id"], str(prior_run.result.pk))
        self.assertEqual(citation["quote"], quote)
        self.assertEqual(citation["review_at_execution"], "unresolved")
        self.assertEqual(surface[citation["start"] : citation["end"]], quote)
        self.assertEqual(
            exported["sources"][0]["reference"]["evidence_id"], str(source.pk)
        )
        self.assertEqual(exported["sources"][0]["reference"]["sha256"], source.sha256)
        self.assertEqual(exported["sources"][0]["source"], source.source)
        self.assertEqual(exported["sources"][0]["source_url"], source.source_url)
        self.assertIn("Quoted prior findings", html)
        self.assertIn("derived findings, not original evidence", html)
        prior_run.result.delete()
        with self.assertRaises(ValidationError):
            build_brief(saved, self.case)

    def test_500_findings_share_one_result_read_and_source_validation(self):
        source = self.text()
        snapshot = self.source_ref(source)["source"]
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.users["owner"],
            operation="entities",
            method_version="synthetic.v1",
            status="complete",
            inputs=[snapshot],
        )
        result = AnalysisResult.objects.create(
            case=self.case,
            run=run,
            data={
                "summary": "unused retained detail " * 140_000,
                "items": [
                    {
                        "label": "Synthetic finding",
                        "text": f"Retained finding {index}",
                        "source_ids": [snapshot["id"]],
                        "anchors": [],
                    }
                    for index in range(500)
                ],
            },
        )
        saved = self.selection(
            [
                {"type": "finding", "result_id": str(result.pk), "item_index": index}
                for index in range(500)
            ]
        )
        with (
            CaptureQueriesContext(connection) as queries,
            patch(
                "core.analysis.selections.resolve_inputs", wraps=resolve_inputs
            ) as check,
        ):
            resolved = resolve_selection(saved, self.case, excerpt_only=True)
        self.assertEqual(resolved["unavailable"], [])
        self.assertEqual(len(resolved["members"]), 500)
        self.assertEqual(resolved["members"][-1]["text"], "Retained finding 499")
        self.assertEqual(check.call_count, 1)
        self.assertLessEqual(len(queries), 3)
        payload_reads = [
            query for query in queries if '"core_analysisresult"."data"' in query["sql"]
        ]
        self.assertEqual(len(payload_reads), 1)
        self.assertTrue(
            all(
                "data" in row["result"].get_deferred_fields()
                for row in resolved["members"]
            )
        )
        TextEvidence.objects.filter(pk=source.pk).update(body="Changed source")
        with (
            CaptureQueriesContext(connection) as queries,
            patch(
                "core.analysis.selections.resolve_inputs", wraps=resolve_inputs
            ) as check,
        ):
            unavailable = resolve_selection(saved, self.case)
        self.assertEqual(len(unavailable["unavailable"]), 500)
        self.assertEqual(check.call_count, 1)
        self.assertLessEqual(len(queries), 2)

    def test_extraction_excerpts_release_full_payloads_between_source_members(self):
        source = self.text()
        original = self.source_ref(source)["source"]
        members = []
        for index in range(3):
            run = AnalysisRun.objects.create(
                case=self.case,
                requested_by=self.users["owner"],
                operation="native_text",
                method_version="synthetic.v1",
                status="complete",
                inputs=[original],
            )
            result = AnalysisResult.objects.create(
                case=self.case,
                run=run,
                data={
                    "summary": "Unused extraction detail " * 125_000,
                    "items": [
                        {
                            "label": "Retained extraction",
                            "text": str(index) * 5000,
                            "source_ids": [original["id"]],
                            "anchors": [],
                        }
                    ],
                },
            )
            members.append(
                source_member(
                    self.case,
                    {
                        "kind": "text",
                        "id": str(source.pk),
                        "result_id": str(result.pk),
                    },
                )
            )
        saved = self.selection(members)
        caches = []

        def resolve(*args, **kwargs):
            cache = kwargs["cache"]
            self.assertEqual(cache.prior_results, {})
            self.assertTrue(
                all("body" not in item.__dict__ for item in cache.originals.values())
            )
            caches.append(cache)
            return resolve_snapshots(*args, **kwargs)

        with patch("core.analysis.selections.resolve_snapshots", side_effect=resolve):
            resolved = resolve_selection(saved, self.case, excerpt_only=True)
        self.assertEqual(resolved["unavailable"], [])
        self.assertEqual(len(caches), 3)
        self.assertEqual(caches[-1].prior_results, {})
        for index, row in enumerate(resolved["members"]):
            self.assertEqual(row["excerpt"], str(index) * 4000)
            self.assertEqual(row["total_characters"], 5000)
            self.assertTrue(row["truncated"])
            self.assertNotIn("body", row["source"].item.__dict__)
