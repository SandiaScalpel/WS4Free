#!/usr/bin/env bash
# Build the stylesheet with the Tailwind CSS standalone CLI (no Node required).
#
#   ./scripts/build-css.sh           one-off minified build
#   ./scripts/build-css.sh --watch   rebuild on template changes
#
# The CLI binary is downloaded to bin/ (git-ignored) on first use. The output,
# weather/static/weather/css/app.css, is committed so deployments never need it.
set -euo pipefail

TAILWIND_VERSION="v4.3.3"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN="$ROOT/bin/tailwindcss"
INPUT="$ROOT/assets/css/app.css"
OUTPUT="$ROOT/weather/static/weather/css/app.css"

if [[ ! -x "$BIN" ]] || ! "$BIN" --help 2>/dev/null | grep -q "${TAILWIND_VERSION}"; then
    case "$(uname -s)-$(uname -m)" in
        Linux-x86_64)   asset="tailwindcss-linux-x64" ;;
        Linux-aarch64)  asset="tailwindcss-linux-arm64" ;;
        Darwin-x86_64)  asset="tailwindcss-macos-x64" ;;
        Darwin-arm64)   asset="tailwindcss-macos-arm64" ;;
        *) echo "Unsupported platform $(uname -s)-$(uname -m); install Tailwind ${TAILWIND_VERSION} manually to $BIN" >&2; exit 1 ;;
    esac
    echo "Downloading Tailwind CSS ${TAILWIND_VERSION} (${asset})..."
    mkdir -p "$ROOT/bin"
    curl -fsSL -o "$BIN" "https://github.com/tailwindlabs/tailwindcss/releases/download/${TAILWIND_VERSION}/${asset}"
    chmod +x "$BIN"
fi

cd "$ROOT/assets/css"
if [[ "${1:-}" == "--watch" ]]; then
    exec "$BIN" -i "$INPUT" -o "$OUTPUT" --watch
fi
"$BIN" -i "$INPUT" -o "$OUTPUT" --minify
