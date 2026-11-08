from django.urls import include, path

app_name = "core"
urlpatterns = [
    path("evidence/", include("core.evidence.urls")),
    path("analysis/", include("core.analysis.urls")),
]
