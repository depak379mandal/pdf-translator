#!/usr/bin/env bash
# Translate text nodes in data/html/*.html → data/translated/ (Georgian by default).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ $# -gt 0 ]]; then
  args=( "$@" )
else
  shopt -s nullglob
  html=( data/html/*.html )
  if [[ ${#html[@]} -eq 0 ]]; then
    echo "No HTML in data/html/. Run ./scripts/pdf-to-html.sh first." >&2
    exit 1
  fi
  args=()
  for f in "${html[@]}"; do
    args+=( "$(basename "$f")" )
  done
fi

docker compose run --rm translator "${args[@]}"
