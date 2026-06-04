#!/usr/bin/env python3
"""Translate visible text in pdf2htmlEX HTML while preserving tags and layout CSS."""

from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Tag

WORKSPACE = Path("/workspace")
HTML_DIR = WORKSPACE / "html"
TRANSLATED_DIR = WORKSPACE / "translated"

SOURCE_LANG = os.environ.get("SOURCE_LANG", "auto")
TARGET_LANG = os.environ.get("TARGET_LANG", "ka")
TRANSLATOR = os.environ.get("TRANSLATOR", "google").lower()
CHUNK_SIZE = 4500
SKIP_PARENT_TAGS = frozenset({"script", "style", "noscript"})

GEORGIAN_FONT_HEAD = """
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+Georgian:wght@400;700&display=swap" rel="stylesheet">
<style id="georgian-font-override">
  body, .t, .t span, div.t, span.t {
    font-family: "Noto Sans Georgian", "Noto Sans", sans-serif !important;
  }
</style>
"""


def _parent_chain(tag: Tag) -> list[Tag]:
    chain: list[Tag] = []
    node: Tag | None = tag
    while node and isinstance(node, Tag):
        chain.append(node)
        node = node.parent  # type: ignore[assignment]
    return chain


def _should_translate(node: NavigableString) -> bool:
    if not str(node).strip():
        return False
    parent = node.parent
    if not isinstance(parent, Tag):
        return False
    for ancestor in _parent_chain(parent):
        if ancestor.name in SKIP_PARENT_TAGS:
            return False
    return True


def _collect_text_nodes(soup: BeautifulSoup) -> list[NavigableString]:
    nodes: list[NavigableString] = []
    for element in soup.find_all(string=True):
        if not isinstance(element, NavigableString):
            continue
        if isinstance(element, Tag):
            continue
        if _should_translate(element):
            nodes.append(element)
    return nodes


def _translate_google_free(texts: list[str]) -> list[str]:
    from deep_translator import GoogleTranslator

    translator = GoogleTranslator(source=SOURCE_LANG, target=TARGET_LANG)
    out: list[str] = []
    for text in texts:
        if not text.strip():
            out.append(text)
            continue
        out.append(translator.translate(text))
    return out


def _translate_deepl(texts: list[str]) -> list[str]:
    import deepl

    key = os.environ.get("DEEPL_API_KEY", "")
    if not key:
        raise RuntimeError("DEEPL_API_KEY is required when TRANSLATOR=deepl")
    translator = deepl.Translator(key)
    target = TARGET_LANG.upper()
    if target == "KA":
        target = "KA"  # DeepL uses EN-US style; Georgian is KA
    results = translator.translate_text(
        texts,
        source_lang=None if SOURCE_LANG == "auto" else SOURCE_LANG.upper(),
        target_lang=target,
    )
    if isinstance(results, list):
        return [r.text for r in results]
    return [results.text]


def _translate_gcp(texts: list[str]) -> list[str]:
    from google.cloud import translate_v2 as translate

    client = translate.Client()
    source = None if SOURCE_LANG == "auto" else SOURCE_LANG
    results = client.translate(texts, target_language=TARGET_LANG, source_language=source)
    if isinstance(results, dict):
        return [results["translatedText"]]
    return [r["translatedText"] for r in results]


def translate_batch(texts: list[str]) -> list[str]:
    if not texts:
        return []
    if TRANSLATOR == "deepl":
        return _translate_deepl(texts)
    if TRANSLATOR == "gcp":
        return _translate_gcp(texts)
    return _translate_google_free(texts)


def make_text_visible(soup: BeautifulSoup) -> None:
    """pdf2htmlEX hides text via transparent colors and hidden font classes."""
    for style in soup.find_all("style"):
        if not style.string:
            continue
        text = style.string
        text = text.replace("visibility:hidden", "visibility:visible")
        text = re.sub(r"color:\s*transparent", "color:#000", text)
        style.string = text
    if soup.head:
        visible = soup.new_tag("style", id="text-visibility-fix")
        visible.string = (
            ".t, .t span, .t * { visibility: visible !important; color: #000 !important; }"
            '[class*="fc"] { color: #000 !important; }'
            '[class^="ff"] { visibility: visible !important; '
            'font-family: "Noto Sans Georgian", Arial, sans-serif !important; }'
        )
        soup.head.append(visible)


def inject_georgian_font(soup: BeautifulSoup) -> None:
    if soup.head is None:
        head = soup.new_tag("head")
        if soup.html:
            soup.html.insert(0, head)
        else:
            soup.insert(0, head)
    fragment = BeautifulSoup(GEORGIAN_FONT_HEAD, "lxml")
    for child in list(fragment.head.children) if fragment.head else []:
        soup.head.append(child)
    for style in fragment.find_all("style"):
        if style.get("id") == "georgian-font-override":
            soup.head.append(style)


def copy_assets(stem: str) -> None:
    """Copy pdf2htmlEX sidecar files (fonts only; skip bitmap assets in text-only mode)."""
    TRANSLATED_DIR.mkdir(parents=True, exist_ok=True)
    skip_suffixes = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}
    for path in HTML_DIR.iterdir():
        if path.name == f"{stem}.html":
            continue
        if path.suffix.lower() in skip_suffixes:
            continue
        if path.stem == stem or path.name.startswith(f"{stem}."):
            dest = TRANSLATED_DIR / path.name
            if path.is_dir():
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(path, dest)
            else:
                shutil.copy2(path, dest)


def translate_file(html_name: str) -> None:
    src = HTML_DIR / html_name
    if not src.exists():
        raise FileNotFoundError(f"Missing {src}")

    stem = src.stem
    soup = BeautifulSoup(src.read_text(encoding="utf-8", errors="replace"), "lxml")
    nodes = _collect_text_nodes(soup)
    originals = [str(n) for n in nodes]

    # Translate in chunks to respect API limits.
    translated: list[str] = []
    for i in range(0, len(originals), 50):
        batch = originals[i : i + 50]
        merged = "\n␞\n".join(batch)
        if len(merged) > CHUNK_SIZE:
            for item in batch:
                translated.extend(translate_batch([item]))
        else:
            parts = translate_batch([merged])[0].split("\n␞\n")
            if len(parts) != len(batch):
                for item in batch:
                    translated.extend(translate_batch([item]))
            else:
                translated.extend(parts)

    for node, new_text in zip(nodes, translated, strict=False):
        node.replace_with(new_text)

    make_text_visible(soup)
    inject_georgian_font(soup)

    TRANSLATED_DIR.mkdir(parents=True, exist_ok=True)
    out = TRANSLATED_DIR / html_name
    html_out = str(soup)
    html_out = re.sub(
        r'<img[^>]*class="bi[^"]*"[^>]*/?\s*>',
        "",
        html_out,
        flags=re.IGNORECASE,
    )
    out.write_text(html_out, encoding="utf-8")
    copy_assets(stem)
    print(f"Translated: {src.name} → {out}")


def main(argv: list[str]) -> int:
    names = argv or [p.name for p in sorted(HTML_DIR.glob("*.html"))]
    if not names:
        print("No HTML files to translate in /workspace/html", file=sys.stderr)
        return 1

    for name in names:
        if not name.endswith(".html"):
            name = f"{name}.html"
        translate_file(name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
