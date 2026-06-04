#!/usr/bin/env bash
# Full workflow: PDF → HTML → Translate → PDF (all via Docker, data/ volume sync).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "=== Step 1/3: PDF → HTML (pdf2htmlEX) ==="
"$ROOT/scripts/pdf-to-html.sh" "$@"

echo ""
echo "=== Step 2/3: Translate HTML → Georgian ==="
"$ROOT/scripts/translate-html.sh"

echo ""
echo "=== Step 3/3: HTML → PDF (Chromium) ==="
"$ROOT/scripts/html-to-pdf.sh"

echo ""
echo "Pipeline complete. Output PDFs: data/output/"
