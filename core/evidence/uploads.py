"""Bound each original while streaming, before it fills temporary storage."""

from django.conf import settings
from django.core.files.uploadhandler import FileUploadHandler, StopUpload


class LimitedUploadHandler(FileUploadHandler):
    def new_file(self, *args, **kwargs):
        super().new_file(*args, **kwargs)
        self.received = 0
        self.limit = settings.PANDECT_MAX_FILE_BYTES
        match = self.request.resolver_match
        if match and match.view_name == "geo:vehicle_import_upload":
            from geo.vehicle_imports import MAX_BYTES

            self.limit = min(self.limit, MAX_BYTES)
        elif match and match.view_name == "phone:imessage_upload":
            from phone.analysis.deterministic.imessage import import_file_limit

            self.limit = import_file_limit()
        if self.content_length and self.content_length > self.limit:
            self.request.evidence_upload_too_large = True
            raise StopUpload(connection_reset=False)

    def receive_data_chunk(self, raw_data, start):
        self.received += len(raw_data)
        if self.received > self.limit:
            self.request.evidence_upload_too_large = True
            raise StopUpload(connection_reset=False)
        return raw_data

    def file_complete(self, file_size):
        return None
