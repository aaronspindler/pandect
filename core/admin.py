from django.contrib import admin
from django_tasks_db.models import DBTaskResult

# The queue has no case boundary. Global model permissions must not expose it.
# django_tasks_db precedes this app in INSTALLED_APPS and registers it first.
admin.site.unregister(DBTaskResult)
