from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0036_chat_selection_members")]

    operations = [
        migrations.AddField(
            model_name="chatturn",
            name="review_of",
            field=models.OneToOneField(
                null=True,
                on_delete=models.CASCADE,
                related_name="deeper_review",
                to="core.chatturn",
            ),
        ),
        migrations.AddField(
            model_name="chatturn",
            name="review_requests",
            field=models.PositiveIntegerField(db_default=0, default=0, editable=False),
        ),
    ]
