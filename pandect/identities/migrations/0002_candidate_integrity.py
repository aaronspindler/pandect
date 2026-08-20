import uuid

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models

import pandect.catalog.models


class Migration(migrations.Migration):
    dependencies = [
        ("identities", "0001_initial"),
        ("processing", "0002_processingstage_remote_aggregate"),
    ]

    operations = [
        migrations.AddField(
            model_name="identitycandidate",
            name="producing_run",
            field=models.ForeignKey(
                blank=True,
                editable=False,
                help_text="Durable processing run that produced this candidate set.",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="identity_candidates",
                to="processing.processingrun",
            ),
        ),
        migrations.AddField(
            model_name="identitycandidate",
            name="producing_stage",
            field=models.ForeignKey(
                blank=True,
                editable=False,
                help_text="Durable processing stage that produced this candidate set.",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="identity_candidates",
                to="processing.processingstage",
            ),
        ),
        migrations.CreateModel(
            name="IdentityCandidateDecision",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "decision",
                    models.CharField(
                        choices=[
                            ("confirmed", "Confirmed"),
                            ("rejected", "Rejected"),
                        ],
                        max_length=16,
                    ),
                ),
                (
                    "origin",
                    models.CharField(
                        choices=[
                            ("operator", "Operator"),
                            (
                                "supersession",
                                "Superseded by another confirmation",
                            ),
                        ],
                        default="operator",
                        max_length=16,
                    ),
                ),
                ("note", models.TextField(blank=True)),
                (
                    "decided_at",
                    models.DateTimeField(
                        default=django.utils.timezone.now,
                        validators=[pandect.catalog.models.validate_timezone_aware],
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "candidate",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="decision_history",
                        to="identities.identitycandidate",
                    ),
                ),
            ],
            options={
                "ordering": ["decided_at", "created_at", "id"],
            },
        ),
        migrations.AddConstraint(
            model_name="identitycandidate",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("producing_run__isnull", True),
                        ("producing_stage__isnull", True),
                    ),
                    models.Q(
                        ("producing_run__isnull", False),
                        ("producing_stage__isnull", False),
                    ),
                    _connector="OR",
                ),
                name="identity_candidate_provenance_pair",
            ),
        ),
        migrations.AddIndex(
            model_name="identitycandidatedecision",
            index=models.Index(
                fields=["candidate", "-decided_at"],
                name="identity_decision_cand_idx",
            ),
        ),
    ]
