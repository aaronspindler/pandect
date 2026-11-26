"""Bounded input and export-time interpretation for retained CSV imports."""

import csv
import hashlib
import io
from datetime import UTC


class CSVByteLimitExceeded(Exception):
    pass


class CSVRecordLimitExceeded(Exception):
    pass


class BoundedCSVStream(io.RawIOBase):
    """Hash the original bytes while adapting storage to a buffered reader."""

    def __init__(self, original, *, byte_limit):
        super().__init__()
        self.original = original
        self.byte_limit = byte_limit
        self.size = 0
        self.digest = hashlib.sha256()

    def readable(self):
        return True

    def readinto(self, buffer):
        chunk = self.original.read(min(len(buffer), self.byte_limit - self.size + 1))
        self.size += len(chunk)
        if self.size > self.byte_limit:
            raise CSVByteLimitExceeded
        self.digest.update(chunk)
        buffer[: len(chunk)] = chunk
        return len(chunk)


class BoundedCSVRecords:
    """Read strict CSV records with a character limit, including quoted newlines."""

    def __init__(self, text, *, record_limit, offset=0, byte_offset=None):
        self._text = text
        self.record_limit = record_limit
        self.offset = offset
        self.byte_offset = byte_offset
        self._record_chars = 0

    def _lines(self):
        while line := self._text.readline(self.record_limit - self._record_chars + 1):
            self._record_chars += len(line)
            if self._record_chars > self.record_limit:
                raise CSVRecordLimitExceeded
            self.offset += len(line)
            if self.byte_offset is not None:
                self.byte_offset += len(line.encode("utf-8"))
            yield line

    def __iter__(self):
        reader = csv.reader(self._lines(), strict=True)
        while True:
            self._record_chars = 0
            cells = next(reader, None)
            if cells is None:
                return
            yield cells


def utc_candidates(local, zone):
    """Return the distinct UTC instants represented by a naive local time."""
    candidates = set()
    for fold in (0, 1):
        utc = local.replace(tzinfo=zone, fold=fold).astimezone(UTC)
        # The round trip discards nonexistent times without picking a fold.
        if utc.astimezone(zone).replace(tzinfo=None) == local:
            candidates.add(utc)
    return candidates
