"""Keep evidence collection filters through detail and analysis navigation."""


def list_params(request):
    return collection_params(request.GET)


def collection_params(values):
    params = values.copy()
    for key in list(params):
        if (
            key == "view"
            and params.get(key) == "gallery"
            and params.get("type") == "image"
        ):
            continue
        if key not in {"q", "type", "state", "page", "person", "checksum"}:
            del params[key]
    if "q" in params:
        params["q"] = params["q"][:200]
    if "checksum" in params:
        params["checksum"] = params["checksum"][:64]
    return params


def query_suffix(request):
    value = list_params(request).urlencode()
    return f"?{value}" if value else ""
