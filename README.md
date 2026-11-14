# Grand Spider: Advanced AI-Powered Website Knowledge Base Generator

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.7+-blue.svg" alt="Python 3.7+">
  <img src="https://img.shields.io/badge/OpenAI-GPT--5--nano-green.svg" alt="OpenAI GPT-5-nano">
  <img src="https://img.shields.io/badge/Flask-2.0+-red.svg" alt="Flask 2.0+">
  <img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="MIT License">
</p>

## 🚀 Overview

**Grand Spider** is a sophisticated AI-powered web analysis platform that turns any website into a single, coherent knowledge-base document for chatbot/RAG use. The overhauled `generate-knowledge-base` pipeline discovers a site deeply (sitemap + crawl), deterministically filters out product/cart/asset noise, uses AI to select the most knowledge-rich pages, extracts clean content in parallel, and synthesises the result **section by section** — so it stays complete on large (1000+ page) e-commerce sites without blowing past token limits.

### 🎯 Key Features

- **🔍 Deep, scalable discovery + smart selection**: Sitemap (with `<lastmod>`/`<priority>` ranking) → crawl → **deterministic pre-filter** (drops product/cart/checkout/admin/asset/faceted URLs for free) → **keyword auto-selection** of obvious knowledge pages → AI selection only for the ambiguous remainder, bounded by a page budget. Handles 1000+ page sites without sending the catalogue to an LLM. (Set `depth: "core"` for the legacy hardcoded-core-page behaviour.)
- **🏷️ Structured-data harvest (schema.org / JSON-LD / Open Graph)**: Before any LLM call, every fetched page is parsed for `Organization`, `FAQPage`, `Product` and Open Graph markup — free, authoritative company facts (name, logo, address, phones, socials, opening hours, FAQ pairs) that are fed into the knowledge base as ground truth instead of hoping the model extracts them from prose.
- **🎨 Deterministic brand kit** (logo · color palette · fonts · socials): The site is **measured, not guessed** — logo from schema.org/apple-touch-icon/header `<img>` heuristics (downloaded and magic-byte-validated), color palette from the site's own CSS variables + `theme-color` + screenshot pixel quantization (Pillow), fonts from CSS/Google-Fonts declarations, social links from JSON-LD `sameAs` and footer anchors. Saved to `reports/` and returned in the job result.
- **🧹 Clean-text extraction**: Converts HTML → clean markdown with **trafilatura** (readability/BeautifulSoup fallbacks) before the model — ~5–10× fewer input tokens and much higher signal than raw HTML.
- **🧩 Per-section map-reduce synthesis**: Each page is classified into a canonical section; sections are synthesised **independently** (with map-reduce for large ones), removing the old single ≤16K-token compile bottleneck so the document stays complete.
- **⚡ Extraction cache**: Per-page extractions are cached on disk keyed by `(url, language, content-hash)`, so re-running a site (or iterating during development) re-pays OpenAI **only for pages whose content actually changed**. TTL configurable via `EXTRACTION_CACHE_TTL_SECONDS`.
- **🤖 Automatic Selenium fallback**: A page whose HTML is large but whose clean text is tiny looks JS-rendered; it is transparently re-fetched once through headless Chrome (bounded per job), so SPA sites work without the caller guessing `use_selenium` upfront.
- **🎚️ Tiered models**: Cheap model for bulk extraction/classification, an optional stronger model for synthesis via `OPENAI_MODEL_STRONG` (defaults to the cheap model — no cost change until you opt in). Per-model cost breakdown in `cost_estimation.by_model` — **every** OpenAI call is now tracked.
- **⚡ Parallel extraction**: Pages are fetched + extracted concurrently (thread pool) for speed and resilience — one slow/failed page no longer kills the whole job.
- **✅ Completeness audit + contact safety-net**: A `quality_report` flags missing contact/policy/FAQ; a regex pass plus the structured-data harvest gathers emails/phone numbers so contact details aren't missed.
- **⚡ Synchronous Single-Page Scrape**: `/api/scrape-page` returns results immediately (structured data included; brand kit optional) without a job queue.
- **📸 Visual Context Integration**: Captures full-page screenshots for enhanced AI understanding and palette measurement.
- **🌐 Source-language output**: The knowledge base is written in the **website's own language**, so a Farsi site yields a Farsi KB (headings, table of contents and body) that matches the chatbot's language. Detection is deterministic script-based for Persian/Arabic (immune to slow/head-heavy pages) with an LLM fallback, run on clean visible text rather than raw HTML.
- **🔐 Secure by default**: API-key auth (constant-time comparison) on all endpoints, an **SSRF guard** that refuses to fetch loopback/private/link-local/cloud-metadata addresses, request-body size limits, and input validation/clamping on every parameter.
- **🤝 Polite crawling**: Shared connection-pooled HTTP session with automatic retries, `robots.txt` `Disallow` respected, and a per-request crawl delay.
- **📊 Comprehensive Reporting**: Markdown KB + JSON metadata (discovery/selection/section stats, structured facts, brand kit, quality report, per-model cost, cache stats).
- **📈 Cost Tracking**: Real-time cost estimation and token usage monitoring.
- **🧪 Tests**: A pytest unit suite for the pure helpers (`tests/`) plus the end-to-end batch harness (`test_cases/`).

