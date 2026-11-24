from django.urls import include, path

app_name = "core"
urlpatterns = [
    path("chat/", include("core.evidence_chat.urls")),
    path("search/", include("core.case_search.urls")),
    path("evidence/", include("core.evidence.urls")),
    path("analysis/", include("core.analysis.urls")),
    path("important/", include("core.important.urls")),
]
