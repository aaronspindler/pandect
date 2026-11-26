import io
from hashlib import sha256
from unittest import TestCase

from core.csv_import import (
    BoundedCSVRecords,
    BoundedCSVStream,
    CSVByteLimitExceeded,
    CSVRecordLimitExceeded,
)


class BoundedCSVTests(TestCase):
    def test_multiline_records_reset_the_limit_and_track_unicode_offsets(self):
        text = io.StringIO('"é\r\nx",a\r\nb,c\r\n', newline="")
        records = BoundedCSVRecords(text, record_limit=10, offset=4, byte_offset=7)
        reader = iter(records)

        self.assertEqual(next(reader), ["é\r\nx", "a"])
        self.assertEqual((records.offset, records.byte_offset), (14, 18))
        self.assertEqual(list(reader), [["b", "c"]])
        self.assertEqual((records.offset, records.byte_offset), (19, 23))

    def test_short_physical_lines_cannot_exceed_the_logical_record_limit(self):
        text = io.StringIO('"one\ntwo\nthree",value', newline="")
        records = BoundedCSVRecords(text, record_limit=12)

        with self.assertRaises(CSVRecordLimitExceeded):
            next(iter(records))
        self.assertEqual(text.tell(), 13)

    def test_binary_stream_hashes_original_bytes_and_stops_at_first_excess_byte(self):
        data = b"\xef\xbb\xbfcolumn\r\n"
        original = io.BytesIO(data + b"extra")
        stream = BoundedCSVStream(original, byte_limit=len(data))
        with io.BufferedReader(stream, buffer_size=len(data)) as bounded:
            self.assertEqual(bounded.read(len(data)), data)
            self.assertEqual(stream.size, len(data))
            self.assertEqual(stream.digest.hexdigest(), sha256(data).hexdigest())
            with self.assertRaises(CSVByteLimitExceeded):
                bounded.read(1)
        self.assertEqual(original.tell(), len(data) + 1)
