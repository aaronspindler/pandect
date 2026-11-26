"""Synthetic in-memory input fixtures shared by processing regression suites."""

import hashlib
import io
import struct
import wave
from types import SimpleNamespace
from uuid import uuid4

from PIL import Image, ImageDraw, PngImagePlugin

from core.analysis.sources import Source


def approve_case_execution(case, owner, executions):
    """Seed an exact-route approval from before destination-scoped grants."""
    from django.utils import timezone

    executions = {
        op: (
            route | {"case_ai_revision": str(case.external_ai_revision)}
            if route.get("backend") == "gateway"
            else route
        )
        for op, route in executions.items()
    }
    case.external_ai_enabled = True
    case.external_analysis_approval = {
        "revision": str(uuid4()),
        "approved_by": str(owner.pk),
        "approved_at": timezone.now().isoformat(),
        "executions": executions,
    }
    case.save(update_fields=("external_ai_enabled", "external_analysis_approval"))


def external_grant_kwargs(case, operation):
    """Approve a synthetic test configuration and bind one external execution."""
    from core.analysis.execution import configured_external_executions

    executions = configured_external_executions(case)
    if case.external_analysis_approval.get("executions") != executions:
        approve_case_execution(case, case.owner, executions)
    return {
        "approved_execution": executions[operation],
        "external_grant_revision": case.external_analysis_approval["revision"],
    }


class ByteStorage:
    def __init__(self, content):
        self.content = content

    def open(self, name, mode="rb"):
        return io.BytesIO(self.content)


def file_source(content, kind="document", **values):
    case_id = uuid4()
    model_name = f"{kind}evidence"
    item = SimpleNamespace(
        pk=uuid4(),
        case_id=case_id,
        title="Synthetic retained source",
        _meta=SimpleNamespace(model_name=model_name),
        file=SimpleNamespace(
            name=f"evidence/cases/{case_id}/{model_name}/{uuid4().hex}",
            storage=ByteStorage(content),
        ),
        byte_size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        **values,
    )
    return Source(str(uuid4()), kind, item)


def image_bytes(*, exif=False, xmp=None, rotate=False):
    image = Image.new("RGB", (120, 80), "white")
    ImageDraw.Draw(image).text((5, 20), "Synthetic receipt", fill="black")
    if rotate:
        image = image.rotate(90, expand=True)
    buffer = io.BytesIO()
    if exif:
        tags = Image.Exif()
        tags[271] = "Synthetic camera"
        tags[306] = "2026:10:27 12:34:56"
        tags[274] = 6
        image.save(buffer, format="JPEG", exif=tags)
    else:
        metadata = PngImagePlugin.PngInfo()
        if xmp is not None:
            metadata.add_itxt("XML:com.adobe.xmp", xmp)
        image.save(buffer, format="PNG", pnginfo=metadata)
    return buffer.getvalue()


def wav_bytes(seconds=1, value=200, *, channels=1, rate=8000):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as recording:
        recording.setnchannels(channels)
        recording.setsampwidth(2)
        recording.setframerate(rate)
        recording.writeframes(struct.pack("<h", value) * int(rate * seconds) * channels)
    return buffer.getvalue()


def preview_image_bytes():
    stream = io.BytesIO()
    image = Image.new("RGB", (80, 60), "white")
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("Comment", "Synthetic original metadata")
    image.save(stream, format="PNG", pnginfo=metadata)
    return stream.getvalue()
