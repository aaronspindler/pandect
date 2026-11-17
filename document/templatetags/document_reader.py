"""Read an explicitly opened document through the bounded parser."""

from django import template

from core.analysis.errors import AnalysisError
from document.previews import inspect_document, preview_message

register = template.Library()


@register.inclusion_tag("document/reader.html", takes_context=True)
def document_reader(context, item):
    request = context["request"]
    result = {"item": item, "case": context["case"]}
    query = request.GET.copy()
    query.pop("document_page", None)
    result["first_page_url"] = f"?{query.urlencode()}#document-preview"
    result["page_fields"] = [
        (name, value) for name, values in query.lists() for value in values
    ]
    try:
        number = int(request.GET.get("document_page", "1"))
        if not 1 <= number <= 100:
            raise ValueError
    except ValueError:
        result["error"] = "Choose a document page from 1 to 100."
        result["page_error"] = True
        return result
    try:
        parsed = inspect_document(item, page=number)
    except AnalysisError as error:
        result["error"] = preview_message(error)
        result["page_error"] = error.code == "invalid_document_page"
        result["source_problem"] = error.code in {
            "source_unavailable",
            "source_changed",
        }
        return result
    result |= {"preview": parsed, "number": number}
    if parsed["format"] == "PDF":
        for name, candidate in (("previous", number - 1), ("next", number + 1)):
            if 1 <= candidate <= parsed["page_count"]:
                query = request.GET.copy()
                query["document_page"] = candidate
                result[name] = f"?{query.urlencode()}#document-preview"
    return result
