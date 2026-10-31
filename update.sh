#!/usr/bin/env bash
# Run from a separate checkout/release, never from the live installation directory.
set -euo pipefail
SOURCE="$(cd "$(dirname "$0")" && pwd)"
exec bash "$SOURCE/install.sh" --upgrade "$@"
