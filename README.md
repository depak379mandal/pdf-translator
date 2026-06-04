# PDF → HTML → Georgian → PDF (Docker)

Layout-preserving PDF translation pipeline using **pdf2htmlEX** in Docker (volume-synced `data/`), HTML text translation to Georgian (`ka`), and headless Chromium for PDF output.

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and Docker Compose v2
- On Apple Silicon Macs, Docker runs the pdf2htmlEX image under `linux/amd64` (configured in `docker-compose.yml`)

## Quick start

1. Copy environment defaults (optional):

   ```bash
   cp .env.example .env
   ```

2. Put your PDF in `data/input/`:

   ```bash
   cp /path/to/document.pdf data/input/document.pdf
   ```

3. Run the pipeline (see [Commands](#commands) below).

4. Collect the translated PDF from `data/output/document.pdf`.

Intermediate files:

| Directory | Contents |
|-----------|----------|
| `data/input/` | Source PDFs |
| `data/html/` | pdf2htmlEX output (HTML + assets) |
| `data/translated/` | Georgian HTML + copied assets |
| `data/output/` | Final PDFs |

## Commands

Recommended workflow: **Docker for PDF→HTML**, then **native Python** for translate + PDF (with checkpoints).

### One-time setup

```bash
cp .env.example .env          # edit GOOGLE_APPLICATION_CREDENTIALS or API key
make build                    # pull pdf2htmlEX image (Docker)
make install-native           # pip install -r requirements.txt && playwright install chromium
```

### Full pipeline (document.pdf)

```bash
# 1) PDF → text-only HTML (Docker)
./scripts/pdf-to-html.sh document

# 2) Group → translate → inject HTML → PDF (native, with checkpoints)
make native
```

Or without Make:

```bash
./scripts/pdf-to-html.sh document
python3 translator_pipeline.py
```

### Run steps individually

**Docker — PDF to HTML only:**

```bash
./scripts/pdf-to-html.sh document    # input: data/input/document.pdf → data/html/document.html
make pdf                             # same, default basename "document"
```

**Native — translate + PDF (review checkpoints between steps):**

```bash
python3 translator_pipeline.py --step group
python3 translator_pipeline.py --step translate
python3 translator_pipeline.py --step html
python3 translator_pipeline.py --step pdf
```

Or with Make:

```bash
make native-group
make native-translate
make native-html
make native-pdf
```

Resume after a failure or review:

```bash
python3 translator_pipeline.py --from-step translate
```

### Legacy Docker translate + render

```bash
make pipeline                      # ./scripts/pipeline.sh (all Docker steps)
./scripts/translate-html.sh
./scripts/html-to-pdf.sh
make translate
make pdf-out
```

## Volume sync (pdf2htmlEX)

The `data/` folder is mounted at `/workspace` inside every container. pdf2htmlEX reads from `/workspace/input` and writes to `/workspace/html`:

```bash
docker compose run --rm pdf2html \
  --zoom 1.3 \
  --dest-dir /workspace/html \
  /workspace/input/document.pdf
```

The helper script `scripts/pdf-to-html.sh` wraps this. Any files pdf2htmlEX creates beside the HTML (fonts, images) stay under `data/html/` and are copied to `data/translated/` during translation.

## Translation backends

| `TRANSLATOR` | Setup |
|--------------|--------|
| `google` (default) | Free via [deep-translator](https://github.com/nidhaloff/deep-translator); rate limits apply |
| `deepl` | Set `DEEPL_API_KEY` in `.env` |
| `gcp` | Mount service account JSON and set `GOOGLE_APPLICATION_CREDENTIALS` in the translator service |

Georgian font: **Noto Sans Georgian** is injected into the HTML `<head>` so Mkhedruli glyphs render instead of “tofu” boxes.

## Configuration

See `.env.example` for:

- `SOURCE_LANG` / `TARGET_LANG` (default `ka`)
- `PDF2HTML_ZOOM` — pdf2htmlEX scale (default `1.3`)
- `PDF_FORMAT` — Playwright PDF format (`A4` default; `auto` uses HTML page pixel size)
- `PAGE_SIDE_MARGIN` — right margin in px (defaults to left margin from `x0`)
- `LENGTH_RATIO_THRESHOLD` / `FONT_SCALE_MIN` — shrink font when translation is much longer than source

## pdf2htmlEX: text layer vs page images

By default, pdf2htmlEX may output **full-page PNG backgrounds** (`<img class="bi">`) with little selectable text. That happens when it cannot map PDF fonts and falls back to rasterizing each page.

Conversion produces **text-only HTML** (no background page images):

```bash
./scripts/pdf-to-html.sh document
```

You should see `✓ text nodes: 3000+, background images: 0`.

A post-processing step (`scripts/fix-html-display.py`) makes the text visible in the browser (pdf2htmlEX normally hides it with transparent colors). Logos and line art from the PDF are not included — only positioned text for translation.

If you get almost no text nodes, the PDF may be scanned — OCR it first.

## Layout limitations

- **Text expansion:** Georgian is often longer than English; the pipeline splits text across original line slots and caps line length. Very long clauses may still need manual review in `03_translated.html`.
- **Fonts:** Original PDF fonts may not include Georgian; Noto Sans Georgian is applied via CSS override.
- **Complex PDFs:** For best results, try [Google Translate Documents](https://translate.google.com/?op=docs) first; use this pipeline when you need a self-hosted or scriptable workflow.

## Make targets

```bash
make help              # list all targets
make build             # Docker: pull pdf2htmlEX
make pdf               # Docker: PDF → HTML
make install-native    # pip + Playwright Chromium
make native            # full native pipeline
make native-group | native-translate | native-html | native-pdf
make clean             # clear data/html, data/output, checkpoints
```

## Native Python pipeline (recommended for translate + PDF)

After HTML exists in `data/html/`, run locally **without Docker**. See [Commands](#commands) for copy-paste steps.

Install once:

```bash
make install-native
# or: pip install -r requirements.txt && playwright install chromium
```

Custom input name:

```bash
python3 translator_pipeline.py --input data/html/myfile.html --name myfile
```

### Checkpoint files

```
checkpoints/document/
  manifest.json
  01_paragraph_groups.json   # grouped source text (18px rule)
  02_translations.json       # source + Georgian per block
  03a_distribution.json      # per-line text split per block (review)
  03_translated.html         # layout-preserved HTML (open in browser)
  04_final.pdf
  run.log
```

**Layout:** Step `html` puts **one line of Georgian per original y-row** (reuses pdf2htmlEX positions, symmetric left/right margins, A4 PDF output).

Resume from a step: `python3 translator_pipeline.py --from-step translate`

## Project layout

```
├── translator_pipeline.py  # Native translate + PDF (Playwright)
├── requirements.txt
├── docker-compose.yml      # pdf2html only (Docker)
├── checkpoints/            # Per-run review artifacts (gitignored)
├── data/                   # input → html → output
└── scripts/                # pdf-to-html, fix-html-display
```
