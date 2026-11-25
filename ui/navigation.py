"""Navigation shared by searchable record lists."""

from urllib.parse import urlencode


def list_query(request):
    values = {}
    query = request.GET.get("q", "").strip()[:200]
    if query:
        values["q"] = query
    if request.GET.get("page", "").isdigit():
        values["page"] = request.GET["page"]
    return urlencode(values)
