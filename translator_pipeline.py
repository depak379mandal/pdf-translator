#!/usr/bin/env python3
"""
Local PDF translation pipeline (no Docker):
  HTML → paragraph grouping (18px rule) → Google Cloud Translate → HTML → PDF

Checkpoint files are written under checkpoints/<name>/ for review between steps.
"""

from __future__ import annotations

import argparse
import html
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
FONT_SIZE_PATTERN = re.compile(
    r"\.(fs[0-9a-z]+)\s*\{[^}]*?font-size\s*:\s*(-?[0-9.]+)\s*(px|pt)",
    re.IGNORECASE,
)
MATRIX_SCALE_PATTERN = re.compile(
    r"\.(m[0-9a-z]+)\s*\{[^}]*?transform\s*:\s*matrix\(\s*(-?[0-9.]+)",
    re.IGNORECASE,
)
GEORGIAN_FONT_STYLE = """
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+Georgian&display=swap');
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
"""

FLOWING_DOCUMENT_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+Georgian&display=swap');
@page {
  size: A4;
  margin: 10mm 14mm 14mm 14mm;
}
html, body {
  margin: 0;
  padding: 0;
}
body {
  font-family: 'Noto Sans Georgian', Arial, sans-serif;
  font-size: 11pt;
  line-height: 1.45;
  color: #000;
  -webkit-print-color-adjust: exact;
  print-color-adjust: exact;
}
p {
  margin: 0;
  overflow-wrap: anywhere;
}
"""

HEADING_PATTERN = re.compile(
    r"^\d+(?:\.\d+)*\.?\s*(?:Article|მუხლი\b)",
    re.IGNORECASE,
)
FLOW_BODY_LEFT_PX = 26.0
FLOW_CENTER_X_RATIO = 0.38
FLOW_INDENT_DIVISOR = 14.0
FLOW_GAP_EM_FACTOR = 0.38
FLOW_EMPHASIS_FONT_PX = 12.5

# pdf2htmlEX duplicates coordinates in pt inside @media print; pt bottoms exceed page
# height and clip/squash text when Playwright uses print media.
PRINT_PT_COORD_RULE = re.compile(
    r"\.[yxwh][0-9a-z]+\{[^}]*(?:bottom|left|width|height)\s*:[^;}]*pt[^;}]*;?[^}]*\}",
    re.IGNORECASE,
)

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
    line_height: dict[str, float]
    font_size: dict[str, float]
    matrix_scale: dict[str, float]
    side_margin: float

    def max_text_width_px(self, node: Tag) -> float:
        page = _find_page_frame(node)
        pw = self.page_width.get(page, 774.0)
        left = self._node_left(node)
        right_pad = max(8.0, pw - max(self.x.values(), default=left) - 4.0)
        return max(80.0, pw - left - min(self.side_margin, right_pad))

    def _node_left(self, node: Tag) -> float:
        x_class = next((c for c in node.get("class", []) if c.startswith("x")), None)
        return self.x.get(x_class, self.side_margin) if x_class else self.side_margin


@dataclass
class PageReflowState:
    """Tracks rendered bottom on a page so the next block chains relatively."""

    last_bottom: float | None = None
    last_y_class: str | None = None


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


def _find_page_id(node: Tag) -> str:
    parent = node.parent
    while parent is not None:
        page_id = parent.get("id", "")
        if parent.name == "div" and page_id.startswith("pf"):
            return page_id
        parent = parent.parent
    return "pf0"


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
        line_height=_class_px_only(css_text, PAGE_HEIGHT_PATTERN),
        font_size=_class_px_only(css_text, FONT_SIZE_PATTERN),
        matrix_scale={
            cls: float(val) for cls, val in MATRIX_SCALE_PATTERN.findall(css_text)
        },
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


def distribute_proportional(group: ParagraphGroup, translated: str) -> list[str]:
    """Split translated text across original lines by proportional source capacity."""
    if not group.nodes:
        return []

    line_capacities: list[int] = []
    for line_nodes in group.nodes:
        line_str = " ".join(n.get_text(strip=True) for n in line_nodes)
        line_capacities.append(max(len(line_str), 1))

    total_capacity = sum(line_capacities)
    words = translated.split()
    if not words:
        return [""] * len(group.nodes)

    total_trans_chars = sum(len(w) for w in words) + max(0, len(words) - 1)
    distributed_lines: list[str] = []
    word_idx = 0

    for i in range(len(group.nodes)):
        if i == len(group.nodes) - 1:
            distributed_lines.append(" ".join(words[word_idx:]))
            break

        target_len = (line_capacities[i] / total_capacity) * total_trans_chars
        current_line_words: list[str] = []
        current_len = 0

        while word_idx < len(words):
            word = words[word_idx]
            if current_len + len(word) > target_len and current_len > 0:
                break
            current_line_words.append(word)
            current_len += len(word) + 1
            word_idx += 1

        distributed_lines.append(" ".join(current_line_words))

    while len(distributed_lines) < len(group.nodes):
        distributed_lines.append("")

    return distributed_lines


def _char_width_px(node: Tag, layout: LayoutMaps) -> float:
    fs_class = next((c for c in node.get("class", []) if c.startswith("fs")), None)
    m_class = next((c for c in node.get("class", []) if c.startswith("m")), None)
    font_size = layout.font_size.get(fs_class, 14.0)
    scale = layout.matrix_scale.get(m_class, 0.325)
    width_factor = float(os.getenv("GEORGIAN_WIDTH_FACTOR", "1.05"))
    return max(4.0, font_size * scale * 0.52 * width_factor)


def _source_line_gap(
    layout: LayoutMaps,
    y_top: str,
    y_bottom: str,
    default: float = 16.0,
) -> float:
    """Vertical distance between two source baselines (pdf2html bottom coords)."""
    top = layout.y.get(y_top)
    bottom = layout.y.get(y_bottom)
    if top is None or bottom is None:
        return default
    return max(12.0, top - bottom)


def _wrap_words_to_width(words: list[str], max_chars: int) -> list[str]:
    lines: list[str] = []
    current: list[str] = []
    current_len = 0
    for word in words:
        add_len = len(word) + (1 if current else 0)
        if current and current_len + add_len > max_chars:
            lines.append(" ".join(current))
            current = [word]
            current_len = len(word)
        else:
            current.append(word)
            current_len += add_len
    if current:
        lines.append(" ".join(current))
    return lines or [""]


def _remove_wrap_spill_nodes(anchor: Tag) -> None:
    for sib in list(anchor.next_siblings):
        if not isinstance(sib, Tag):
            continue
        if sib.name == "div" and "pipeline-wrap" in sib.get("class", []):
            sib.decompose()
            continue
        if sib.name == "div" and "t" in sib.get("class", []):
            break


def _set_node_bottom(node: Tag, layout: LayoutMaps, bottom_px: float) -> None:
    left = layout._node_left(node)
    node["style"] = f"bottom:{bottom_px:.3f}px;left:{left:.3f}px"


def _clone_wrap_node(soup: BeautifulSoup, template: Tag, text: str) -> Tag:
    spill = soup.new_tag("div")
    classes = list(template.get("class", []))
    if "pipeline-wrap" not in classes:
        classes.append("pipeline-wrap")
    spill["class"] = classes
    if text:
        spill.string = text
    return spill


def _wrap_step_for_slot(
    layout: LayoutMaps,
    y_classes: list[str],
    slot: int,
) -> float:
    if slot < len(y_classes) - 1:
        return _source_line_gap(layout, y_classes[slot], y_classes[slot + 1])
    if slot > 0:
        return _source_line_gap(layout, y_classes[slot - 1], y_classes[slot])
    return 16.0


def _anchor_bottom_for_line(
    layout: LayoutMaps,
    cursor: PageReflowState,
    y_class: str,
    *,
    cross_page: bool,
) -> float:
    """Place line relative to previous node on this page, preserving source y-gap."""
    original = layout.y.get(y_class, 0.0)
    if cross_page:
        return original
    if (
        cursor.last_bottom is not None
        and cursor.last_y_class
        and y_class
    ):
        gap = _source_line_gap(layout, cursor.last_y_class, y_class)
        return cursor.last_bottom - gap
    return original


def inject_distributed_lines(
    group: ParagraphGroup,
    distributed_lines: list[str],
    layout: LayoutMaps,
    soup: BeautifulSoup,
    page_states: dict[str, PageReflowState],
) -> list[dict[str, Any]]:
    """Inject with width wrap; each node chains below the previous on the same page."""
    wrap_records: list[dict[str, Any]] = []
    prev_page_id: str | None = None

    for i, line_nodes in enumerate(group.nodes):
        if not line_nodes:
            continue

        chunk = distributed_lines[i] if i < len(distributed_lines) else ""
        first = line_nodes[0]
        page_id = _find_page_id(first)
        cursor = page_states.setdefault(page_id, PageReflowState())
        _remove_wrap_spill_nodes(first)

        max_width = layout.max_text_width_px(first)
        char_w = _char_width_px(first, layout)
        safety = float(os.getenv("WRAP_SAFETY_FACTOR", "0.92"))
        max_chars = max(8, int((max_width / char_w) * safety))
        wrapped = _wrap_words_to_width(chunk.split(), max_chars) if chunk else [""]

        y_class = group.y_classes[i] if i < len(group.y_classes) else ""
        cross_page = i > 0 and prev_page_id is not None and page_id != prev_page_id
        anchor_bottom = _anchor_bottom_for_line(
            layout, cursor, y_class, cross_page=cross_page
        )
        wrap_step = _wrap_step_for_slot(layout, group.y_classes, i)

        first.clear()
        if wrapped[0]:
            first.string = wrapped[0]
        _set_node_bottom(first, layout, anchor_bottom)

        for node in line_nodes[1:]:
            node.clear()
            if node.get("style"):
                del node["style"]

        ref = first
        line_bottom = anchor_bottom
        for wi, line_text in enumerate(wrapped[1:], 1):
            spill = _clone_wrap_node(soup, first, line_text)
            spill_bottom = line_bottom - wrap_step
            _set_node_bottom(spill, layout, spill_bottom)
            ref.insert_after(spill)
            ref = spill
            line_bottom = spill_bottom

        cursor.last_bottom = line_bottom
        if y_class:
            cursor.last_y_class = y_class
        prev_page_id = page_id

        if len(wrapped) > 1:
            wrap_records.append(
                {
                    "slot": i,
                    "page_id": page_id,
                    "wrap_lines": len(wrapped),
                    "spill_rows": len(wrapped) - 1,
                    "anchor_bottom": anchor_bottom,
                    "wrap_step": wrap_step,
                    "max_chars": max_chars,
                    "cross_page": cross_page,
                }
            )

    return wrap_records


def _strip_print_pt_coordinates(css_text: str) -> str:
    """Drop pt-based layout rules inside @media print so px coordinates are used."""
    if "@media print" not in css_text:
        return css_text

    out: list[str] = []
    i = 0
    while i < len(css_text):
        start = css_text.find("@media print", i)
        if start == -1:
            out.append(css_text[i:])
            break
        out.append(css_text[i:start])
        brace = css_text.find("{", start)
        if brace == -1:
            out.append(css_text[start:])
            break
        depth = 0
        j = brace
        while j < len(css_text):
            ch = css_text[j]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        print_block = css_text[start : j + 1]
        out.append(PRINT_PT_COORD_RULE.sub("", print_block))
        i = j + 1
    return "".join(out)


def _page_sort_key(page_id: str) -> int:
    if page_id.startswith("pf"):
        suffix = page_id[2:] or "0"
        try:
            return int(suffix, 16)
        except ValueError:
            return 0
    return 0


def _sort_groups_visually(
    groups: list[ParagraphGroup],
    layout: LayoutMaps,
) -> list[ParagraphGroup]:
    """Order blocks top-to-bottom per page (matches source PDF reading order)."""
    return sorted(
        groups,
        key=lambda g: (
            _page_sort_key(_find_page_id(g.nodes[0][0])),
            -layout.y.get(g.y_classes[0], 0.0),
        ),
    )


def _is_heading_paragraph(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if HEADING_PATTERN.search(stripped):
        return True
    return len(stripped) < 72 and stripped.endswith(".") and stripped[0].isdigit()


def _same_page(prev: ParagraphGroup, curr: ParagraphGroup) -> bool:
    return _find_page_id(prev.nodes[-1][0]) == _find_page_id(curr.nodes[0][0])


def _vertical_gap_px(
    layout: LayoutMaps,
    top_y: str,
    bottom_y: str,
) -> float | None:
    top = layout.y.get(top_y)
    bottom = layout.y.get(bottom_y)
    if top is None or bottom is None:
        return None
    return abs(top - bottom)


def _line_font_px(node: Tag, layout: LayoutMaps) -> float:
    fs_class = next((c for c in node.get("class", []) if c.startswith("fs")), None)
    m_class = next((c for c in node.get("class", []) if c.startswith("m")), None)
    font_size = layout.font_size.get(fs_class, 42.0)
    scale = layout.matrix_scale.get(m_class, 0.325)
    return font_size * scale


def _gap_to_margin_em(gap_px: float) -> float:
    em = gap_px / 16.0 * FLOW_GAP_EM_FACTOR
    if gap_px < 20:
        return max(0.04, em)
    if gap_px > 55:
        return max(em, 0.75)
    return em


def _flow_line_style(
    group: ParagraphGroup,
    line_idx: int,
    prev_ref: tuple[ParagraphGroup, int] | None,
    layout: LayoutMaps,
    text: str,
) -> str:
    """Map source x/y coordinates to inline paragraph style (same rule everywhere)."""
    node = group.nodes[line_idx][0]
    styles: list[str] = []

    if prev_ref is not None:
        prev_group, prev_idx = prev_ref
        prev_node = prev_group.nodes[prev_idx][0]
        if _find_page_id(prev_node) == _find_page_id(node):
            prev_y = prev_group.y_classes[prev_idx]
            curr_y = group.y_classes[line_idx]
            gap = _vertical_gap_px(layout, prev_y, curr_y)
            if gap is not None:
                styles.append(f"margin-top:{_gap_to_margin_em(gap):.2f}em")

    left = layout._node_left(node)
    page = _find_page_frame(node)
    page_w = layout.page_width.get(page, 774.0)

    font_px = _line_font_px(node, layout)

    if left >= page_w * FLOW_CENTER_X_RATIO:
        styles.append("text-align:center")
        styles.append("font-weight:700")
        if font_px > 12.0:
            styles.append(f"font-size:{font_px / 12.0:.3f}em")
    elif left > FLOW_BODY_LEFT_PX + 25:
        indent = (left - FLOW_BODY_LEFT_PX) / FLOW_INDENT_DIVISOR
        styles.append(f"padding-left:{indent:.2f}em")
        styles.append("text-align:left")
    else:
        styles.append("text-align:justify")

    if _is_heading_paragraph(text):
        styles.append("font-weight:700")
    elif font_px >= FLOW_EMPHASIS_FONT_PX + 2.0:
        styles.append(f"font-size:{font_px / 12.0:.3f}em")

    return ";".join(styles)


def build_flowing_html(
    groups: list[ParagraphGroup],
    translations: dict[int, str],
    layout: LayoutMaps,
) -> str:
    """Build continuous HTML; one line per source row using coordinate rules only."""
    parts = [
        "<!DOCTYPE html>",
        '<html lang="ka">',
        "<head>",
        '<meta charset="utf-8"/>',
        "<title>Translated document</title>",
        "<style>",
        FLOWING_DOCUMENT_CSS,
        "</style>",
        "</head>",
        "<body>",
    ]

    ordered = _sort_groups_visually(groups, layout)
    prev_ref: tuple[ParagraphGroup, int] | None = None

    for group in ordered:
        translated = translations.get(group.block_id, "").strip()
        if not translated or not group.nodes:
            continue

        if len(group.nodes) == 1:
            line_texts = [translated]
        else:
            line_texts = distribute_proportional(group, translated)

        for line_idx, line_nodes in enumerate(group.nodes):
            if not line_nodes:
                continue
            line_text = line_texts[line_idx] if line_idx < len(line_texts) else ""
            line_text = line_text.strip()
            if not line_text:
                continue

            style = _flow_line_style(group, line_idx, prev_ref, layout, line_text)
            style_attr = f' style="{style}"' if style else ""
            parts.append(f"<p{style_attr}>{html.escape(line_text)}</p>")
            prev_ref = (group, line_idx)

    parts.extend(["</body>", "</html>"])
    return "\n".join(parts) + "\n"


def apply_display_fixes(soup: BeautifulSoup) -> BeautifulSoup:
    for style in soup.find_all("style"):
        if not style.string:
            continue
        text = style.string.replace("visibility:hidden", "visibility:visible")
        text = re.sub(r"color:\s*transparent", "color:#000", text, flags=re.IGNORECASE)
        text = _strip_print_pt_coordinates(text)
        style.string = text

    if soup.head is None:
        head = soup.new_tag("head")
        if soup.html:
            soup.html.insert(0, head)
        else:
            soup.insert(0, head)

    if not soup.find("style", id="pipeline-display-fix"):
        tag = soup.new_tag("style", id="pipeline-display-fix")
        soup.head.append(tag)

    fix_tag = soup.find("style", id="pipeline-display-fix")
    fix_tag.string = GEORGIAN_FONT_STYLE

    html = str(soup)
    html = re.sub(r'<img[^>]*class="bi[^"]*"[^>]*/?\s*>', "", html, flags=re.IGNORECASE)
    html = re.sub(r'class="pc (pc[0-9a-f]+)', r'class="pc opened \1', html)
    return BeautifulSoup(html, "lxml")


def step_html(cfg: Config, logger: logging.Logger) -> Path:
    logger.info("Step 3: Building flowing HTML (no page frames)")
    if not cfg.translations_json.exists():
        raise FileNotFoundError(f"Run --step translate first. Missing {cfg.translations_json}")

    translations = {
        b["block_id"]: b["translated"]
        for b in json.loads(cfg.translations_json.read_text(encoding="utf-8"))["blocks"]
        if b.get("status") == "ok" and b.get("translated")
    }

    soup = BeautifulSoup(cfg.input_html.read_text(encoding="utf-8", errors="replace"), "lxml")
    layout = parse_layout_maps(soup)
    groups = rebuild_groups_for_inject(soup, layout.y, cfg.gap_min, cfg.gap_max)

    distribution_records: list[dict] = []
    injected = 0
    multi_line_blocks = 0

    for group in groups:
        translated = translations.get(group.block_id)
        if not translated or not group.nodes:
            continue

        distributed = distribute_proportional(group, translated)
        source_len = len(group.source_text or "")
        trans_len = len(translated)
        if len(group.nodes) > 1:
            multi_line_blocks += 1

        distribution_records.append(
            {
                "block_id": group.block_id,
                "line_count": len(group.nodes),
                "line_capacities": [
                    max(len(" ".join(n.get_text(strip=True) for n in line)), 1)
                    for line in group.nodes
                ],
                "distributed_lines": distributed,
                "source_text_len": source_len,
                "translated_text_len": trans_len,
                "layout_mode": "flowing",
            }
        )
        injected += 1

    cfg.distribution_json.write_text(
        json.dumps({"blocks": distribution_records}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    logger.info("  Checkpoint: %s", cfg.distribution_json)

    flowing_html = build_flowing_html(groups, translations, layout)
    cfg.translated_html.write_text(flowing_html, encoding="utf-8")
    logger.info(
        "  Built flowing document: %d / %d paragraphs (%d multi-line) → %s",
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
            "layout_mode": "flowing",
            "distribution": str(cfg.distribution_json),
            "output": str(cfg.translated_html),
        },
    )
    return cfg.translated_html


def step_pdf(cfg: Config, logger: logging.Logger) -> Path:
    logger.info("Step 4: Converting flowing HTML to PDF with Playwright")
    if not cfg.translated_html.exists():
        raise FileNotFoundError(f"Run --step html first. Missing {cfg.translated_html}")

    from playwright.sync_api import sync_playwright

    file_url = cfg.translated_html.resolve().as_uri()
    DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    cfg.checkpoint_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
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
            pdf_opts["format"] = "A4"
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
