from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("phone", "0012_group_message_destination")]
    operations = [
        migrations.RemoveIndex("textmessageevidence", "srch_text_message_words"),
        migrations.RemoveField("textmessageevidence", "search_words"),
    ]