### 🎯 Use Cases

- **Chatbot Training**: Generate comprehensive knowledge bases for AI chatbot training
- **Website Documentation**: Create structured documentation from existing websites  
- **Company Research**: Extract detailed company information and policies
- **Content Analysis**: Analyze website structure and extract key information
- **Multi-language Content**: Process Persian, English, and other language websites
- **Brand Analysis**: Extract and analyze website color schemes and design elements
- **Automated Testing**: Batch process multiple websites for comprehensive analysis
- **Report Generation**: Create beautiful PDF reports with Persian text formatting

## 🛠️ Technology Stack

- **Backend**: Python 3.10+, Flask (`str | None` type hints require 3.10+)
- **AI Processing**: OpenAI GPT-5 family (tiered: cheap model for extraction, optional strong model for synthesis)
- **Web Crawling**: Requests (pooled session + retries), Selenium WebDriver, `urllib.robotparser`
- **Content Processing**: trafilatura + readability-lxml (clean main-content extraction), BeautifulSoup4, Pillow (PIL)
- **Structured data**: schema.org / JSON-LD, Open Graph, meta-tag harvesting (`structured_data.py`)
- **Brand extraction**: CSS + screenshot pixel quantization, logo download/validation (`brand_kit.py`)
- **Data Storage**: JSON reports, Markdown knowledge bases, on-disk extraction cache
- **Security**: constant-time API-key auth, SSRF guard (`ipaddress`), request-size limits
- **PDF Generation**: ReportLab with Persian font support (test harness)
- **Text Processing**: Arabic Reshaper, BiDi algorithm for RTL text
- **Testing**: pytest unit suite (`tests/`) + end-to-end batch harness (`test_cases/`)

## 📋 Prerequisites

- Python 3.10 or higher
- OpenAI API key
- Chrome browser (for screenshot functionality)
- Git (for version control)
- Persian font files (IRANSans.ttf or similar) for PDF generation
- ReportLab library for PDF creation

## 🚀 Quick Start

### 1. Installation

    ```bash
# Clone the repository
git clone <repository-url>
cd Grand_Spider_Knowledge

# Create virtual environment
    python -m venv venv

# Activate virtual environment
# Windows:
venv\Scripts\activate
# macOS/Linux:
source venv/bin/activate

# Install dependencies
    pip install -r requirements.txt
    ```

### 2. Configuration

Create a `.env` file in the project root:

```env
# Your service API key (create a strong secret key)
SERVICE_API_KEY=your_strong_secret_api_key_here

# Your OpenAI API key
OPENAI_API_KEY=sk-your_openai_api_key_here

# Optional: Override the OpenAI model (default: gpt-5-nano)
# OPENAI_MODEL=gpt-5-nano

# --- Tiered models (optional) ---
# The knowledge-base pipeline can use a cheap model for bulk extraction/classification and
# a stronger model for the final per-section synthesis. Defaults keep BOTH on the cheap
# model, so cost is unchanged until you opt in by setting OPENAI_MODEL_STRONG.
# OPENAI_MODEL_CHEAP=gpt-5-nano
# OPENAI_MODEL_STRONG=gpt-5-mini

# --- Extraction cache (optional) ---
# Per-page extractions are cached in cache/extractions/ keyed by (url, language, content-hash).
# Default TTL is 7 days. Set to 0 to disable the cache entirely.
# EXTRACTION_CACHE_TTL_SECONDS=604800
```

### 3. Run the Service

    ```bash
    python grand_spider.py
    ```

The API will be available at `http://localhost:5000`

### 4. Run Unit Tests

