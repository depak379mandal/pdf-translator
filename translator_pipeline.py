#!/usr/bin/env python3
"""
Local PDF translation pipeline (no Docker):
  HTML → paragraph grouping (18px rule) → Google Cloud Translate → HTML → PDF

Checkpoint files are written under checkpoints/<name>/ for review between steps.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup, Tag
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT = ROOT / "data" / "html" / "document.html"
DEFAULT_OUTPUT_DIR = ROOT / "data" / "output"
CHECKPOINTS_ROOT = ROOT / "checkpoints"

Y_COORD_PATTERN = re.compile(
    r"\.(y[0-9a-z]+)\s*\{[^}]*?bottom\s*:\s*(-?[0-9.]+)\s*(px|pt)",
    re.IGNORECASE,
)
X_COORD_PATTERN = re.compile(
    r"\.(x[0-9a-z]+)\s*\{[^}]*?left\s*:\s*(-?[0-9.]+)\s*(px|pt)",
    re.IGNORECASE,
)
PAGE_WIDTH_PATTERN = re.compile(
    r"\.(w[0-9a-z]+)\s*\{[^}]*?width\s*:\s*(-?[0-9.]+)\s*(px|pt)",
    re.IGNORECASE,
)
PAGE_HEIGHT_PATTERN = re.compile(
    r"\.(h[0-9a-z]+)\s*\{[^}]*?height\s*:\s*(-?[0-9.]+)\s*(px|pt)",
    re.IGNORECASE,
)

GEORGIAN_FONT_STYLE = """
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+Georgian&display=swap');
@page { size: A4; margin: 0; }
.t, .t span, .t * {
  font-family: 'Noto Sans Georgian', Arial, sans-serif !important;
  visibility: visible !important;
  color: #000 !important;
}
[class*="fc"] { color: #000 !important; }
[class^="ff"] {
  visibility: visible !important;
  font-family: 'Noto Sans Georgian', Arial, sans-serif !important;
}
.pf .t {
  white-space: pre !important;
}
@media print {
  .pf { page-break-after: always; overflow: hidden; }
}
"""

STEPS = ("group", "translate", "html", "pdf", "all")
STEP_ORDER = ["group", "translate", "html", "pdf"]


@dataclass
class Config:
    input_html: Path
    name: str
    source_lang: str | None
    target_lang: str
    gap_min: float
    gap_max: float
    pdf_format: str
    print_background: bool
    checkpoint_dir: Path

    @property
    def groups_json(self) -> Path:
        return self.checkpoint_dir / "01_paragraph_groups.json"

    @property
    def translations_json(self) -> Path:
        return self.checkpoint_dir / "02_translations.json"

    @property
    def distribution_json(self) -> Path:
        return self.checkpoint_dir / "03a_distribution.json"

    @property
    def translated_html(self) -> Path:
        return self.checkpoint_dir / "03_translated.html"

    @property
    def final_pdf(self) -> Path:
        return self.checkpoint_dir / "04_final.pdf"

    @property
    def manifest_json(self) -> Path:
        return self.checkpoint_dir / "manifest.json"

    @property
    def run_log(self) -> Path:
        return self.checkpoint_dir / "run.log"

    @property
    def output_pdf(self) -> Path:
        return DEFAULT_OUTPUT_DIR / f"{self.name}.pdf"


@dataclass
class LayoutMaps:
    y: dict[str, float]
    x: dict[str, float]
    page_width: dict[str, float]
    page_height: dict[str, float]
    side_margin: float

    def max_text_width_px(self, node: Tag) -> float:
        page = _find_page_frame(node)
        pw = self.page_width.get(page, 774.0)
        left = self._node_left(node)
        return max(80.0, pw - left - self.side_margin)

    def _node_left(self, node: Tag) -> float:
        x_class = next((c for c in node.get("class", []) if c.startswith("x")), None)
        return self.x.get(x_class, self.side_margin) if x_class else self.side_margin


@dataclass
class ParagraphGroup:
    block_id: int
    texts: list[str]
    nodes: list[list[Tag]]
    y_classes: list[str]
    source_text: str = ""

    def to_checkpoint(self, text_nodes: list[Tag]) -> dict:
        first_idx = None
        if self.nodes and self.nodes[0]:
            try:
                first_idx = text_nodes.index(self.nodes[0][0])
            except ValueError:
                first_idx = None
        return {
            "block_id": self.block_id,
            "source_text": self.source_text or " ".join(self.texts),
            "y_classes": self.y_classes,
            "line_count": len(self.texts),
            "node_count": sum(len(line) for line in self.nodes),
            "first_node_index": first_idx,
        }


def setup_logging(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("translator_pipeline")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


def load_config_from_env(input_html: Path | None, name: str | None) -> Config:
    load_dotenv(ROOT / ".env")
    html_path = Path(input_html or os.getenv("INPUT_HTML", DEFAULT_INPUT))
    if not html_path.is_absolute():
        html_path = ROOT / html_path
    doc_name = name or html_path.stem
    cp_dir = CHECKPOINTS_ROOT / doc_name
    source = os.getenv("SOURCE_LANG", "auto")
    return Config(
        input_html=html_path,
        name=doc_name,
        source_lang=None if source in ("", "auto") else source,
        target_lang=os.getenv("TARGET_LANG", "ka"),
        gap_min=float(os.getenv("PARAGRAPH_GAP_MIN", "18")),
        gap_max=float(os.getenv("PARAGRAPH_GAP_MAX", "400")),
        pdf_format=os.getenv("PDF_FORMAT", "A4"),
        print_background=os.getenv("PRINT_BACKGROUND", "true").lower() != "false",
        checkpoint_dir=cp_dir,
    )


def update_manifest(cfg: Config, **kwargs: Any) -> None:
    cfg.checkpoint_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {}
    if cfg.manifest_json.exists():
        manifest = json.loads(cfg.manifest_json.read_text(encoding="utf-8"))
    manifest.update(kwargs)
    manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
    manifest.setdefault("name", cfg.name)
    manifest.setdefault("input_html", str(cfg.input_html))
    cfg.manifest_json.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _find_page_frame(node: Tag) -> str:
    parent = node.parent
    while parent is not None:
        if parent.name == "div" and "pf" in parent.get("class", []):
            for cls in parent.get("class", []):
                if cls.startswith("w"):
                    return cls
            return "w0"
        parent = parent.parent
    return "w0"


def _class_px_only(css_text: str, pattern: re.Pattern[str]) -> dict[str, float]:
    """Parse CSS rules; prefer px over pt when both exist for the same class."""
    out: dict[str, float] = {}
    units: dict[str, str] = {}
    for cls, value, unit in pattern.findall(css_text):
        unit = unit.lower()
        if cls not in out or (units.get(cls) == "pt" and unit == "px"):
            out[cls] = float(value)
            units[cls] = unit
    return out


def parse_layout_maps(soup: BeautifulSoup) -> LayoutMaps:
    css_text = "".join(tag.get_text() for tag in soup.find_all("style"))
    x_map = _class_px_only(css_text, X_COORD_PATTERN)
    side_margin = float(os.getenv("PAGE_SIDE_MARGIN", "0"))
    if side_margin <= 0:
        side_margin = min(x_map.get("x0", 38.0), 45.0)
    return LayoutMaps(
        y=_class_px_only(css_text, Y_COORD_PATTERN),
        x=x_map,
        page_width=_class_px_only(css_text, PAGE_WIDTH_PATTERN),
        page_height=_class_px_only(css_text, PAGE_HEIGHT_PATTERN),
        side_margin=side_margin,
    )


def parse_y_coordinates(soup: BeautifulSoup) -> dict[str, float]:
    return parse_layout_maps(soup).y


def group_paragraphs(
    soup: BeautifulSoup,
    y_coordinates: dict[str, float],
    gap_min: float,
    gap_max: float,
) -> tuple[list[ParagraphGroup], list[Tag]]:
    text_nodes = soup.find_all("div", class_="t")
    groups: list[ParagraphGroup] = []
    current: ParagraphGroup | None = None
    last_y_class: str | None = None
    block_id = 0

    for node in text_nodes:
        y_class = next((c for c in node.get("class", []) if c.startswith("y")), None)
        text = node.get_text(strip=True)
        if not y_class or not text:
            continue

        if current is None:
            current = ParagraphGroup(block_id, [], [], [])
            block_id += 1

        if y_class == last_y_class:
            current.texts[-1] += " " + text
            current.nodes[-1].append(node)
        else:
            if current.y_classes:
                prev_y = current.y_classes[-1]
                curr_bottom = y_coordinates.get(prev_y)
                next_bottom = y_coordinates.get(y_class)
                if curr_bottom is not None and next_bottom is not None:
                    gap = abs(curr_bottom - next_bottom)
                    if gap_min < gap < gap_max:
                        current.source_text = " ".join(current.texts)
                        groups.append(current)
                        current = ParagraphGroup(block_id, [], [], [])
                        block_id += 1

            current.texts.append(text)
            current.nodes.append([node])
            current.y_classes.append(y_class)
            last_y_class = y_class

    if current and current.texts:
        current.source_text = " ".join(current.texts)
        groups.append(current)

    return groups, text_nodes


def step_group(cfg: Config, logger: logging.Logger) -> list[ParagraphGroup]:
    logger.info("Step 1: Loading HTML and grouping paragraphs from %s", cfg.input_html)
    if not cfg.input_html.exists():
        raise FileNotFoundError(f"Input HTML not found: {cfg.input_html}")

    soup = BeautifulSoup(cfg.input_html.read_text(encoding="utf-8", errors="replace"), "lxml")
    y_coords = parse_y_coordinates(soup)
    logger.info("  Parsed %d y-coordinate CSS rules", len(y_coords))

    groups, text_nodes = group_paragraphs(soup, y_coords, cfg.gap_min, cfg.gap_max)
    logger.info("  Found %d text nodes → %d paragraph groups", len(text_nodes), len(groups))

    checkpoint = {
        "meta": {
            "input_html": str(cfg.input_html),
            "text_node_count": len(text_nodes),
            "paragraph_count": len(groups),
            "gap_min": cfg.gap_min,
            "gap_max": cfg.gap_max,
            "y_rule_count": len(y_coords),
        },
        "blocks": [g.to_checkpoint(text_nodes) for g in groups],
    }
    cfg.groups_json.write_text(
        json.dumps(checkpoint, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    logger.info("  Checkpoint: %s", cfg.groups_json)
    update_manifest(cfg, step_group={"paragraph_count": len(groups), "text_nodes": len(text_nodes)})
    return groups


def load_groups_from_checkpoint(cfg: Config) -> list[dict]:
    if not cfg.groups_json.exists():
        raise FileNotFoundError(f"Run --step group first. Missing {cfg.groups_json}")
    return json.loads(cfg.groups_json.read_text(encoding="utf-8"))["blocks"]


class TranslateClient:
    """Wrapper: service-account via google-cloud-translate, or REST for API keys."""

    def __init__(self, mode: str, client: Any = None, api_key: str = "") -> None:
        self.mode = mode
        self._client = client
        self._api_key = api_key

    def translate(self, text: str, target_language: str, source_language: str | None = None) -> str:
        if self.mode == "rest":
            import requests

            params: dict[str, str] = {
                "key": self._api_key,
                "q": text,
                "target": target_language,
                "format": "text",
            }
            if source_language:
                params["source"] = source_language
            resp = requests.post(
                "https://translation.googleapis.com/language/translate/v2",
                data=params,
                timeout=60,
            )
            resp.raise_for_status()
            return resp.json()["data"]["translations"][0]["translatedText"]

        kwargs: dict[str, Any] = {"target_language": target_language}
        if source_language:
            kwargs["source_language"] = source_language
        result = self._client.translate(text, **kwargs)
        return result["translatedText"]


def create_translate_client() -> TranslateClient:
    api_key = os.getenv("GOOGLE_TRANSLATE_API_KEY", "").strip()
    creds = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()

    if api_key:
        return TranslateClient("rest", api_key=api_key)
    if creds.startswith("AIza"):
        return TranslateClient("rest", api_key=creds)
    if creds and os.path.isfile(creds):
        from google.cloud import translate_v2 as translate

        return TranslateClient("gcp", client=translate.Client())
    raise RuntimeError(
        "Set GOOGLE_TRANSLATE_API_KEY or GOOGLE_APPLICATION_CREDENTIALS "
        "(path to service-account JSON, or API key) in .env"
    )


def step_translate(cfg: Config, logger: logging.Logger) -> list[dict]:
    logger.info("Step 2: Translating paragraph groups via Google Cloud")
    blocks = load_groups_from_checkpoint(cfg)
    client = create_translate_client()

    results: list[dict] = []
    failed = 0

    for block in blocks:
        block_id = block["block_id"]
        source_text = block["source_text"]
        try:
            translated = client.translate(
                source_text,
                target_language=cfg.target_lang,
                source_language=cfg.source_lang,
            )
            status = "ok"
        except Exception as exc:  # noqa: BLE001
            translated = ""
            status = "error"
            failed += 1
            logger.error("  Block %d failed: %s", block_id, exc)

        results.append(
            {
                "block_id": block_id,
                "source": source_text,
                "translated": translated,
                "target_language": cfg.target_lang,
                "status": status,
            }
        )

    cfg.translations_json.write_text(
        json.dumps({"blocks": results, "failed": failed}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    logger.info(
        "  Checkpoint: %s (%d ok, %d failed)",
        cfg.translations_json,
        len(results) - failed,
        failed,
    )
    update_manifest(cfg, step_translate={"total": len(results), "failed": failed})
    return results


def rebuild_groups_for_inject(
    soup: BeautifulSoup,
    y_coordinates: dict[str, float],
    gap_min: float,
    gap_max: float,
) -> list[ParagraphGroup]:
    groups, _ = group_paragraphs(soup, y_coordinates, gap_min, gap_max)
    return groups


def _line_max_width_px(line_nodes: list[Tag], layout: LayoutMaps) -> float:
    """Usable width from the leftmost fragment to the symmetric right margin."""
    leftmost = min(layout._node_left(n) for n in line_nodes)
    page = _find_page_frame(line_nodes[0])
    pw = layout.page_width.get(page, 774.0)
    return max(80.0, pw - leftmost - layout.side_margin)


def _estimate_max_chars(line_nodes: list[Tag], layout: LayoutMaps) -> int:
    """One visual line per div.t — match original line length, capped by page width."""
    max_width = _line_max_width_px(line_nodes, layout)
    source_len = max(len(" ".join(n.get_text(strip=True) for n in line_nodes)), 1)
    width_cap = max(12, int(max_width / 6.0))
    return min(source_len, width_cap)


def distribute_to_line_slots(
    group: ParagraphGroup,
    translated: str,
    layout: LayoutMaps,
) -> list[str]:
    """Fill each original y-row with a single line of text (no in-node line breaks)."""
    if not group.nodes:
        return []

    limits = [_estimate_max_chars(line_nodes, layout) for line_nodes in group.nodes]
    words = translated.split()
    if not words:
        return [""] * len(group.nodes)

    distributed: list[str] = []
    word_idx = 0

    for limit in limits:
        parts: list[str] = []
        used = 0
        while word_idx < len(words):
            word = words[word_idx]
            add_len = len(word) + (1 if parts else 0)
            if parts and used + add_len > limit:
                break
            parts.append(word)
            used += add_len
            word_idx += 1
        distributed.append(" ".join(parts))

    if word_idx < len(words):
        remainder = " ".join(words[word_idx:])
        if distributed:
            distributed[-1] = f"{distributed[-1]} {remainder}".strip()
        else:
            distributed.append(remainder)

    while len(distributed) < len(group.nodes):
        distributed.append("")

    return distributed[: len(group.nodes)]


def inject_distributed_lines(
    group: ParagraphGroup,
    distributed_lines: list[str],
    layout: LayoutMaps,
) -> None:
    """Place each logical line in the first div.t only; wrap inside max-width box."""
    for i, line_nodes in enumerate(group.nodes):
        if not line_nodes:
            continue
        chunk = distributed_lines[i] if i < len(distributed_lines) else ""
        first = line_nodes[0]
        first.clear()
        if chunk:
            first.string = chunk
        max_width = _line_max_width_px(line_nodes, layout)
        style = first.get("style", "")
        extra = f"max-width:{max_width:.1f}px;white-space:pre"
        first["style"] = f"{style};{extra}".strip(";") if style else extra
        for node in line_nodes[1:]:
            node.clear()


def apply_display_fixes(soup: BeautifulSoup) -> BeautifulSoup:
    for style in soup.find_all("style"):
        if not style.string:
            continue
        text = style.string.replace("visibility:hidden", "visibility:visible")
        text = re.sub(r"color:\s*transparent", "color:#000", text, flags=re.IGNORECASE)
        style.string = text

    if soup.head is None:
        head = soup.new_tag("head")
        if soup.html:
            soup.html.insert(0, head)
        else:
            soup.insert(0, head)

    if not soup.find("style", id="pipeline-display-fix"):
        tag = soup.new_tag("style", id="pipeline-display-fix")
        tag.string = GEORGIAN_FONT_STYLE
        soup.head.append(tag)

    html = str(soup)
    html = re.sub(r'<img[^>]*class="bi[^"]*"[^>]*/?\s*>', "", html, flags=re.IGNORECASE)
    html = re.sub(r'class="pc (pc[0-9a-f]+)', r'class="pc opened \1', html)
    return BeautifulSoup(html, "lxml")


def step_html(cfg: Config, logger: logging.Logger) -> Path:
    logger.info("Step 3: Injecting translations (one line per y-row)")
    if not cfg.translations_json.exists():
        raise FileNotFoundError(f"Run --step translate first. Missing {cfg.translations_json}")

    translations = {
        b["block_id"]: b["translated"]
        for b in json.loads(cfg.translations_json.read_text(encoding="utf-8"))["blocks"]
        if b.get("status") == "ok" and b.get("translated")
    }

    ratio_threshold = float(os.getenv("LENGTH_RATIO_THRESHOLD", "1.8"))
    font_scale_min = float(os.getenv("FONT_SCALE_MIN", "0.85"))

    soup = BeautifulSoup(cfg.input_html.read_text(encoding="utf-8", errors="replace"), "lxml")
    layout = parse_layout_maps(soup)
    y_coords = layout.y
    groups = rebuild_groups_for_inject(soup, y_coords, cfg.gap_min, cfg.gap_max)

    distribution_records: list[dict] = []
    injected = 0
    multi_line_blocks = 0

    for group in groups:
        translated = translations.get(group.block_id)
        if not translated or not group.nodes:
            continue

        distributed = distribute_to_line_slots(group, translated, layout)
        inject_distributed_lines(group, distributed, layout)

        source_len = len(group.source_text or "")
        trans_len = len(translated)
        if len(group.nodes) > 1:
            multi_line_blocks += 1

        record: dict = {
            "block_id": group.block_id,
            "line_count": len(group.nodes),
            "line_capacities": [
                max(len(" ".join(n.get_text(strip=True) for n in line)), 1)
                for line in group.nodes
            ],
            "distributed_lines": distributed,
            "source_text_len": source_len,
            "translated_text_len": trans_len,
        }

        if source_len > 0 and trans_len / source_len > ratio_threshold and len(group.nodes) == 1:
            scale = min(1.0, font_scale_min * (source_len / trans_len) + (1 - source_len / trans_len))
            first = group.nodes[0][0]
            existing = first.get("style", "")
            first["style"] = f"{existing};font-size:{scale:.3f}em".strip(";")
            record["font_scale_applied"] = scale

        distribution_records.append(record)
        injected += 1

    cfg.distribution_json.write_text(
        json.dumps({"blocks": distribution_records}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    logger.info("  Checkpoint: %s", cfg.distribution_json)

    soup = apply_display_fixes(soup)
    cfg.translated_html.write_text(str(soup), encoding="utf-8")
    logger.info(
        "  Injected %d / %d groups (%d multi-line) → %s",
        injected,
        len(groups),
        multi_line_blocks,
        cfg.translated_html,
    )
    update_manifest(
        cfg,
        step_html={
            "injected": injected,
            "multi_line_blocks": multi_line_blocks,
            "distribution": str(cfg.distribution_json),
            "output": str(cfg.translated_html),
        },
    )
    return cfg.translated_html


def step_pdf(cfg: Config, logger: logging.Logger) -> Path:
    logger.info("Step 4: Converting HTML to PDF with Playwright")
    if not cfg.translated_html.exists():
        raise FileNotFoundError(f"Run --step html first. Missing {cfg.translated_html}")

    from playwright.sync_api import sync_playwright

    file_url = cfg.translated_html.resolve().as_uri()
    DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    cfg.checkpoint_dir.mkdir(parents=True, exist_ok=True)

    source_html = cfg.input_html if cfg.input_html.exists() else cfg.translated_html
    layout = parse_layout_maps(
        BeautifulSoup(source_html.read_text(encoding="utf-8", errors="replace"), "lxml"),
    )
    page_w = int(layout.page_width.get("w0", 774))
    page_h = int(layout.page_height.get("h0", 1095))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": page_w, "height": page_h})
        page.goto(file_url, wait_until="networkidle", timeout=120_000)
        try:
            page.emulate_media(media="print")
        except AttributeError:
            pass  # older playwright versions
        pdf_opts: dict[str, Any] = {
            "path": str(cfg.final_pdf),
            "print_background": cfg.print_background,
            "margin": {"top": "0", "right": "0", "bottom": "0", "left": "0"},
            "prefer_css_page_size": True,
        }
        if cfg.pdf_format.lower() == "auto":
            pdf_opts["width"] = f"{page_w}px"
            pdf_opts["height"] = f"{page_h}px"
        else:
            pdf_opts["format"] = cfg.pdf_format
        page.pdf(**pdf_opts)
        browser.close()

    shutil.copy2(cfg.final_pdf, cfg.output_pdf)
    logger.info("  Checkpoint: %s", cfg.final_pdf)
    logger.info("  Output: %s", cfg.output_pdf)
    update_manifest(
        cfg,
        step_pdf={"checkpoint": str(cfg.final_pdf), "output": str(cfg.output_pdf)},
    )
    return cfg.output_pdf


def run_pipeline(cfg: Config, from_step: str, logger: logging.Logger) -> None:
    cfg.checkpoint_dir.mkdir(parents=True, exist_ok=True)
    update_manifest(
        cfg,
        started_at=datetime.now(timezone.utc).isoformat(),
        target_lang=cfg.target_lang,
        source_lang=cfg.source_lang,
    )

    start_idx = STEP_ORDER.index(from_step) if from_step in STEP_ORDER else 0
    for step in STEP_ORDER[start_idx:]:
        if step == "group":
            step_group(cfg, logger)
        elif step == "translate":
            step_translate(cfg, logger)
        elif step == "html":
            step_html(cfg, logger)
        elif step == "pdf":
            step_pdf(cfg, logger)

    logger.info("Pipeline complete. Review checkpoints in %s", cfg.checkpoint_dir)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Translate pdf2htmlEX HTML to Georgian and export PDF (local, with checkpoints).",
    )
    parser.add_argument("--input", type=Path, default=None, help=f"Input HTML (default: {DEFAULT_INPUT})")
    parser.add_argument("--name", default=None, help="Checkpoint folder name (default: input stem)")
    parser.add_argument("--step", choices=STEPS, default="all", help="Run one step or all")
    parser.add_argument(
        "--from-step",
        choices=STEP_ORDER,
        default="group",
        help="When --step all, start from this step",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = load_config_from_env(args.input, args.name)
    logger = setup_logging(cfg.run_log)

    try:
        if args.step == "all":
            run_pipeline(cfg, args.from_step, logger)
        elif args.step == "group":
            step_group(cfg, logger)
        elif args.step == "translate":
            step_translate(cfg, logger)
        elif args.step == "html":
            step_html(cfg, logger)
        elif args.step == "pdf":
            step_pdf(cfg, logger)
    except Exception:
        logger.exception("Pipeline failed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
