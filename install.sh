#!/usr/bin/env bash
# Download with curl, or run locally. Requires Python 3.11+ and curl.
main() {
    local python='' candidate
    local probe='import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'
    if [[ -n "${VENASTINE_PYTHON:-}" ]]; then
        python="$VENASTINE_PYTHON"
        "$python" -c "$probe" || { echo 'VENASTINE_PYTHON must name Python 3.11+.' >&2; return 1; }
    else
        for candidate in python3 python python3.14 python3.13 python3.12 python3.11; do
            if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c "$probe" 2>/dev/null; then
                python="$candidate"
                break
            fi
        done
    fi
    [[ -n "$python" ]] || { echo 'Install Python 3.11+ (including venv), then run this installer again.' >&2; return 1; }
    command -v curl >/dev/null 2>&1 || { echo 'Install curl, then run this installer again.' >&2; return 1; }
    helper=$(mktemp "${TMPDIR:-/tmp}/venastine-install.XXXXXXXX.py")
    # A subshell keeps cleanup and shell options out of a caller's shell.
    trap 'rm -f -- "$helper"' EXIT
    curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
        'https://raw.githubusercontent.com/YahyaHammad/Venastine-Research-Harness/main/scripts/install.py' \
        --output "$helper"
    "$python" "$helper" "$@"
}

# Parse the whole function before executing it, including when stdin is a pipe.
(set -euo pipefail; main "$@")
