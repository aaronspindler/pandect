"""Keep evidence collection filters through detail and analysis navigation."""


def list_params(request):
    return collection_params(request.GET)


def collection_params(values):
    params = values.copy()
    for key in list(params):
        if key not in {"q", "type", "state", "page", "person"}:
            del params[key]
    if "q" in params:
        params["q"] = params["q"][:200]
    return params


def query_suffix(request):
    value = list_params(request).urlencode()
    return f"?{value}" if value else ""
