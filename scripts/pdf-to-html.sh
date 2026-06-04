#!/usr/bin/env bash
# Convert PDF(s) to text-only HTML (positioned text, no background page images).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

ZOOM="${PDF2HTML_ZOOM:-1.3}"
INPUT_DIR="data/input"
HTML_DIR="data/html"

# Text layer only — no full-page PNG backgrounds in HTML.
PDF2HTML_ARGS=(
  --zoom "$ZOOM"
  --dest-dir /workspace/html
  --fallback 0
  --process-nontext 0
  --process-type3 1
  --tounicode 1
  --correct-text-visibility 0
  --embed-font 1
  --embed-image 0
)

if [[ $# -eq 0 ]]; then
  shopt -s nullglob
  files=( "$INPUT_DIR"/*.pdf )
  if [[ ${#files[@]} -eq 0 ]]; then
    echo "No PDFs found in $INPUT_DIR. Place a .pdf file there and retry." >&2
    exit 1
  fi
else
  files=()
  for arg in "$@"; do
    if [[ -f "$arg" ]]; then
      files+=( "$arg" )
    elif [[ -f "$INPUT_DIR/$arg" ]]; then
      files+=( "$INPUT_DIR/$arg" )
    elif [[ -f "$INPUT_DIR/${arg%.pdf}.pdf" ]]; then
      files+=( "$INPUT_DIR/${arg%.pdf}.pdf" )
    else
      echo "PDF not found: $arg" >&2
      exit 1
    fi
  done
fi

mkdir -p "$HTML_DIR"

for pdf in "${files[@]}"; do
  base="$(basename "$pdf" .pdf)"
  out_html="$HTML_DIR/${base}.html"
  echo "→ pdf2htmlEX (text only): $pdf → $out_html"
  docker compose run --rm pdf2html \
    "${PDF2HTML_ARGS[@]}" \
    "/workspace/input/$(basename "$pdf")"

  if [[ ! -f "$out_html" ]]; then
    echo "Expected output missing: $out_html" >&2
    exit 1
  fi

  python3 "$ROOT/scripts/fix-html-display.py" "$out_html"

  text_nodes=$( (grep -o 'class="t ' "$out_html" || true) | wc -l | tr -d ' ')
  bg_images=$( (grep -o 'class="bi' "$out_html" || true) | wc -l | tr -d ' ')

  if [[ "$text_nodes" -lt 5 ]]; then
    echo "" >&2
    echo "WARNING: $out_html has almost no text nodes ($text_nodes)." >&2
    echo "  The PDF may be scanned — OCR it first, then convert again." >&2
    exit 1
  fi

  echo "  ✓ text nodes: $text_nodes, background images: $bg_images"
done

echo "Done. Text-only HTML in $HTML_DIR/"