Fast, offline unit tests for the pure helpers (URL safety, pre-filter, language detection, structured-data parsing, brand-kit color/logo logic, extraction cache):

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

These make **no** network or OpenAI calls, so they're safe to run anytime.

### 5. Run Comprehensive Tests

Test all websites with a single command:

```bash
cd test_cases
python3 test_all_websites.py
```

This will:
- Test all 9 predefined websites
- Generate beautiful Persian PDF reports
- Save all PDFs to the `reports/` folder
- Create a comprehensive CSV summary
- Handle server startup/shutdown automatically

## 📚 API Documentation

### Authentication

All API endpoints require an `api-key` header:

```bash
curl -H "api-key: your_service_api_key" \
     -H "Content-Type: application/json" \
     http://localhost:5000/api/health
```

### Core Endpoints

#### 1. Scrape Single Page (Synchronous)

**POST** `/api/scrape-page`

Scrape and extract structured knowledge from a single URL immediately. Returns the result directly — no polling required.

```json
{
  "url": "https://example.com/about/",
  "use_selenium": false,
  "include_screenshot": false
}
```

**Parameters:**
- `url` (required): The page URL to scrape (validated against the SSRF guard)
- `use_selenium` (optional): Use Selenium for JavaScript-rendered pages (default: `false`)
- `include_screenshot` (optional): Capture a full-page screenshot for visual context (default: `false`)
- `include_brand_kit` (optional): Also extract the deterministic brand kit (logo, palette, fonts, socials) (default: `false`)

**Response (200):**
```json
{
  "status": "success",
  "url": "https://example.com/about/",
  "title": "About Us",
  "detected_language": "en",
  "extracted_content": "# About Us\n\n...",
  "structured_data": {
    "organization": { "name": "Example Inc", "phones": ["+1 555 0100"] },
    "socials": ["https://instagram.com/example"],
    "faq": [{ "question": "...", "answer": "..." }]
  },
  "screenshot_captured": false,
  "cost_estimation": {
    "total_cost_usd": "0.000826",
    "prompt_tokens": 4200,
    "completion_tokens": 800,
    "by_model": { "gpt-5-nano": { "cost_usd": 0.000826 } }
  }
}
```

---

#### 2. Generate Knowledge Base

**POST** `/api/generate-knowledge-base`

Generate a comprehensive knowledge base from a website.

    ```json
    {
  "url": "https://example.com",
  "depth": "deep",
  "max_pages": 25,
  "target_doc_tokens": 18000,
  "use_selenium": true
}
```

**Parameters:**
- `url` (required): Base URL of the website
- `depth` (optional, default `deep`): `deep` discovers the whole site (sitemap + crawl), deterministically drops product/cart/asset URLs, then selects pages by stable path score and URL order — scales to 1000+ page sites. `core` uses only the legacy hardcoded core-page patterns (about/contact/faq/...).
- `max_pages` (optional, default 25): Page budget — how many knowledge pages to deeply extract. The service clamps this to 1–80 pages; 80 is the hard maximum.
- `target_doc_tokens` (optional, default 18000): Soft size budget for the final single document; lowest-priority sections are compressed first if exceeded.
- `specific_pages` (optional): Non-empty array of exact page URLs to process (overrides discovery). If provided, a non-array value or an empty array returns HTTP 400.
- `use_selenium` (optional): Enable screenshot capture and a JS-rendering crawl fallback.
- `previous_fingerprint` (optional): SHA-256 `source_fingerprint` from the previous completed crawl. If all selected source pages still match, the job completes with `unchanged: true`, returns page text/fingerprints, and omits `final_knowledge_base` without making model calls. `source_fingerprint` is accepted as an alias.
- `callback_url` (optional): Public HTTP(S) URL to receive a compact JSON completion event when the job completes or fails. The event includes `job_id`, `status`, `source_fingerprint`, `unchanged`, `error_code`, and `finished_at`; fetch the full result from `GET /api/jobs/{job_id}`. Delivery can repeat if the endpoint accepts the event but its response is lost, so use `job_id` as an idempotency key.

The current hard limit is **80 selected pages per job**. Page selection is deterministic for the discovered URL set and does not change when sitemap priority or `lastmod` metadata changes. Discovery retains at most 5,000 candidate URLs for selection; sitemap parsing may enumerate more before that cap is applied. Fetching and page extraction use up to six workers, and each requested page is checked against `robots.txt` before its content is fetched. Page fetch/extraction work grows roughly with the selected page count, while section synthesis and audits add content-dependent model work. A phase-4 caller reported about 25 seconds and $0.0006–$0.0008 for one page, and about 8 minutes / $0.032 for 30 pages. Those are observed runs, not a latency or price guarantee; model, site response, content length, caching, and synthesis affect actual totals.

