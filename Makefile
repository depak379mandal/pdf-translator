.PHONY: help build pdf html translate pdf-out pipeline native native-group native-translate native-html native-pdf install-native clean

help:
	@echo "PDF → HTML → Translate (Georgian) → PDF"
	@echo ""
	@echo "Docker (pdf2html only):"
	@echo "  make build      Pull pdf2htmlEX image"
	@echo "  make pdf        PDF → HTML (text-only)"
	@echo ""
	@echo "Native Python (translate + PDF, uses .env):"
	@echo "  make install-native   pip + playwright chromium"
	@echo "  make native           Full pipeline with checkpoints"
	@echo "  make native-group     Step 1: paragraph grouping"
	@echo "  make native-translate Step 2: Google Cloud translate"
	@echo "  make native-html      Step 3: inject + HTML"
	@echo "  make native-pdf       Step 4: Playwright PDF"
	@echo ""
	@echo "Legacy Docker translate/render:"
	@echo "  make pipeline   Docker translate + render"

build:
	docker compose pull pdf2html
	docker compose build

pdf:
	./scripts/pdf-to-html.sh

translate:
	./scripts/translate-html.sh

pdf-out:
	./scripts/html-to-pdf.sh

pipeline:
	./scripts/pipeline.sh

install-native:
	pip install -r requirements.txt
	playwright install chromium

native:
	python translator_pipeline.py

native-group:
	python translator_pipeline.py --step group

native-translate:
	python translator_pipeline.py --step translate

native-html:
	python translator_pipeline.py --step html

native-pdf:
	python translator_pipeline.py --step pdf

clean:
	rm -f data/html/* data/translated/* data/output/*
	rm -rf checkpoints/*
	@touch data/html/.gitkeep data/translated/.gitkeep data/output/.gitkeep
