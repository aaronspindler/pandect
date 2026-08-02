#!/bin/sh
set -eu

project_root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_root"

for source_dir in pandect tests; do
    if [ -d "$source_dir" ]; then
        find "$source_dir" -type d -name __pycache__ -prune -exec rm -rf {} +
        find "$source_dir" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
    fi
done
rm -rf .pytest_cache .ruff_cache build dist htmlcov staticfiles
rm -f .coverage

echo "Removed disposable artifacts. Persistent data and .env were preserved."
