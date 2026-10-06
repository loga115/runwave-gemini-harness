#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -z "${GEMINI_API_KEY:-}" && -z "${RUNWAVE_API_KEY:-}" && -z "${GOOGLE_API_KEY:-}" ]]; then
  echo "Set GEMINI_API_KEY (or RUNWAVE_API_KEY / GOOGLE_API_KEY)." >&2
  exit 2
fi

if [[ "$#" -lt 1 ]]; then
  echo "usage: $0 <new-output-dir> [game-root] [instruction-file] [resources-dir]" >&2
  exit 2
fi

output_dir="$1"
shift
game_root="${1:-/app/game}"
instruction_file="${2:-/app/task/instruction.md}"
resources_dir="${3:-/app/resources}"
if [[ $# -gt 0 ]]; then shift; fi
if [[ $# -gt 0 ]]; then shift; fi
if [[ $# -gt 0 ]]; then shift; fi

exec python3 "$root/gemini_runner.py" \
  --config "$game_root/webgame.json" \
  --instructions "$instruction_file" \
  --resources "$resources_dir" \
  --out-dir "$output_dir" \
  "$@"
