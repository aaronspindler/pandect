from urllib.parse import parse_qsl, urlencode, urlsplit
from uuid import UUID

from django.urls import reverse


def page_number(value):
    if len(value) <= 9 and value.isascii() and value.isdecimal() and int(value) > 0:
        return int(value)
    return 1


def return_params(query):
    item = query.get("important_item", "")
    if len(item) == 36:
        try:
            return {
                "important_item": str(UUID(item)),
                "important_page": page_number(query.get("important_page", "")),
            }
        except ValueError:
            pass
    return {}


def with_return(url, params, *, replace=()):
    if not params and not replace:
        return url
    parts = urlsplit(url)
    query = {
        key: value
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key not in replace
    } | params
    return parts._replace(query=urlencode(query)).geturl()


def with_collection_return(url, encoded):
    """Overlay the two fields updated by Evidence's live collection search."""
    try:
        values = dict(parse_qsl(encoded, max_num_fields=2))
    except ValueError:
        return url
    params = {}
    if query := values.get("q", "")[:200]:
        params["q"] = query
    if "page" in values:
        params["page"] = page_number(values["page"])
    return with_return(url, params, replace=("q", "page"))


def list_url(case, *, page=1, item_id=None):
    return reverse(
        "core:important:list",
        args=[case.pk],
        query={"page": page},
        fragment=f"important-{item_id}" if item_id else None,
    )
