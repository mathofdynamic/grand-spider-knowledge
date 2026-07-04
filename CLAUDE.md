# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Grand Spider is a Flask API service that crawls a website and uses OpenAI models to synthesize it into a single knowledge-base document (Markdown) for chatbot/RAG use. Most application code lives in `grand_spider.py`, with two focused helper modules: `structured_data.py` (schema.org/JSON-LD/Open Graph harvesting) and `brand_kit.py` (deterministic logo/color/font/social extraction).

## Commands

```bash
pip install -r requirements.txt   # install dependencies
python grand_spider.py            # run the API on http://localhost:5000 (PORT env overrides)
```

Requires a `.env` file with `SERVICE_API_KEY` (auth for this API's endpoints, sent as the `api-key` header) and `OPENAI_API_KEY`. The service refuses to start without both. Optional: `OPENAI_MODEL_CHEAP` / `OPENAI_MODEL_STRONG` for tiered models (both default to `gpt-5-nano`), and `EXTRACTION_CACHE_TTL_SECONDS` (default 7 days; `0` disables the on-disk extraction cache in `cache/extractions/`).

### Tests

Fast offline unit tests for the pure helpers live in `tests/` (network- and LLM-free):

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

These cover the SSRF validator, `prefilter_candidate_urls`, `knowledge_path_score`, language detection, sitemap parsing, `structured_data.py`, `brand_kit.py`, and the extraction cache. Add to them when you touch a pure helper.

There is also an end-to-end batch harness (`test_cases/test_all_websites.py`) that starts the server as a subprocess, runs `/api/generate-knowledge-base` against ~9 real Persian e-commerce sites, and produces PDF/CSV reports. It takes 30–45 minutes and spends real OpenAI credits — do not run it casually. It needs extra deps not in `requirements.txt`: `reportlab`, `arabic_reshaper`, `python-bidi`, `markdown`, plus a Persian font file (`IRANSANS_MEDIUM_0.TTF`) in `test_cases/`.

To test a runtime change cheaply, run the server and hit the synchronous `/api/scrape-page` endpoint, or `/api/generate-knowledge-base` with a small `max_pages` and poll `GET /api/jobs/<job_id>`.

## Architecture

### Layout of grand_spider.py

The file is ordered roughly: imports/optional-dependency guards → config constants → cost tracking (`CostAccumulator`, `MODEL_PRICING`) → LLM helpers (`llm_chat`, `parse_json_response`) → HTML fetching/cleaning → language detection → legacy per-page OpenAI analysis functions → report saving → job runners → the deep-KB pipeline functions → Flask routes at the bottom.

### Job model

All long-running endpoints (`/api/generate-knowledge-base`, `/api/analyze-company`, `/api/qualify-prospects`) create a job in the in-memory `jobs` dict (guarded by `jobs_lock`), spawn a background `threading.Thread` running a `run_*_job` function, and return a job id immediately. Clients poll `GET /api/jobs/<job_id>`. Jobs are lost on restart — there is no persistence or queue. Progress messages are bilingual: every English progress string set via `update_job_progress` should have a Persian counterpart in `PROGRESS_MESSAGES_FA`.

`/api/scrape-page` is the exception: synchronous, returns extraction results directly.

### The deep knowledge-base pipeline (`run_knowledge_base_job`)

This is the core of the project. Stages, each a top-level function:

1. **Discovery** — `discover_all_candidate_urls`: sitemap parsing + crawl (Selenium fallback if `use_selenium`), capped at `MAX_DISCOVERY_URLS`.
2. **Deterministic pre-filter** — `prefilter_candidate_urls`: drops product/cart/checkout/asset/faceted URLs by pattern (`_ASSET_EXTENSIONS`, `_TRANSACTIONAL_PATH_HINTS`, etc.) before any LLM sees them. This is what lets 1000+-page sites stay affordable.
3. **AI page selection** — `select_knowledge_pages`: cheap model picks the most knowledge-rich pages within the `max_pages` budget (default 25, hard cap `MAX_KB_PAGE_BUDGET` = 80).
4. **Parallel extraction** — `extract_pages_parallel`: thread pool (`KB_EXTRACTION_WORKERS` = 6) fetches each page, converts HTML → clean markdown via `clean_text_from_html` (trafilatura → readability → BeautifulSoup fallback chain), and the cheap model extracts content and classifies it into one of the canonical sections in `KB_SECTIONS`.
5. **Per-section map-reduce synthesis** — `synthesize_section` on the strong model, batching chunks by `SECTION_SYNTH_INPUT_TOKEN_BUDGET` so no single call needs the whole site; this replaced an old single-call compile step that hit token limits.
6. **Assembly + audit** — `assemble_final_kb` (intro/TOC, compresses lowest-priority sections per `KB_SECTION_PRIORITY` if over `target_doc_tokens`), then `check_kb_completeness` plus a regex contact harvest (`harvest_contact_candidates`) that produces the `quality_report`.

`depth: "core"` bypasses stages 1–3 and uses the hardcoded path list in `discover_core_pages_only`. `identify_knowledge_rich_content_clusters` is the one legacy LLM helper still load-bearing — `select_knowledge_pages` calls it, but only for the ambiguous URLs left after `knowledge_path_score` auto-selects the obvious knowledge pages and when the remainder exceeds the page budget (otherwise the LLM selection call is skipped entirely).

### Cross-cutting subsystems added on top of the pipeline

- **SSRF guard** (`validate_public_http_url`): every caller-supplied URL is resolved and rejected if any resolved IP is non-public (loopback/RFC1918/link-local/metadata) or the scheme isn't http(s). Applied in all POST endpoints and to each `specific_pages` entry.
- **Shared HTTP session** (`SESSION`): pooled `requests.Session` with retry/backoff; **all** outbound HTTP goes through it. `get_robots_parser`/`robots_allows` enforce `robots.txt` `Disallow` in both crawlers, with a `CRAWL_DELAY_SECONDS` politeness delay.
- **Structured-data harvest** (`structured_data.py`): `extract_structured_data` runs per page during `extract_pages_parallel`; `merge_structured_facts` unions them; the result is fed into the contact/overview/FAQ sections as authoritative ground truth and stored in `comprehensive_analysis.structured_facts`.
- **Brand kit** (`brand_kit.py`): `build_brand_kit` measures logo/palette/fonts/socials deterministically (CSS + screenshot quantization + schema.org). It is the **primary** color source now; the old `extract_website_colors_with_openai` only runs as a fallback when nothing could be measured. Downloaded logos are validated by magic bytes (`_sniff_image_ext`) — never trust the URL extension or Content-Type, since SPA servers return HTML with 200 for missing icons.
- **Extraction cache** (`load_cached_extraction`/`store_cached_extraction`): keyed by `(url, lang, sha256(clean_text))`, so cache hits only occur when a page's cleaned content is byte-identical. Pages with rotating content (carousels) legitimately miss. Screenshot-augmented (homepage) extractions are never cached.
- **Job lifecycle**: create jobs via `register_job` (not raw `jobs[id] = ...`), which evicts finished jobs by TTL/count. All three runners set `started_at`/`finished_at` and have top-level try/except so a job can never stick in `running`.
- **LLM plumbing**: every OpenAI call goes through `llm_chat` (auto cost tracking) — do not call `openai_client.chat.completions.create` directly. Parse model JSON with `parse_json_response`.

### Output language

The knowledge base is written in the website's own language, not English. `detect_site_language` runs deterministic script-based detection first (`detect_language_deterministic` — reliable for Persian/Arabic) with an LLM fallback, on clean visible text rather than raw HTML. Persian/RTL is a first-class concern throughout (localized section titles in `KB_SECTION_TITLES_LOCALIZED`, `progress_fa`, Persian PDF rendering in the test harness).

### Cost tracking

Every LLM call goes through `llm_chat` and records usage into a `CostAccumulator`, which produces the per-model breakdown in `cost_estimation.by_model`. If you add a model or change models, update `MODEL_PRICING`. Token counting uses tiktoken via `count_tokens`.

### Optional dependencies

Selenium, PIL, trafilatura, and readability are all imported in try/except blocks with `*_AVAILABLE` flags; code must degrade gracefully when they're missing. Follow that pattern for any new optional dependency.

### Reports

Completed KB jobs auto-save to `reports/` as `knowledge_base_<domain>_<timestamp>_<jobid>.md` plus a `metadata_*.json` sidecar (`save_knowledge_base_report`). Existing files there are useful as examples of expected output shape.
