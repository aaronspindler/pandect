"""Provenance, independent directional comparisons and source retention."""

import hashlib
import json
from uuid import uuid4

from django.core.files.base import ContentFile
from django.test import TestCase

from core.evidence.deletion import CASE_CONFLICT, execute_deletion, plan_deletion
from core.storage import retain_uploaded_originals
from core.testing import EvidenceTestMixin
from document.models import DocumentEvidence
from socialmedia.models import (
    InstagramCapture,
    InstagramCheck,
    InstagramComparison,
    InstagramConnection,
    InstagramMonitor,
)
from socialmedia.monitor_evidence import publish_captures


def roster(*handles, reason=""):
    return {
        "complete": not reason,
        "reason": reason,
        "members": [
            {"handle": row}
            if isinstance(row, str)
            else {"handle": row[0], "id": row[1]}
            for row in handles
        ],
    }


class MonitorEvidenceTests(EvidenceTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.connection = InstagramConnection.objects.create(
            user=self.owner, observer="example"
        )
        self.monitor = InstagramMonitor.objects.create(
            case=self.case,
            connection=self.connection,
            created_by=self.owner,
            target="test.account",
        )

    def record(self, followers, following, **overrides):
        observation = {
            "collector_version": "synthetic-browser-v1",
            "observer": "example",
            "target": "test.account",
            "target_id": "target1",
            "followers": followers,
            "following": following,
            **overrides,
        }
        with retain_uploaded_originals() as originals:
            check = InstagramCheck.objects.create(monitor=self.monitor)
            publish_captures(check, observation, originals=originals)
            check.status = "complete"
            check.save(update_fields=["status"])
        return {
            capture.direction: capture
            for capture in check.captures.select_related("evidence")
        }

    def test_equal_counts_different_members_and_renamed_account(self):
        first = self.record(roster(("first", "1"), ("gone", "2")), roster("prior"))
        next_ = self.record(
            roster(("renamed", "1"), ("new", "3"), ("new", "3")), roster("other")
        )
        self.assertEqual(next_["followers"].comparison.previous, first["followers"])
        self.assertEqual(
            next_["followers"].comparison.additions,
            [{"id": "3", "handle": "new", "handle_only": False}],
        )
        self.assertEqual(
            next_["following"].comparison.additions,
            [{"id": "", "handle": "other", "handle_only": True}],
        )
        capture = next_["followers"]
        self.assertEqual(capture.entry_count, 2)
        evidence = capture.evidence
        with evidence.file.open("rb") as f:
            original = f.read()
        self.assertEqual(evidence.sha256, hashlib.sha256(original).hexdigest())
        self.assertEqual(evidence.uploaded_by, self.owner)
        self.assertEqual(evidence.case, self.case)
        manifest = json.loads(original)
        self.assertEqual(manifest["members"], capture.entries)
        self.assertEqual(manifest["observer"], "example")
        self.assertEqual(manifest["source"], "synthetic")
        self.assertEqual(manifest["target_id"], "target1")
        self.assertIn("started_at", manifest)
        self.assertIn("finished_at", manifest)

    def test_partial_direction_retains_visible_names_and_preserves_its_baseline(self):
        first = self.record(roster("a"), roster())
        second = self.record(roster("b", reason="stalled"), roster("listed"))
        self.assertFalse(hasattr(second["followers"], "comparison"))
        self.assertEqual(second["followers"].entries, [{"handle": "b", "id": ""}])
        self.assertEqual(second["following"].comparison.previous, first["following"])
        third = self.record(roster("b", "a"), roster("listed"))
        self.assertEqual(third["followers"].comparison.previous, first["followers"])
        self.assertEqual(third["followers"].comparison.additions[0]["handle"], "b")
        self.assertEqual(third["following"].comparison.previous, second["following"])
        self.assertEqual(third["following"].comparison.addition_count, 0)

    def test_source_change_keeps_history_and_establishes_a_fresh_baseline(self):
        pilot = self.record(roster("old"), roster())["followers"]
        self.connection.source = "instagram"
        self.connection.save(update_fields=["source"])
        with self.assertRaisesMessage(ValueError, "observation_source_changed"):
            self.record(roster(), roster())
        # Even if a monitor is incorrectly reassigned, its previous capture cannot
        # become the baseline for another source.
        self.monitor.source = "instagram"
        self.monitor.save(update_fields=["source"])
        for metadata in (
            {"collector_version": "synthetic-browser-v1"},
            {"collector_version": "instagram-browser-v1"},
            {"collector_version": "instagram-endpoints-v1", "source": "synthetic"},
            {"collector_version": "instagram-endpoints-v1", "observer": "elsewhere"},
            {"collector_version": "instagram-endpoints-v1", "target": "elsewhere"},
        ):
            with (
                self.subTest(metadata=metadata),
                self.assertRaisesMessage(ValueError, "observation_source_changed"),
            ):
                self.record(roster(), roster(), **metadata)
        live = self.record(
            roster("new") | {"page_count": 1, "expected_count": 1},
            roster() | {"page_count": 1},
            collector_version="instagram-endpoints-v1",
        )["followers"]
        self.assertFalse(hasattr(live, "comparison"))
        self.assertEqual(live.evidence.source, "Instagram monitor")
        self.assertEqual(
            live.evidence.source_url, "https://www.instagram.com/test.account/"
        )
        with live.evidence.file.open("rb") as original:
            manifest = json.load(original)
        self.assertEqual(manifest["source"], "instagram")
        self.assertEqual(manifest["source_url"], live.evidence.source_url)
        self.assertEqual(manifest["collector_version"], "instagram-endpoints-v1")
        self.assertEqual((manifest["page_count"], manifest["expected_count"]), (1, 1))
        self.assertEqual(pilot.evidence.source, "Synthetic Instagram monitor")
        self.assertEqual(pilot.evidence.source_url, "")
        with pilot.evidence.file.open("rb") as original:
            self.assertEqual(json.load(original)["source"], "synthetic")

    def test_changed_target_identity_or_observer_starts_new_baselines(self):
        self.record(roster(), roster())
        b = self.record(roster("one"), roster(), target_id="other")
        self.assertFalse(hasattr(b["followers"], "comparison"))
        c = self.record(roster("two"), roster())
        self.assertFalse(hasattr(c["followers"], "comparison"))
        self.connection.observer_epoch = uuid4()
        self.connection.save()
        d = self.record(roster(), roster())
        self.assertFalse(hasattr(d["following"], "comparison"))

    def test_endpoint_baseline_retains_prior_version_and_partial_page_counts(self):
        self.connection.source = self.monitor.source = "instagram"
        self.connection.save(update_fields=["source"])
        self.monitor.save(update_fields=["source"])
        old = {
            "collector_version": "instagram-browser-v1",
            "members": [{"handle": "prior", "id": ""}],
        }
        document = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Earlier list",
            file=ContentFile(json.dumps(old).encode(), name="followers.json"),
        )
        legacy = InstagramCapture.objects.create(
            monitor_check=InstagramCheck.objects.create(
                monitor=self.monitor, status="complete"
            ),
            direction="followers",
            evidence=document,
            observer_epoch=self.connection.observer_epoch,
            target_identity="id:target1",
            collector_version=old["collector_version"],
            complete=True,
            entries=old["members"],
            entry_count=1,
        )

        followers = roster("current") | {"page_count": 1, "expected_count": 1}
        following = roster("known", reason="count_mismatch") | {
            "page_count": 2,
            "expected_count": 4,
        }
        version = {"collector_version": "instagram-endpoints-v1"}
        baseline = self.record(followers, following, **version)
        self.assertFalse(hasattr(baseline["followers"], "comparison"))
        self.assertTrue(baseline["following"].baseline_eligible)
        with baseline["following"].evidence.file.open("rb") as original:
            manifest = json.load(original)
        self.assertFalse(manifest["complete"])
        self.assertEqual(manifest["reason"], "count_mismatch")
        self.assertEqual((manifest["page_count"], manifest["expected_count"]), (2, 4))
        interrupted = self.record(
            followers,
            roster("known", reason="time_limit") | {"page_count": 1},
            **version,
        )
        self.assertFalse(interrupted["following"].baseline_eligible)
        self.assertFalse(hasattr(interrupted["following"], "comparison"))
        subsequent = self.record(
            followers,
            following | {"members": [{"handle": "new", "id": "17"}]},
            **version,
        )
        self.assertEqual(
            subsequent["followers"].comparison.previous, interrupted["followers"]
        )
        self.assertEqual(
            subsequent["following"].comparison.previous, baseline["following"]
        )
        self.assertEqual(
            subsequent["following"].comparison.additions,
            [{"handle": "new", "id": "17", "handle_only": True}],
        )
        next_complete = self.record(
            followers,
            roster(("new", "17"), ("second.new", "22"))
            | {"page_count": 2, "expected_count": 2},
            **version,
        )["following"]
        self.assertEqual(next_complete.comparison.previous, subsequent["following"])
        self.assertEqual(
            next_complete.comparison.additions,
            [{"handle": "second.new", "id": "22", "handle_only": False}],
        )
        legacy.refresh_from_db()
        self.assertEqual(legacy.collector_version, "instagram-browser-v1")
        with legacy.evidence.file.open("rb") as original:
            self.assertEqual(json.load(original), old)

        for invalid in (
            {"page_count": False},
            {"page_count": 0},
            {"page_count": 201},
            {"expected_count": None},
            {"expected_count": 1},
            {"expected_count": -1},
            {"expected_count": 10**12},
        ):
            with (
                self.subTest(invalid=invalid),
                self.assertRaisesMessage(ValueError, "invalid_observation"),
            ):
                self.record(followers, following | invalid, **version)

    def test_invalid_second_list_rolls_back_private_files_and_database_state(self):
        with self.assertRaises(ValueError):
            self.record(roster(), roster("invalid handle"))
        self.assertFalse(DocumentEvidence.objects.exists())
        self.assertFalse(InstagramCheck.objects.exists())
        self.assertFalse(any(row.is_file() for row in self.media_root.rglob("*")))
        with self.assertRaises(ValueError):
            self.record(roster(), roster(), observer="unexpected")
        with self.assertRaises(ValueError):
            self.record(roster(), roster(), collector_version="unknown")

    def test_deleting_a_source_invalidates_only_dependent_comparisons(self):
        first = self.record(roster("one"), roster("one"))
        before = plan_deletion(first["followers"].evidence, "document")
        second = self.record(roster("two"), roster("two"))
        plan = plan_deletion(first["followers"].evidence, "document")
        self.assertFalse(plan.blocker)
        self.assertNotEqual(before.fingerprint, plan.fingerprint)
        self.assertEqual(
            {row["type"] for row in plan.entries},
            {
                "Document evidence",
                "Instagram list capture",
                "Instagram list comparison",
            },
        )
        self.owner.is_superuser = True
        with self.captureOnCommitCallbacks(execute=True):
            execute_deletion(plan, self.owner)
        self.assertFalse(
            InstagramComparison.objects.filter(capture=second["followers"]).exists()
        )
        self.assertEqual(second["following"].comparison.previous, first["following"])
        self.assertTrue(
            InstagramCapture.objects.filter(pk=second["followers"].pk).exists()
        )
        self.assertFalse(
            first["followers"].evidence.file.storage.exists(
                first["followers"].evidence.file.name
            )
        )
        third = self.record(roster("three"), roster())
        self.assertEqual(third["followers"].comparison.previous, second["followers"])

    def test_corrupted_cross_case_comparison_blocks_deletion(self):
        first = self.record(roster(), roster())
        second = self.record(roster(), roster())
        self.monitor = InstagramMonitor.objects.create(
            case=self.other_case,
            connection=self.connection,
            created_by=self.owner,
            target="test.account",
        )
        foreign = self.record(roster(), roster())
        InstagramComparison.objects.filter(capture=second["followers"]).update(
            previous=foreign["following"]
        )
        self.assertEqual(
            plan_deletion(foreign["following"].evidence, "document").blocker,
            CASE_CONFLICT,
        )
        self.assertTrue(
            DocumentEvidence.objects.filter(pk=first["followers"].evidence_id).exists()
        )
