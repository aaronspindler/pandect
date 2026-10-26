from uuid import uuid4

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models.functions import Lower
from django.utils import timezone


def initialize_case_details(apps, schema_editor):
    Case = apps.get_model("cases", "Case")
    database = schema_editor.connection.alias
    for case in Case.objects.using(database).only("pk", "created_at").iterator():
        Case.objects.using(database).filter(pk=case.pk).update(
            name=f"Case #{case.pk}", updated_at=case.created_at
        )


class Migration(migrations.Migration):
    dependencies = [
        ("cases", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="case",
            name="name",
            field=models.CharField(default="", max_length=255),
        ),
        migrations.AddField(
            model_name="case",
            name="description",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="case",
            name="updated_at",
            field=models.DateTimeField(auto_now=True, default=timezone.now),
            preserve_default=False,
        ),
        migrations.RunPython(initialize_case_details, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="case",
            constraint=models.CheckConstraint(
                condition=models.Q(name__regex=r"\S"),
                name="cases_case_name_present",
            ),
        ),
        migrations.CreateModel(
            name="CaseInvitation",
            fields=[
                ("id", models.UUIDField(default=uuid4, editable=False, primary_key=True, serialize=False)),
                ("email", models.EmailField(max_length=254)),
                ("role", models.CharField(choices=[("viewer", "Viewer"), ("editor", "Editor")], default="viewer", max_length=6)),
                ("token_digest", models.CharField(editable=False, max_length=64, unique=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("expires_at", models.DateTimeField()),
                ("accepted_at", models.DateTimeField(blank=True, null=True)),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                ("last_sent_at", models.DateTimeField(blank=True, null=True)),
                ("delivery_failed", models.BooleanField(default=False)),
                ("case", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="invitations", to="cases.case")),
                ("invited_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="case_invitations_sent", to=settings.AUTH_USER_MODEL)),
                ("accepted_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="case_invitations_accepted", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ("-created_at", "pk"),
                "constraints": [
                    models.UniqueConstraint(Lower("email"), models.F("case"), condition=models.Q(accepted_at__isnull=True, revoked_at__isnull=True), name="cases_one_pending_invitation"),
                    models.CheckConstraint(condition=models.Q(role__in=("viewer", "editor")), name="cases_valid_invitation_role"),
                ],
            },
        ),
    ]
