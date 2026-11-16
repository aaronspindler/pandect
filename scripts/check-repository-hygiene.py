"""Keep local evidence out of the public repository without reading its contents."""

import argparse
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import PurePosixPath

LOCAL_DIRECTORIES = {
    "data",
    "media",
    "uploads",
    "exports",
    "artifacts",
    "backups",
    "local-evidence",
    "local_evidence",
    "test-evidence",
    "test_evidence",
    "secrets",
    ".secrets",
    "credentials",
    ".credentials",
}
EVIDENCE_EXTENSIONS = {
    ".csv",
    ".tsv",
    ".jsonl",
    ".pdf",
    ".doc",
    ".docx",
    ".odt",
    ".rtf",
    ".xls",
    ".xlsx",
    ".ods",
    ".ppt",
    ".pptx",
    ".eml",
    ".msg",
    ".mbox",
    ".vcf",
    ".gpx",
    ".kml",
    ".kmz",
    ".geojson",
    ".sql",
    ".dump",
    ".backup",
    ".bak",
    ".zip",
    ".tar",
    ".gz",
    ".tgz",
    ".bz2",
    ".xz",
    ".7z",
    ".rar",
    ".wav",
    ".mp3",
    ".mp4",
    ".mov",
    ".m4a",
    ".avi",
    ".flac",
    ".ogg",
    ".webm",
    ".aac",
}
IMAGE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".svg",
    ".tif",
    ".tiff",
    ".bmp",
    ".heic",
}


def prohibited_reason(path):
    normalized = PurePosixPath(path.casefold())
    if LOCAL_DIRECTORIES.intersection(normalized.parts):
        return "local storage"
    if (
        normalized.name == ".env" or normalized.name.startswith(".env.")
    ) and path not in {
        ".env.example",
        ".env.dokploy.example",
    }:
        return "environment files"
    if normalized.suffix in {".pem", ".key", ".p12", ".pfx"}:
        return "credentials"
    if re.search(r"\.(?:sqlite3?|db)(?:-(?:wal|shm|journal))?$", normalized.name):
        return "databases"
    if normalized.suffix in EVIDENCE_EXTENSIONS:
        return "evidence/export formats"
    if normalized.suffix in IMAGE_EXTENSIONS and not (
        "static" in normalized.parts[:-1] or path.startswith("docs/design/")
    ):
        return "images outside source assets"
    return None


def git(*arguments):
    return subprocess.check_output(["git", *arguments], stderr=subprocess.PIPE)


def history_paths(refs):
    if git("rev-parse", "--is-shallow-repository").strip() == b"true":
        raise ValueError(
            "Full history is required; fetch it with git fetch --unshallow."
        )
    # Include merge parents and root commits; later deletions must not hide files.
    return git(
        "log",
        "--format=",
        "--name-only",
        "-z",
        "--no-renames",
        "--diff-filter=AMT",
        "--root",
        "-m",
        *refs,
        "--",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--staged", action="store_true")
    modes.add_argument("--history", action="store_true")
    modes.add_argument("--pre-push", action="store_true")
    args = parser.parse_args()
    try:
        if args.staged:
            raw = git("ls-files", "--cached", "-z")
        elif args.history:
            raw = history_paths(["--all"])
        else:
            tips = set()
            for line in sys.stdin:
                fields = line.split()
                if len(fields) != 4 or not re.fullmatch(
                    r"[0-9a-f]{40}(?:[0-9a-f]{24})?", fields[1]
                ):
                    raise ValueError("Invalid pre-push input; refusing unchecked push.")
                if set(fields[1]) != {"0"}:
                    tips.add(fields[1])
            raw = history_paths(sorted(tips)) if tips else b""
        paths = {os.fsdecode(path) for path in raw.split(b"\0") if path}
        violations = Counter(
            reason for path in paths if (reason := prohibited_reason(path))
        )
    except (subprocess.CalledProcessError, ValueError) as error:
        message = (
            str(error) if isinstance(error, ValueError) else "Git inspection failed."
        )
        print(f"Repository hygiene: {message}", file=sys.stderr)
        return 1
    if violations:
        summary = ", ".join(
            f"{reason}: {count}" for reason, count in sorted(violations.items())
        )
        print(
            f"Repository hygiene blocked local material ({summary}).", file=sys.stderr
        )
        print(
            "Keep evidence under ignored data/ or outside the checkout. "
            "Remove it from the index and every outgoing commit before pushing. "
            "See docs/validation.md#repository-hygiene.",
            file=sys.stderr,
        )
        return 1
    print("Repository hygiene passed; no prohibited paths or file formats found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
