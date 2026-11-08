"""Retained recording eligibility shared by audio analysis methods."""


def _audio_input(source):
    return source.kind == "audio" or (
        source.kind in {"phone-call", "facetime"}
        and source.attachment is not None
        and bool(source.attachment.file)
    )