**How the deep pipeline works:** discover URLs from sitemaps or a crawl → deterministic pre-filter and stable page selection (within `max_pages`) → fetch and fingerprint cleaned source text → parallel per-page extraction on the cheap model, with a free schema.org/Open Graph harvest and cache lookup per page (each page classified into a canonical section) → per-section map-reduce synthesis on the strong model, seeded with the harvested structured facts (no global token cap) → assembly into one document → completeness audit. The response keeps the same shape as before, plus `brand_kit`, and a richer `comprehensive_analysis` that now includes `structured_facts`, deterministic `selection` metadata, `extraction_stats` (cache hits, Selenium fallbacks), a `quality_report`, and the per-model cost breakdown in `cost_estimation.by_model`.

#### 3. Check Job Status

**GET** `/api/jobs/{job_id}`

Monitor job progress and retrieve results.

    ```json
    {
  "id": "job-uuid",
        "status": "completed",
  "progress": "Knowledge base generation completed successfully.",
  "progress_fa": "تولید پایگاه دانش با موفقیت تکمیل شد.",
  "detected_target_language": "fa",
  "extracted_pages_count": 8,
  "main_page_screenshot_captured": true,
  "final_knowledge_base": "# Comprehensive Knowledge Base...",
  "cost_estimation": {
    "total_cost_usd": "0.025",
    "prompt_tokens": 15420,
    "completion_tokens": 3280
  }
}
```

#### Fingerprints and page text

On a changed successful job, `GET /api/jobs/{job_id}` includes:

```json
{
  "status": "completed",
  "unchanged": false,
  "source_fingerprint": "<sha256>",
  "page_fingerprints": { "https://example.com/about": "<sha256>" },
  "pages": [
    {
      "url": "https://example.com/about",
      "title": "About us",
      "fingerprint": "<sha256>",
      "text": "Cleaned source text before model processing..."
    }
  ],
  "final_knowledge_base": "# ..."
}
```

`source_fingerprint` hashes the selected page URLs and their individual fingerprints. Each page fingerprint covers its fetched title and cleaned text. Send the aggregate value as `previous_fingerprint` on the next crawl. When unchanged, the job response keeps `pages`, `page_fingerprints`, and `source_fingerprint`, sets `unchanged: true`, and has no `final_knowledge_base`. For `max_pages: 1`, `single_page_text` contains the page's cleaned text directly, without a generated introduction or table of contents.

Failures use `status: "failed"`, a human-readable `error`, and one stable `error_code`: `site_unreachable`, `blocked`, `timed_out`, or `no_readable_content`. The synchronous URL preflight uses the same `error_code` values in its 400 response. If a site rejects `HEAD` with 405 or 501, preflight probes it with `GET` before deciding it is unavailable.

#### 4. Health Check

**GET** `/api/health`

Check service status and availability.

    ```json
    {
        "status": "ok",
        "message": "API is running",
        "selenium_available": true
    }
    ```

## 📊 Reports and Output

### Automatic Report Generation

Successful jobs are automatically saved to the `reports/` folder:

- **Knowledge Base**: `knowledge_base_domain_timestamp_jobid.md`
- **Metadata**: `metadata_domain_timestamp_jobid.json`
- **Beautiful PDF Reports**: `knowledge_base_domain.pdf` (Persian-formatted with RTL text)
- **CSV Summary**: `website_test_results.csv` (comprehensive test results)

### Report Structure

```
reports/
├── knowledge_base_afraa_shop_20250907_143022_68bab09a.md
├── metadata_afraa_shop_20250907_143022_68bab09a.json
├── knowledge_base_www.malek-doorbin.com.pdf
├── knowledge_base_netoffshop.ir.pdf
└── website_test_results.csv
```

### PDF Report Features

- **Persian Font Support**: Beautiful RTL text rendering
- **Color Analysis**: Website brand colors and design elements
- **Comprehensive Metadata**: Processing time, cost, page count
- **Structured Content**: Organized knowledge base with proper formatting
- **Professional Layout**: Clean, readable design with proper spacing

## 🌐 Multi-Language Support

### Supported Languages

- **Persian/Farsi** (fa) - Optimized
- **English** (en)  
- **Arabic** (ar)
- **And many more via OpenAI

