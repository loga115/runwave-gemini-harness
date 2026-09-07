#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
port="${PORT:-8822}"
echo "Serving Suminagashi at http://127.0.0.1:${port}/"
exec python3 -m http.server "$port" --bind 127.0.0.1
