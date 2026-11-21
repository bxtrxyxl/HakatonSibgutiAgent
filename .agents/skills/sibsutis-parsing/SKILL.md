---
name: sibsutis-parsing
description: >-
  Guide for SibSUTIS web scraping, PDF parsing, structured catalogs (programs,
  achievements, min_scores, site_sources), and site rebuild / autoupdate jobs.
  Use when modifying src/ingest.py, data/docs/, or PARSING.md.
---

# SibSUTIS Parsing & Data Pipeline Runbook

Read `PARSING.md` before changing any URL list, HTML selector, or PDF parser.

## CLI Commands

Always set `$env:PYTHONIOENCODING='utf-8'` on Windows before running `src.ingest`:

```powershell
$env:PYTHONIOENCODING='utf-8'
.venv\Scripts\python.exe -m src.ingest --dir data/docs          # curated Markdown KB
.venv\Scripts\python.exe -m src.ingest --programs               # data/programs.json (38 profiles)
.venv\Scripts\python.exe -m src.ingest --achievements           # data/achievements.json (App. 3 PDF)
.venv\Scripts\python.exe -m src.ingest --min-scores             # data/min_scores.json (App. 2 PDF)
.venv\Scripts\python.exe -m src.ingest --site-sources           # data/site_sources.json (~77 sources)
.venv\Scripts\python.exe -m src.ingest --file <path> --url <url> --title <title> --section <section> [--year 2026]
```

## Known Network & Data Invariants

- **`sibsutis.ru` disconnects intermittently** (`RemoteProtocolError: Server disconnected without sending a response`). This is external site behavior, not a code bug. `fetch_url` supports retries and Wayback fallback; log failures to `stats.json` and continue without crashing ingest.
- **`portal.sibsutis.ru` timeouts**: `discover_site_sources()` preserves previously discovered profiles/PDFs via `_previous_sources` instead of wiping `data/site_sources.json`.
- **PDF text layers**: `achievements.json` and `min_scores.json` depend on PDF text extraction via `pypdf`. If parsing returns empty, keep the previous valid JSON — never overwrite with empty data.
- **Full site rebuild safety**: `run_site_rebuild()` creates a backup first and rolls back automatically if 0 documents or fewer than half of the previous document count are collected.
- **Change detection**: autoupdate compares `_catalog_body_hash` (JSON excluding `generated_at`/`changed`) and `_file_hash`.
- **Master's programs**: `budget` / `paid` fields being `null` in `data/programs.json` is expected.
- **Chunking**: `chunk_markdown` defers orphan headings and keeps Markdown table rows intact with repeated headers; frontmatter anchors prevent duplicate `doc_id`.
