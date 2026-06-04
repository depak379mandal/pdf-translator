#!/usr/bin/env node
/**
 * Render translated HTML to PDF using headless Chromium (layout as in browser).
 */

import fs from "fs/promises";
import path from "path";
import puppeteer from "puppeteer";

const WORKSPACE = "/workspace";
const INPUT_DIR = path.join(WORKSPACE, "translated");
const OUTPUT_DIR = path.join(WORKSPACE, "output");
const PRINT_BACKGROUND = process.env.PRINT_BACKGROUND !== "false";
const PDF_FORMAT = process.env.PDF_FORMAT || "A4";

async function listHtmlFiles(argv) {
  if (argv.length > 0) {
    return argv.map((name) => (name.endsWith(".html") ? name : `${name}.html`));
  }
  const entries = await fs.readdir(INPUT_DIR);
  return entries.filter((f) => f.endsWith(".html"));
}

async function renderOne(browser, htmlName) {
  const htmlPath = path.join(INPUT_DIR, htmlName);
  const outName = htmlName.replace(/\.html$/i, ".pdf");
  const pdfPath = path.join(OUTPUT_DIR, outName);

  await fs.access(htmlPath);
  await fs.mkdir(OUTPUT_DIR, { recursive: true });

  const page = await browser.newPage();
  const fileUrl = `file://${htmlPath}`;
  await page.goto(fileUrl, { waitUntil: "networkidle0", timeout: 120_000 });
  await page.emulateMediaType("print");

  const pdfOptions = {
    path: pdfPath,
    printBackground: PRINT_BACKGROUND,
    preferCSSPageSize: true,
    margin: { top: 0, right: 0, bottom: 0, left: 0 },
  };
  if (PDF_FORMAT && PDF_FORMAT !== "auto") {
    pdfOptions.format = PDF_FORMAT;
  }

  await page.pdf(pdfOptions);
  await page.close();
  console.log(`Rendered: ${htmlName} → output/${outName}`);
}

async function main() {
  const files = await listHtmlFiles(process.argv.slice(2));
  if (files.length === 0) {
    console.error("No HTML in /workspace/translated");
    process.exit(1);
  }

  const browser = await puppeteer.launch({
    headless: true,
    args: [
      "--no-sandbox",
      "--disable-setuid-sandbox",
      "--font-render-hinting=none",
    ],
  });

  try {
    for (const name of files) {
      await renderOne(browser, name);
    }
  } finally {
    await browser.close();
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
