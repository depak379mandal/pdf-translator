#!/usr/bin/env python3
"""Make pdf2htmlEX text-only HTML readable in a browser (no background images)."""

from __future__ import annotations

import re
import sys
from pathlib import Path

TEXT_ONLY_CSS = """
<style id="pdf-translate-display-fix">
  /* Text-only mode: no page bitmaps; force visible text */
  .bi, .bf, img.bi {
    display: none !important;
    visibility: hidden !important;
    width: 0 !important;
    height: 0 !important;
  }
  .t, .t span, .t * {
    visibility: visible !important;
    color: #000 !important;
    opacity: 1 !important;
  }
  [class*="fc"] {
    color: #000 !important;
  }
  [class^="ff"] {
    visibility: visible !important;
    font-family: Arial, Helvetica, "Noto Sans", sans-serif !important;
  }
  .pc {
    display: block !important;
    position: relative !important;
  }
  .pf {
    position: relative !important;
    background: #fff !important;
    margin: 12px auto !important;
    box-shadow: 0 1px 4px rgba(0, 0, 0, 0.15);
    overflow: visible !important;
  }
  #page-container {
    position: relative !important;
    inset: auto !important;
    width: 100% !important;
    height: auto !important;
    overflow: visible !important;
    background: #e0e0e0 !important;
    padding: 12px 0 !important;
  }
  #sidebar, .loading-indicator {
    display: none !important;
  }
</style>
"""


def strip_background_images(html: str) -> str:
    """Remove embedded page PNG/SVG backgrounds from pdf2htmlEX output."""
    html = re.sub(r'<img[^>]*class="bi[^"]*"[^>]*/?\s*>', "", html, flags=re.IGNORECASE)
    html = re.sub(r'<img[^>]*class=\'bi[^\']*\'[^>]*/?\s*>', "", html, flags=re.IGNORECASE)
    return html


def fix_styles(html: str) -> str:
    """Patch inline styles pdf2htmlEX uses to hide text."""
    html = html.replace("visibility:hidden", "visibility:visible")
    html = re.sub(r"color:\s*transparent", "color:#000", html)
    return html


def fix_html(path: Path, *, strip_images: bool = True) -> None:
    html = path.read_text(encoding="utf-8", errors="replace")
    html = fix_styles(html)

    if strip_images:
        html = strip_background_images(html)

    # Replace or inject display-fix stylesheet
    if "pdf-translate-display-fix" in html:
        html = re.sub(
            r"<style id=\"pdf-translate-display-fix\">.*?</style>",
            TEXT_ONLY_CSS.strip(),
            html,
            count=1,
            flags=re.DOTALL,
        )
    elif "</head>" in html:
        html = html.replace("</head>", f"{TEXT_ONLY_CSS}\n</head>", 1)
    else:
        html = TEXT_ONLY_CSS + html

    html = re.sub(
        r'class="pc (pc[0-9a-f]+)',
        r'class="pc opened \1',
        html,
    )

    path.write_text(html, encoding="utf-8")
    text = len(re.findall(r'class="t ', html))
    bi = len(re.findall(r'class="bi', html))
    print(f"  ✓ text-only HTML ready: {path.name} ({text} text nodes, {bi} images remaining)")


def main(argv: list[str]) -> int:
    if not argv:
        print("Usage: fix-html-display.py <file.html> [...]", file=sys.stderr)
        return 1
    for name in argv:
        path = Path(name)
        if not path.is_file():
            path = Path("data/html") / name
            if not path.suffix:
                path = path.with_suffix(".html")
        if not path.is_file():
            print(f"Not found: {name}", file=sys.stderr)
            return 1
        fix_html(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
