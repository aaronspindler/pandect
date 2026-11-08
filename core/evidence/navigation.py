"""Keep evidence collection filters through detail and analysis navigation."""


def list_params(request):
    params = request.GET.copy()
    for key in list(params):
        if key not in {"q", "type", "state", "page"}:
            del params[key]
    if "q" in params:
        params["q"] = params["q"][:200]
    return params


def query_suffix(request):
    value = list_params(request).urlencode()
    return f"?{value}" if value else ""