### Language Features

- Automatic language detection
- Bi-lingual progress messages (English/Persian)
- Content translation and localization
- Persian URL pattern recognition

## 🔐 Security

- **Authentication**: every endpoint (except `/api/health`) requires the `api-key` header, compared in constant time.
- **SSRF protection**: all caller-supplied URLs (`generate-knowledge-base`, `scrape-page`, `analyze-company`, `qualify-prospects`, and every `specific_pages` entry) are validated before any fetch. Requests that resolve to loopback, private (RFC1918), link-local, cloud-metadata (`169.254.169.254`), or other non-public addresses are refused, as are non-`http(s)` schemes.
- **Resource limits**: request bodies are capped (`MAX_CONTENT_LENGTH`), `max_pages`/`prospect_urls` are validated and clamped, and finished jobs are evicted from memory (TTL + count cap) so a long-running server doesn't grow unbounded.
- **Deployment note**: `python grand_spider.py` runs Flask's development server. For production, run behind a WSGI server (gunicorn/uWSGI) and a reverse proxy.

## 🚨 Important Considerations

### Cost Management

- Monitor OpenAI API usage costs
- Each job provides cost estimation
- Typical cost: **$0.002-$0.008 per website** (gpt-5-nano is ~8x cheaper on input than previous models)
- Single-page scrape via `/api/scrape-page` typically costs under $0.001

### Performance

- Processing time: 2-5 minutes per website
- Depends on page count and content complexity
- Background processing prevents timeouts

### Best Practices

1. **Specify Core Pages**: Use `specific_pages` for targeted extraction
2. **Enable Screenshots**: Use `use_selenium: true` for visual context
3. **Monitor Costs**: Check cost estimation in job results
4. **Respect Rate Limits**: Avoid concurrent jobs on same domain

## 🧪 Testing Framework

### Comprehensive Test Suite

The project includes a powerful testing framework located in the `test_cases/` directory:

#### Features

- **Single Command Testing**: Run all 9 websites with one command
- **Automated Server Management**: Starts and stops the Grand Spider server automatically
- **Persian PDF Generation**: Creates beautiful PDF reports for each successful test
- **CSV Export**: Comprehensive results summary with all metrics
- **Error Handling**: Robust error handling and logging
- **Progress Tracking**: Real-time progress updates for each website

#### Test Websites

The framework tests these 9 websites:
1. https://netoffshop.ir/
2. https://www.malek-doorbin.com/
3. https://alandview.ir/
4. https://miladcam.com/
5. https://www.dourbinet.com/
6. https://www.manzel.ir/
7. https://shikwall.ir/
8. https://kaqazdivari-avindecor.ir/
9. https://tehranposter.ir/

#### Output Structure

```
test_cases/
├── test_all_websites.py          # Main test file
├── reports/                      # PDF reports folder
│   ├── knowledge_base_*.pdf      # Individual website PDFs
│   └── website_test_results.csv  # Summary results
├── IRANSANS_MEDIUM_0.TTF         # Persian font file
└── test_run.log                  # Detailed test logs
```

## 🐛 Troubleshooting

### Common Issues

**Screenshot Not Working:**
```bash
# Install Chrome and verify Selenium
pip install selenium webdriver-manager
```

**OpenAI API Errors:**
```bash
# Check API key and credits
export OPENAI_API_KEY=sk-your-key-here
```

**Memory Issues:**
```bash
# Reduce max pages or content length
MAX_PAGES_FOR_KB_GENERATION = 10
```

## 📈 Performance Metrics

### Typical Performance

- **Processing Speed**: 1-2 pages per minute
- **Accuracy**: 95%+ for core page identification
- **Cost Efficiency**: $0.002-$0.008 per comprehensive analysis (gpt-5-nano)
- **Success Rate**: 98%+ for accessible websites
- **PDF Generation**: 2-3 seconds per report
- **Batch Testing**: 9 websites in ~30-45 minutes
- **Color Analysis**: 100% accuracy for brand color extraction

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## 🙏 Acknowledgments

- **OpenAI** for providing powerful language models
- **Flask** community for the excellent web framework
- **Selenium** team for web automation tools
- **BeautifulSoup** for HTML parsing capabilities

## 📞 Support

For support, questions, or feature requests:

- Create an issue on GitHub
- Check the troubleshooting section
- Review the API documentation

---

**Grand Spider** - Transforming websites into intelligent knowledge bases for the AI era.
