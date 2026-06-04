#!/usr/bin/env bash
# Render translated HTML in data/translated/ to PDF in data/output/.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ $# -gt 0 ]]; then
  args=( "$@" )
else
  shopt -s nullglob
  html=( data/translated/*.html )
  if [[ ${#html[@]} -eq 0 ]]; then
    echo "No HTML in data/translated/. Run ./scripts/translate-html.sh first." >&2
    exit 1
  fi
  args=()
  for f in "${html[@]}"; do
    args+=( "$(basename "$f")" )
  done
fi

docker compose run --rm renderer "${args[@]}"
