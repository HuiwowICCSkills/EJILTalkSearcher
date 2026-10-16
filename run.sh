#!/usr/bin/env sh
# POSIX entry point.  Node.js 22+ and Chrome are needed only for the
# headless-browser fallback; the REST API path is pure Python.
set -eu

SKILL_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

for candidate in "$SKILL_DIR/venv/bin/python" "$(command -v python3 || true)" "$(command -v python || true)"; do
    [ -n "$candidate" ] || continue
    [ -x "$candidate" ] || continue
    if "$candidate" -c 'import bs4' >/dev/null 2>&1; then
        exec "$candidate" -X utf8 "$SKILL_DIR/scripts/ejiltalk_search.py" "$@"
    fi
done

echo "No Python with beautifulsoup4 was found. Run: python3 -m pip install -r requirements.txt" >&2
exit 1
