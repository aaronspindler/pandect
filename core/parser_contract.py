"""Django-free limits and errors shared by bounded file parsers."""

MAX_BYTES = 16 * 1024 * 1024
MAX_TEXT = 2 * 1024 * 1024
MAX_OUTPUT = 8 * 1024 * 1024


class ParserFailure(Exception):
    pass
