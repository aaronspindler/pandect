"""Synthetic hosted hits exercising the normal canonical verification path."""

from contextlib import contextmanager
from unittest.mock import patch
from uuid import uuid4

from django.test import override_settings

from core.case_search import cloudflare, lifecycle, material
from core.models import HostedSearchIndex, HostedSearchItem, HostedSearchSource

CONFIG = {"account": "a" * 32, "namespace": "synthetic", "token": "test-token"}


@contextmanager
def indexed(case, sources):
    """Register (surface, kind, pk) inputs and mock only Cloudflare's network call."""
    with override_settings(PANDECT_CLOUDFLARE_SEARCH=CONFIG):
        case.external_ai_enabled = True
        case.external_ai_revision = uuid4()
        case.external_analysis_approval = {"hosted_search": cloudflare.destination()}
        case.save()
        lifecycle.case_setting_changed(case)
        index = HostedSearchIndex.objects.get(revision=case.external_ai_revision)
        index.provisioned, index.status = True, "ready"
        index.save()
        chunks = {}

        def publish(changed_sources):
            for surface, kind, key in changed_sources:
                source, _ = HostedSearchSource.objects.get_or_create(
                    index=index, surface=surface, kind=kind, source_key=key
                )
                while source.dirty:
                    lifecycle.prepare_window(case, source)
                for item in (
                    HostedSearchItem.objects.filter(source=source)
                    .exclude(status="delete")
                    .order_by("start")
                ):
                    value = material.read(case, source, item.start)
                    if value:
                        chunks[item.pk] = {
                            "item": {"key": item.filename},
                            "text": value.text,
                        }
                        item.status = "completed"
                        item.save(update_fields=["status"])

        publish(sources)

        def lookup(current, method, path, *, payload, authorize_transfer):
            assert current.pk == index.pk and method == "POST" and path == "/search"
            authorize_transfer()
            return {
                "chunks": [
                    row
                    for row in list(chunks.values())
                    if payload["query"].casefold() in row["text"].casefold()
                ]
            }

        with patch("core.case_search.cloudflare.request", side_effect=lookup):
            yield publish
