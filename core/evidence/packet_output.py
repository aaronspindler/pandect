from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from django.template.loader import render_to_string


@dataclass(frozen=True, slots=True)
class PacketExhibit:
    title: str
    excerpt: str
    date: date | None
    kind: str
    citation: str = ""
    source_sha256: str = ""


def packet_context(title: str, exhibits: Sequence[PacketExhibit]) -> dict:
    """Prepare the reviewed disclosure fields without consulting case data."""
    indexed = list(enumerate(exhibits, start=1))
    chronology = sorted(
        indexed, key=lambda entry: (entry[1].date is None, entry[1].date)
    )
    return {
        "title": title,
        "exhibits": indexed,
        "chronology": chronology,
        "includes_checksums": any(exhibit.source_sha256 for exhibit in exhibits),
    }


def render_packet(title: str, exhibits: Sequence[PacketExhibit]) -> str:
    return render_to_string(
        "core/evidence/packet_download.html", packet_context(title, exhibits)
    )
