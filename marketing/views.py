from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_safe

from .catalogue import catalogue_context

PAGE_METADATA = {
    "home": (
        "Open-source investigation software",
        "Pandect is self-hostable investigation software for organizing evidence in "
        "private cases, collaborating, and tracing findings to sources.",
    ),
    "evidence": (
        "Evidence types and analysis tools",
        "Explore Pandect's tools for documents, images, recordings, messages, "
        "call records, and locations. Check supported formats and setup requirements.",
    ),
    "investigations": (
        "Investigation workflows",
        "See how investigators use Pandect for incidents, open-source intelligence, "
        "communications, and vehicle and location evidence.",
    ),
    "investigation_incidents": (
        "Incident investigation software",
        "Bring incident records, photos, messages, and location observations into "
        "a private case. Review recorded activity alongside evidence collection times.",
    ),
    "investigation_osint": (
        "Open-source intelligence (OSINT) investigations",
        "Organize collected public sources in a private case, record source URLs, "
        "and review repeated identifiers against the evidence.",
    ),
    "investigation_communications": (
        "Communications investigation software",
        "Review supported iMessage and WhatsApp exports alongside call records and "
        "transcripts. Trace conversations and call patterns to their sources.",
    ),
    "investigation_vehicle_location": (
        "Vehicle and location investigation software",
        "Review reported locations, import supported vehicle location CSVs, "
        "and trace possible trips to the observations that support them.",
    ),
    "trust": (
        "Case privacy and evidence integrity",
        "Learn how Pandect controls case access, preserves original evidence, "
        "and lets case owners approve configured external processing.",
    ),
    "roadmap": (
        "Project roadmap",
        "See what's planned for Pandect, including managed hosting, and what "
        "the open-source investigation project is exploring.",
    ),
}


def public_url(name):
    return settings.PANDECT_PUBLIC_ORIGIN + reverse(f"marketing:{name}")


@require_safe
def page(request):
    name = request.resolver_match.url_name
    title, description = PAGE_METADATA[name]
    context = {
        "page_title": f"{title} · Pandect",
        "page_description": description,
        "canonical_url": public_url(name),
        "public_origin": settings.PANDECT_PUBLIC_ORIGIN,
    }
    if name == "evidence":
        context.update(catalogue_context(request.GET.get("type", "")))
    return render(request, f"marketing/{name}.html", context)


@require_safe
def product(request):
    return redirect("marketing:home", permanent=True)


@require_safe
def sitemap(request):
    return render(
        request,
        "marketing/sitemap.xml",
        {"locations": [public_url(name) for name in PAGE_METADATA]},
        content_type="application/xml",
    )


@require_safe
def robots(request):
    return HttpResponse(
        f"User-agent: *\nDisallow:\n\nSitemap: {public_url('sitemap')}\n",
        content_type="text/plain",
    )
