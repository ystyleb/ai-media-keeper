#!/bin/bash
# Install Tailwind CSS Standalone CLI binary.
# Idempotent: skips if binary already present.
set -euo pipefail

BIN_DIR="$(cd "$(dirname "$0")/.." && pwd)/bin"
BIN_PATH="$BIN_DIR/tailwindcss"
VERSION="v3.4.13"

if [[ -x "$BIN_PATH" ]]; then
    echo "✓ tailwindcss already installed at $BIN_PATH"
    "$BIN_PATH" --help >/dev/null && exit 0
fi

mkdir -p "$BIN_DIR"

OS="$(uname -s)"
ARCH="$(uname -m)"

case "$OS-$ARCH" in
    Darwin-arm64)  TARGET="macos-arm64" ;;
    Darwin-x86_64) TARGET="macos-x64" ;;
    Linux-x86_64)  TARGET="linux-x64" ;;
    Linux-aarch64) TARGET="linux-arm64" ;;
    *) echo "Unsupported platform: $OS-$ARCH" >&2; exit 1 ;;
esac

URL="https://github.com/tailwindlabs/tailwindcss/releases/download/$VERSION/tailwindcss-$TARGET"
echo "Downloading $URL → $BIN_PATH"
curl -sSL -o "${BIN_PATH}.tmp" "$URL"
chmod +x "${BIN_PATH}.tmp"
mv "${BIN_PATH}.tmp" "$BIN_PATH"
echo "✓ tailwindcss installed at $BIN_PATH"
"$BIN_PATH" --help >/dev/null
