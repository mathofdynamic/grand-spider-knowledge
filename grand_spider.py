import os
import contextvars
import hashlib
import hmac
import ipaddress
import logging
import requests
import functools
import socket
import threading
import time
import uuid
import json
import csv
import re
import collections
import concurrent.futures
import urllib.robotparser
import xml.etree.ElementTree as ET
from flask import Flask, request, jsonify
from openai import OpenAI, APIError, RateLimitError, APITimeoutError, APIConnectionError
from dotenv import load_dotenv
from urllib.parse import urlparse, urljoin, unquote
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from requests.utils import select_proxy
from urllib3.util.retry import Retry
import urllib3.connection as urllib3_connection
import urllib3.connectionpool as urllib3_connectionpool
import tiktoken

from structured_data import (extract_structured_data, merge_structured_facts,
                             structured_facts_summary_text)
from brand_kit import build_brand_kit

# --- Selenium Imports ---
try:
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service as ChromeService
    from selenium.webdriver.common.by import By
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import TimeoutException, WebDriverException, NoSuchElementException
    from webdriver_manager.chrome import ChromeDriverManager
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False
    logging.warning("Selenium or WebDriver Manager not installed. 'use_selenium' option will not be available.")

# --- Additional imports for screenshot functionality ---
try:
    import base64
    from PIL import Image
    from io import BytesIO
    SCREENSHOT_AVAILABLE = True
except ImportError:
    SCREENSHOT_AVAILABLE = False
    logging.warning("PIL not installed. Screenshot functionality will not be available.")

# --- Clean main-content extraction (HTML -> clean text/markdown) ---
# trafilatura gives the best boilerplate-free main content; readability is a fallback.
# Both are optional: if neither is installed we fall back to a BeautifulSoup text strip.
try:
    import trafilatura
    TRAFILATURA_AVAILABLE = True
except ImportError:
    TRAFILATURA_AVAILABLE = False
    logging.warning("trafilatura not installed. Falling back to readability/BeautifulSoup for content extraction.")

try:
    from readability import Document as ReadabilityDocument
    READABILITY_AVAILABLE = True
except ImportError:
    READABILITY_AVAILABLE = False


# --- Configuration & Initialization ---
load_dotenv()
app = Flask(__name__)
# Bound request bodies (JSON payloads / HTML file uploads) to prevent memory-exhaustion DoS.
app.config['MAX_CONTENT_LENGTH'] = 8 * 1024 * 1024
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(threadName)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- API Keys & Vercel AI Gateway Client ---
EXPECTED_SERVICE_API_KEY = os.getenv("SERVICE_API_KEY")
VERCEL_API_KEY = os.getenv("VERCEL_API_KEY")

if not EXPECTED_SERVICE_API_KEY:
    logger.error("FATAL: SERVICE_API_KEY environment variable not set.")
if not VERCEL_API_KEY:
    logger.error("FATAL: VERCEL_API_KEY environment variable not set.")

try:
    if VERCEL_API_KEY:
        openai_client = OpenAI(
            api_key=VERCEL_API_KEY,
            base_url="https://ai-gateway.vercel.sh/v1",
            timeout=120.0,
            max_retries=3
        )
        logger.info("Vercel AI Gateway client initialized successfully.")
    else:
        openai_client = None
        logger.error("Vercel AI Gateway client could not be initialized: VERCEL_API_KEY is missing.")
except Exception as e:
    logger.error(f"Failed to initialize Vercel AI Gateway client: {e}")
    openai_client = None

# --- Constants ---
REQUEST_TIMEOUT = 30
SELENIUM_PAGE_LOAD_TIMEOUT = 45
SELENIUM_RENDER_WAIT_SECONDS = 3
MAX_HTML_CONTENT_LENGTH = 3500000
MAX_HTML_SNIPPET_FOR_LANG_DETECT = 20000
MAX_CONTENT_LENGTH = 15000 # For simple text extraction

# --- Tiered model strategy ---
# Cheap model: bulk classification, per-page extraction, completeness checks.
# Strong model: final per-section synthesis & assembly where quality matters most.
# Defaults keep BOTH on GPT-6 Luna. Model IDs use Vercel AI Gateway's provider/model format.
OPENAI_MODEL_CHEAP = os.getenv("OPENAI_MODEL_CHEAP", os.getenv("OPENAI_MODEL", "openai/gpt-6-luna"))
OPENAI_MODEL_STRONG = os.getenv("OPENAI_MODEL_STRONG", OPENAI_MODEL_CHEAP)
# Backward-compatible alias used by existing (company analysis / prospecting) helpers.
OPENAI_MODEL = OPENAI_MODEL_CHEAP

# --- Token Limits for Different Tasks ---
# For Company Analysis & Prospecting (from Code 1)
MAX_RESPONSE_TOKENS_PAGE = 300
MAX_RESPONSE_TOKENS_SUMMARY = 500
MAX_RESPONSE_TOKENS_PROSPECT = 800
# For Knowledge Base Generation (from Code 2)
DEFAULT_TARGET_LANGUAGE = "en"
MAX_RESPONSE_TOKENS_LANG_DETECT = 50
MAX_RESPONSE_TOKENS_PAGE_SELECTION = 2000

# --- New pipeline tunables (overhauled KB generation) ---
MAX_CLEAN_TEXT_CHARS = 40000                 # cap clean main-content text per page before LLM
MAX_DISCOVERY_URLS = 5000                     # hard cap on URLs pulled from sitemap/crawl
DEFAULT_KB_PAGE_BUDGET = 25                   # default # of knowledge pages to deeply extract
MAX_KB_PAGE_BUDGET = 80                       # safety ceiling for a single job
KB_EXTRACTION_WORKERS = 6                     # parallel per-page extraction threads
MAX_RESPONSE_TOKENS_PAGE_EXTRACTION = 4000    # per-page structured extraction output
MAX_RESPONSE_TOKENS_SECTION_SYNTH = 8000      # per-section synthesis output (NOT a global cap)
MAX_RESPONSE_TOKENS_ASSEMBLY = 4000           # intro/overview + table of contents
MAX_RESPONSE_TOKENS_COMPLETENESS = 1200       # completeness critic
SECTION_SYNTH_INPUT_TOKEN_BUDGET = 14000      # sub-batch threshold for map-reduce within a section
TARGET_DOC_TOKENS = 18000                     # soft size budget for the final single-context doc

# Canonical sections for the final single-context document, in output order.
# Per-page extraction classifies each page into exactly one of these keys; each section is
# then synthesised independently so the document is never bottlenecked by one global call.
KB_SECTIONS = [
    ("company_overview", "Company Overview"),
    ("contact", "Contact Information"),
    ("products_services", "Products & Services"),
    ("policies", "Policies (Shipping / Returns / Warranty / Privacy / Terms)"),
    ("faq", "Frequently Asked Questions"),
    ("ordering_payment", "How to Order & Payment Methods"),
    ("support_howto", "Support, How-To & Troubleshooting"),
    ("additional", "Additional Information"),
]
KB_SECTION_KEYS = [k for k, _ in KB_SECTIONS]
KB_SECTION_TITLES = dict(KB_SECTIONS)
# Lowest-priority sections are compressed first when the doc exceeds TARGET_DOC_TOKENS.
KB_SECTION_PRIORITY = [
    "contact", "policies", "company_overview", "ordering_payment",
    "faq", "support_howto", "products_services", "additional",
]
# Localized section titles so the final document's headings/TOC match the site language.
KB_SECTION_TITLES_LOCALIZED = {
    "fa": {
        "company_overview": "معرفی شرکت",
        "contact": "اطلاعات تماس",
        "products_services": "محصولات و خدمات",
        "policies": "قوانین و سیاست‌ها (ارسال / بازگشت / گارانتی / حریم خصوصی / شرایط)",
        "faq": "پرسش‌های متداول",
        "ordering_payment": "نحوه سفارش و روش‌های پرداخت",
        "support_howto": "پشتیبانی، راهنما و عیب‌یابی",
        "additional": "اطلاعات تکمیلی",
    },
}

def section_title(key: str, lang: str) -> str:
    """Canonical section heading localized to the target language (falls back to English)."""
    loc = KB_SECTION_TITLES_LOCALIZED.get((lang or "").lower())
    if loc and key in loc:
        return loc[key]
    return KB_SECTION_TITLES.get(key, key)

# --- Crawling & Discovery Constants ---
CRAWLER_USER_AGENT = 'GrandSpiderMultiPurposeAnalyzer/2.0 (+http://yourappdomain.com/bot)'
MAX_PAGES_FOR_FALLBACK_DISCOVERY_CRAWL = 30
MAX_COMPANY_ANALYSIS_PAGES = 50        # upper clamp for /api/analyze-company crawls
MAX_PROSPECT_URLS_PER_JOB = 100        # upper clamp for /api/qualify-prospects
CRAWL_DELAY_SECONDS = 0.4              # politeness delay between crawl requests
SELENIUM_FALLBACK_MIN_TEXT = 400       # clean-text length below which a page looks JS-rendered
SELENIUM_FALLBACK_MIN_HTML = 20000     # ...but only when the raw HTML is substantial
SELENIUM_FALLBACK_MAX_PAGES = 5        # per-job cap on automatic Selenium re-fetches

# Shared HTTP session: connection pooling + retry-with-backoff on transient failures.
_pinned_public_ip = contextvars.ContextVar("grand_spider_pinned_public_ip", default=None)


class _PinnedAddressConnectionMixin:
    """Keep the URL hostname for Host/SNI while connecting to a checked IP."""

    def __init__(self, *args, pinned_ip=None, **kwargs):
        if not pinned_ip:
            raise ValueError("A pinned public IP is required")
        super().__init__(*args, **kwargs)
        self._pinned_ip = pinned_ip

    def _new_conn(self):
        # urllib3 uses _dns_host for socket creation and also as the public host
        # property. Swap it only while the socket is opened so TLS SNI and Host
        # headers continue to use the original URL hostname.
        original_dns_host = self._dns_host
        self._dns_host = self._pinned_ip
        try:
            return super()._new_conn()
        finally:
            self._dns_host = original_dns_host


class _PinnedHTTPConnection(_PinnedAddressConnectionMixin, urllib3_connection.HTTPConnection):
    pass


class _PinnedHTTPSConnection(_PinnedAddressConnectionMixin, urllib3_connection.HTTPSConnection):
    pass


class _PinnedHTTPConnectionPool(urllib3_connectionpool.HTTPConnectionPool):
    ConnectionCls = _PinnedHTTPConnection


class _PinnedHTTPSConnectionPool(urllib3_connectionpool.HTTPSConnectionPool):
    ConnectionCls = _PinnedHTTPSConnection


class _PublicIpPinningHTTPAdapter(HTTPAdapter):
    """Use the shared session while pinning explicitly guarded callback requests."""

    @staticmethod
    def _pinned_pool(url, pinned_ip, pool_kwargs=None):
        parsed = urlparse(url)
        pool_class = {
            "http": _PinnedHTTPConnectionPool,
            "https": _PinnedHTTPSConnectionPool,
        }.get(parsed.scheme)
        if pool_class is None or not parsed.hostname:
            raise requests.exceptions.InvalidURL("Pinned requests require an HTTP(S) URL")
        return pool_class(
            host=parsed.hostname,
            port=parsed.port,
            pinned_ip=pinned_ip,
            maxsize=1,
            block=True,
            **(pool_kwargs or {}),
        )

    def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
        pinned_ip = _pinned_public_ip.get()
        if not pinned_ip:
            parent_method = getattr(super(), "get_connection_with_tls_context", None)
            if parent_method:
                return parent_method(request, verify, proxies, cert)
            return super().get_connection(request.url, proxies)

        if select_proxy(request.url, proxies):
            raise requests.exceptions.RequestException(
                "Pinned public callbacks cannot be sent through an HTTP proxy"
            )
        host_params, pool_kwargs = self.build_connection_pool_key_attributes(request, verify, cert)
        return self._pinned_pool(request.url, pinned_ip, pool_kwargs)

    def get_connection(self, url, proxies=None):
        # Requests < 2.32 calls this older hook from HTTPAdapter.send.
        pinned_ip = _pinned_public_ip.get()
        if not pinned_ip:
            return super().get_connection(url, proxies)
        if select_proxy(url, proxies):
            raise requests.exceptions.RequestException(
                "Pinned public callbacks cannot be sent through an HTTP proxy"
            )
        return self._pinned_pool(url, pinned_ip)


SESSION = requests.Session()
_http_adapter = _PublicIpPinningHTTPAdapter(
    max_retries=Retry(total=2, backoff_factor=0.5, status_forcelist=(500, 502, 503, 504),
                      allowed_methods=frozenset(['GET', 'HEAD'])),
    pool_connections=20, pool_maxsize=20,
)
SESSION.mount('http://', _http_adapter)
SESSION.mount('https://', _http_adapter)
SESSION.headers.update({'User-Agent': CRAWLER_USER_AGENT})


def get_robots_parser(base_url: str):
    """Fetch and parse robots.txt for polite crawling. Returns None when unavailable
    (treat as allow-all)."""
    try:
        resp = SESSION.get(urljoin(base_url, "/robots.txt"), timeout=10)
        if resp.status_code == 200 and resp.text.strip():
            rp = urllib.robotparser.RobotFileParser()
            rp.parse(resp.text.splitlines())
            return rp
    except requests.exceptions.RequestException:
        pass
    return None


def robots_allows(rp, url: str) -> bool:
    if rp is None:
        return True
    try:
        return rp.can_fetch(CRAWLER_USER_AGENT, url)
    except Exception:
        return True

# --- Directories ---
REPORTS_DIR = "reports"

# --- Per-page extraction cache ---
# Re-running a site does not re-pay OpenAI for pages whose content hasn't changed:
# the cache key is (url, target language, hash of the fetched title and clean text).
EXTRACTION_CACHE_DIR = os.path.join("cache", "extractions")
EXTRACTION_CACHE_TTL_SECONDS = int(os.getenv("EXTRACTION_CACHE_TTL_SECONDS", 7 * 24 * 3600))


def _extraction_cache_path(url: str) -> str:
    return os.path.join(EXTRACTION_CACHE_DIR,
                        hashlib.sha256(url.encode('utf-8', 'ignore')).hexdigest() + ".json")


def load_cached_extraction(url: str, lang: str, content_hash: str):
    """Return the cached extraction dict for (url, lang, content) or None."""
    if EXTRACTION_CACHE_TTL_SECONDS <= 0:
        return None
    try:
        path = _extraction_cache_path(url)
        if not os.path.exists(path):
            return None
        with open(path, encoding='utf-8') as f:
            entry = json.load(f)
        if entry.get('lang') != lang or entry.get('content_hash') != content_hash:
            return None
        if time.time() - entry.get('ts', 0) > EXTRACTION_CACHE_TTL_SECONDS:
            return None
        return entry.get('data')
    except Exception:
        return None


def store_cached_extraction(url: str, lang: str, content_hash: str, data: dict, model: str):
    if EXTRACTION_CACHE_TTL_SECONDS <= 0:
        return
    try:
        os.makedirs(EXTRACTION_CACHE_DIR, exist_ok=True)
        with open(_extraction_cache_path(url), 'w', encoding='utf-8') as f:
            json.dump({'url': url, 'lang': lang, 'content_hash': content_hash,
                       'model': model, 'ts': time.time(), 'data': data},
                      f, ensure_ascii=False)
    except Exception as e:
        logger.debug(f"Extraction cache write failed for {url}: {e}")

# --- Progress Messages in Farsi ---
PROGRESS_MESSAGES_FA = {
    "Detecting language...": "تشخیص زبان...",
    "Discovering all pages (Sitemap)...": "کشف تمام صفحات (نقشه سایت)...",
    "Performing fallback crawl...": "انجام خزیدن پشتیبان...",
    "Identifying knowledge-rich content clusters...": "شناسایی خوشه‌های محتوای غنی از دانش...",
    "Extracting from company information pages...": "استخراج از صفحات اطلاعات شرکت...",
    "Extracting from educational content pages...": "استخراج از صفحات محتوای آموزشی...",
    "Extracting from troubleshooting support pages...": "استخراج از صفحات پشتیبانی عیب‌یابی...",
    "Extracting from buying guides pages...": "استخراج از صفحات راهنمای خرید...",
    "Extracting from technical explanations pages...": "استخراج از صفحات توضیحات فنی...",
    "Extracting from service information pages...": "استخراج از صفحات اطلاعات خدمات...",
    "Compiling comprehensive knowledge base...": "تدوین پایگاه دانش جامع...",
    "Knowledge base generation completed successfully.": "تولید پایگاه دانش با موفقیت تکمیل شد.",
    "Failed to generate knowledge base.": "تولید پایگاه دانش با شکست مواجه شد.",
    # New progress messages for improved system
    "Capturing main page screenshot...": "گرفتن تصویر کامل صفحه اصلی...",
    "Extracting website colors...": "استخراج رنگ‌های وب‌سایت...",
    "Discovering core website pages...": "کشف صفحات اصلی وب‌سایت...",
    "Extracting knowledge from core pages...": "استخراج دانش از صفحات اصلی...",
    # Overhauled deep pipeline
    "Reading source pages...": "\u062f\u0631 \u062d\u0627\u0644 \u062e\u0648\u0627\u0646\u062f\u0646 \u0635\u0641\u062d\u0627\u062a \u0645\u0646\u0628\u0639...",
    "Reading source page": "\u062f\u0631 \u062d\u0627\u0644 \u062e\u0648\u0627\u0646\u062f\u0646 \u0635\u0641\u062d\u0647 \u0645\u0646\u0628\u0639...",
    "Source pages are unchanged; skipping model processing.": "\u0635\u0641\u062d\u0627\u062a \u0645\u0646\u0628\u0639 \u062a\u063a\u06cc\u06cc\u0631 \u0646\u06a9\u0631\u062f\u0647\u200c\u0627\u0646\u062f\u061b \u067e\u0631\u062f\u0627\u0632\u0634 \u0645\u062f\u0644 \u0631\u062f \u0634\u062f.",
    "Fetching homepage...": "دریافت صفحه اصلی...",
    "Extracting knowledge from pages...": "استخراج دانش از صفحات...",
    "Extracting knowledge": "استخراج دانش از صفحات...",
    "Writing section": "در حال نوشتن بخش پایگاه دانش...",
    "Auditing knowledge base completeness...": "بررسی کامل بودن پایگاه دانش...",
}

def get_progress_fa(progress_en: str) -> str:
    """Get Farsi translation of progress message."""
    # Handle dynamic progress messages with page numbers
    if "Extracting from page" in progress_en and "/" in progress_en:
        # Extract page numbers from "Extracting from page X/Y: URL"
        try:
            parts = progress_en.split(":")
            if len(parts) >= 2:
                page_info = parts[0].strip()  # "Extracting from page X/Y"
                url_part = parts[1].strip()   # "URL"
                # Extract X/Y from the message
                if "Extracting from page" in page_info:
                    numbers = page_info.replace("Extracting from page", "").strip()
                    # Ensure the full URL is preserved
                    return f"استخراج از صفحه {numbers}: {url_part}"
        except Exception as e:
            logger.debug(f"Error processing progress message: {e}")
            pass
    
    # Handle other dynamic messages
    for key, value in PROGRESS_MESSAGES_FA.items():
        if key in progress_en:
            return value
    
    return PROGRESS_MESSAGES_FA.get(progress_en, progress_en)

def update_job_progress(job_id: str, progress_message: str):
    """Update job progress with both English and Farsi messages."""
    with jobs_lock:
        jobs[job_id]["progress"] = progress_message
        jobs[job_id]["progress_fa"] = get_progress_fa(progress_message)


def notify_job_callback(job_id: str):
    """Send a compact completion event; callback failures never change the job result."""
    with jobs_lock:
        job = dict(jobs.get(job_id) or {})
    callback_url = job.get("callback_url")
    if not callback_url:
        return

    payload = {
        "job_id": job_id,
        "status": job.get("status"),
        "source_fingerprint": job.get("source_fingerprint"),
        "unchanged": bool(job.get("unchanged", False)),
        "error_code": job.get("error_code"),
        "finished_at": job.get("finished_at"),
    }
    try:
        post_callback_event(callback_url, payload)
    except Exception as error:
        logger.warning(f"Completion callback failed for job {job_id}: {error}")

# --- Tokenizer and Pricing ---
try:
    try:
        TOKENIZER = tiktoken.encoding_for_model(OPENAI_MODEL)
    except KeyError:
        logger.warning(f"Tokenizer for model '{OPENAI_MODEL}' not found. Falling back to 'cl100k_base'.")
        TOKENIZER = tiktoken.get_encoding("cl100k_base")
except Exception as e:
    logger.error(f"Could not initialize tiktoken tokenizer: {e}. Token counting may be inaccurate.")
    TOKENIZER = None

# Prices are per 1 Million tokens (gpt-5-nano)
PRICE_PER_INPUT_TOKEN_MILLION = 0.05
PRICE_PER_OUTPUT_TOKEN_MILLION = 0.40
PRICE_PER_CACHED_INPUT_TOKEN_MILLION = 0.005

# Per-model pricing ($ per 1M tokens) for accurate tiered-cost accounting.
# Unknown models fall back to DEFAULT_MODEL_PRICING (nano rates).
DEFAULT_MODEL_PRICING = {"input": PRICE_PER_INPUT_TOKEN_MILLION, "output": PRICE_PER_OUTPUT_TOKEN_MILLION}
MODEL_PRICING = {
    "openai/gpt-6-luna": {"input": 0.10, "output": 0.50},
    "gpt-5-nano": {"input": 0.05, "output": 0.40},
    "gpt-5-mini": {"input": 0.25, "output": 2.00},
    "gpt-5": {"input": 1.25, "output": 10.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4o": {"input": 2.50, "output": 10.00},
}

def get_model_pricing(model_name: str) -> dict:
    """Return {input, output} $/1M for a model, tolerant of dated/suffixed variants."""
    if not model_name:
        return DEFAULT_MODEL_PRICING
    if model_name in MODEL_PRICING:
        return MODEL_PRICING[model_name]
    # Match the longest known prefix (so 'gpt-5-mini-2025...' beats 'gpt-5').
    best = None
    for key in MODEL_PRICING:
        if model_name.startswith(key) and (best is None or len(key) > len(best)):
            best = key
    return MODEL_PRICING[best] if best else DEFAULT_MODEL_PRICING


class CostAccumulator:
    """Thread-safe accumulator of LLM token usage across tiered models.

    Exposes aggregate prompt_tokens/completion_tokens (for the backward-compatible
    cost_estimation block) plus a per-model breakdown for accurate tiered pricing.
    """
    def __init__(self):
        self._lock = threading.Lock()
        self.by_model = {}  # model -> [prompt_tokens, completion_tokens]

    def add(self, model: str, prompt_tokens: int, completion_tokens: int):
        with self._lock:
            entry = self.by_model.setdefault(model or OPENAI_MODEL_CHEAP, [0, 0])
            entry[0] += int(prompt_tokens or 0)
            entry[1] += int(completion_tokens or 0)

    @property
    def prompt_tokens(self) -> int:
        with self._lock:
            return sum(v[0] for v in self.by_model.values())

    @property
    def completion_tokens(self) -> int:
        with self._lock:
            return sum(v[1] for v in self.by_model.values())

    def total_cost_usd(self) -> float:
        with self._lock:
            total = 0.0
            for model, (p, c) in self.by_model.items():
                pricing = get_model_pricing(model)
                total += (p / 1_000_000) * pricing["input"] + (c / 1_000_000) * pricing["output"]
            return total

    def breakdown(self) -> dict:
        with self._lock:
            out = {}
            for model, (p, c) in self.by_model.items():
                pricing = get_model_pricing(model)
                out[model] = {
                    "prompt_tokens": p,
                    "completion_tokens": c,
                    "cost_usd": round((p / 1_000_000) * pricing["input"]
                                      + (c / 1_000_000) * pricing["output"], 6),
                }
            return out

    def cost_estimation(self) -> dict:
        """Backward-compatible cost_estimation block + per-model breakdown."""
        return {
            "total_cost_usd": f"{self.total_cost_usd():.6f}",
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "by_model": self.breakdown(),
        }


def llm_chat(messages, model=None, max_tokens=2000, json_mode=False,
             cost: "CostAccumulator" = None, input_text_for_count=None):
    """Single Vercel AI Gateway chat call used by the overhauled KB pipeline.

    Returns (content_str, prompt_tokens, completion_tokens) and records usage into
    `cost` when provided. Uses the API-reported token usage when available.
    """
    if not openai_client:
        raise ConnectionError("Vercel AI Gateway client not initialized.")
    model = model or OPENAI_MODEL_CHEAP
    kwargs = {"model": model, "messages": messages, "max_completion_tokens": max_tokens}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    completion = openai_client.chat.completions.create(**kwargs)
    content = (completion.choices[0].message.content or "").strip()
    usage = completion.usage
    if usage:
        p_tokens = usage.prompt_tokens or 0
        c_tokens = usage.completion_tokens or 0
    else:
        p_tokens = count_tokens(input_text_for_count) if input_text_for_count else 0
        c_tokens = 0
    if cost is not None:
        cost.add(model, p_tokens, c_tokens)
    return content, p_tokens, c_tokens


def parse_json_response(content: str):
    """Best-effort JSON parse that tolerates markdown code fences around the object."""
    if not content:
        raise ValueError("Empty response content")
    text = content.strip()
    if '```json' in text:
        start = text.find('```json') + 7
        end = text.find('```', start)
        if end != -1:
            text = text[start:end].strip()
    elif '```' in text:
        start = text.find('```') + 3
        end = text.rfind('```')
        if end != -1 and end > start:
            text = text[start:end].strip()
    return json.loads(text)

def discover_core_pages_only(base_url: str, specific_pages: list = None) -> list[dict]:
    """
    Discover only core website pages that are essential for chatbot knowledge.
    If specific_pages is provided, use those URLs directly.
    Otherwise, try to find common core pages.
    """
    discovered_pages = []
    
    # If specific pages are provided, use only those URLs directly.
    if specific_pages:
        logger.info(f"Using provided specific pages: {len(specific_pages)} pages")
        for page_url in sorted(set(specific_pages)):
            discovered_pages.append({
                'url': page_url,
                'title': None,
                'type': 'specified_core_page'
            })
        
        return discovered_pages
    
    # If no specific pages provided, try to discover core pages automatically
    logger.info(f"Auto-discovering core pages for {base_url}")
    robots = get_robots_parser(base_url)
    
    # Always start with the main page
    discovered_pages.append({
        'url': base_url,
        'title': 'Main Page', 
        'type': 'main_page'
    })
    
    # Try to find standard core pages by common path patterns
    parsed_url = urlparse(base_url)
    base_path = f"{parsed_url.scheme}://{parsed_url.netloc}"
    
    # Common core page patterns to try
    core_patterns = [
        "/about/", "/about-us/", "/درباره-ما/",
        "/contact/", "/contact-us/", "/تماس-با-ما/", "/تماس/",
        "/terms/", "/terms-and-conditions/", "/قوانین/", "/شرایط/", "/قوانین-شرایط/",
        "/privacy/", "/privacy-policy/", "/حریم-خصوصی/",
        "/faq/", "/faqs/", "/سوالات-متداول/", "/پرسش-های-متداول/",
        "/help/", "/support/", "/پشتیبانی/", "/راهنما/",
        "/services/", "/خدمات/",
        "/returns/", "/return-policy/", "/بازگشت-کالا/",
        "/shipping/", "/delivery/", "/ارسال/",
        "/installments-rules/", "/اقساط/"
    ]
    
    for pattern in core_patterns:
        test_url = base_path + pattern
        if not robots_allows(robots, test_url):
            continue
        try:
            response = SESSION.head(test_url, timeout=5, allow_redirects=True)
            if response.status_code < 400:
                discovered_pages.append({
                    'url': test_url,
                    'title': pattern.strip('/').replace('-', ' ').title(),
                    'type': 'auto_discovered_core_page'
                })
                logger.info(f"✓ Found core page: {test_url}")
        except requests.exceptions.RequestException:
            # Silently continue - many URLs won't exist
            pass
    
    logger.info(f"Core page discovery completed: {len(discovered_pages)} pages found")
    return discovered_pages

# --- Job Management (Thread-Safe) ---
jobs = {}
jobs_lock = threading.Lock()
JOB_RETENTION_SECONDS = 24 * 3600  # evict finished jobs older than this
MAX_RETAINED_FINISHED_JOBS = 200   # hard cap on finished jobs kept in memory


def register_job(job_id: str, job_entry: dict):
    """Insert a new job, evicting old finished jobs so memory stays bounded."""
    now = time.time()
    with jobs_lock:
        def _finished():
            return [(jid, j) for jid, j in jobs.items()
                    if j.get("status") in ("completed", "failed")]
        for jid, j in _finished():
            if now - (j.get("finished_at") or j.get("created_at") or now) > JOB_RETENTION_SECONDS:
                del jobs[jid]
        finished = _finished()
        if len(finished) > MAX_RETAINED_FINISHED_JOBS:
            finished.sort(key=lambda kv: kv[1].get("finished_at") or kv[1].get("created_at") or 0)
            for jid, _ in finished[:len(finished) - MAX_RETAINED_FINISHED_JOBS]:
                del jobs[jid]
        jobs[job_id] = job_entry


# --- Authentication Decorator ---
def require_api_key(f):
    """Decorator to check for the presence and validity of the API key header."""
    @functools.wraps(f)
    def decorated_function(*args, **kwargs):
        incoming_api_key = request.headers.get('api-key')
        if not EXPECTED_SERVICE_API_KEY:
             logger.error("Internal Server Error: Service API Key is not configured.")
             return jsonify({"error": "Internal Server Error", "message": "Service API key not configured."}), 500
        if not incoming_api_key:
            logger.warning("Unauthorized access attempt: Missing API key")
            return jsonify({"error": "Unauthorized: Missing 'api-key' header"}), 401
        if not hmac.compare_digest(incoming_api_key.encode('utf-8'),
                                   EXPECTED_SERVICE_API_KEY.encode('utf-8')):
            logger.warning("Unauthorized access attempt: Invalid API key provided.")
            return jsonify({"error": "Unauthorized: Invalid API key"}), 401
        return f(*args, **kwargs)
    return decorated_function


def get_json_body():
    """Parse the request's JSON body. Returns (data, None) or (None, error_response)."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None, (jsonify({"error": "Request body must be a JSON object.",
                               "error_code": "invalid_request"}), 400)
    return data, None


# --- URL Safety (SSRF guard) ---
def resolve_public_http_url(url) -> tuple[list[str] | None, str | None]:
    """Resolve a public HTTP(S) URL and return its checked address list.

    Every resolved address must be public. Returning the addresses lets sensitive
    outbound calls connect to one of the checked IPs without a second DNS lookup.
    """
    if not url or not isinstance(url, str):
        return None, "A URL string is required."
    try:
        parsed = urlparse(url)
    except ValueError:
        return None, "Invalid URL."
    if parsed.scheme not in ("http", "https"):
        return None, "URL must use http or https."
    hostname = parsed.hostname
    if not hostname:
        return None, "URL has no hostname."
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addr_infos = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OverflowError, ValueError):
        return None, f"Could not resolve hostname '{hostname}'."
    addresses = []
    for info in addr_infos:
        try:
            address = info[4][0]
            ip = ipaddress.ip_address(address)
        except ValueError:
            return None, "URL resolves to an unparsable address."
        if not ip.is_global or ip.is_multicast:
            return None, "URL resolves to a non-public network address; refusing to fetch it."
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        return None, f"Could not resolve hostname '{hostname}'."
    return addresses, None


def validate_public_http_url(url) -> str | None:
    """Reject URLs that are not public http(s) targets (SSRF guard)."""
    _, error = resolve_public_http_url(url)
    return error


def url_validation_error_code(error: str) -> str:
    """Public code for a validate_public_http_url failure: DNS miss vs. refused target."""
    if error and error.startswith("Could not resolve hostname"):
        return "site_unreachable"
    return "url_refused"


def _submission_error(message: str, error_code: str = "invalid_request", **extra):
    """400 response for a rejected submission; every one carries an error_code."""
    return jsonify({"error": message, "error_code": error_code, **extra}), 400


def post_callback_event(callback_url: str, payload: dict):
    """POST through the shared session, pinned to a validated public address."""
    addresses, error = resolve_public_http_url(callback_url)
    if error:
        raise ValueError(f"Unsafe callback URL: {error}")

    # Requests converts international hostnames to IDNA ASCII before proxy
    # selection, so build the proxy bypass keys from its normalized URL.
    prepared_callback_url = requests.Request("POST", callback_url).prepare().url
    parsed_callback_url = urlparse(prepared_callback_url)
    callback_hostname = parsed_callback_url.hostname
    direct_connection_proxies = {
        "http": None,
        "https": None,
        "all": None,
        f"{parsed_callback_url.scheme}://{callback_hostname}": None,
        f"all://{callback_hostname}": None,
    }
    last_connection_error = None
    for address in addresses:
        pin_token = _pinned_public_ip.set(address)
        try:
            response = SESSION.post(
                callback_url,
                json=payload,
                timeout=10,
                allow_redirects=False,
                proxies=direct_connection_proxies,
            )
            response.raise_for_status()
            return response
        except requests.exceptions.ConnectionError as connection_error:
            # Try another checked address if this address is unreachable. A
            # response status error is not retried against another host.
            last_connection_error = connection_error
        finally:
            _pinned_public_ip.reset(pin_token)

    if last_connection_error:
        raise last_connection_error
    raise requests.exceptions.ConnectionError("No callback address could be reached")

# --- Helper Functions (Shared & Feature-Specific) ---

def count_tokens(text: str) -> int:
    """Counts tokens using the tiktoken library for better accuracy."""
    if TOKENIZER and text:
        try:
            return len(TOKENIZER.encode(text))
        except Exception:
            return 0
    return 0

# --- Feature: HTML Element/XPath Analysis (from Code 1) ---

def generate_xpath_for_element(element, soup):
    """Generate generic xpath queries that work across different users/profiles."""
    if not element or not element.name:
        return ""
    
    xpath_queries = []
    tag_name = element.name
    
    # 1. XPath by ID (only if generic/meaningful and stable)
    if element.get('id'):
        element_id = element.get('id')
        is_dynamic_id = (
            len(element_id) > 10 and any(char.isdigit() for char in element_id) or
            '__' in element_id or element_id.startswith('id_') or
            len([c for c in element_id if c.isdigit()]) > 3 or
            any(pattern in element_id.lower() for pattern in ['random', 'temp', 'gen', 'auto'])
        )
        stable_ids = ['react-root', 'app', 'main', 'header', 'footer', 'content', 'nav', 'menu']
        if (not is_dynamic_id and 
            (element_id in stable_ids or (len(element_id) < 8 and not any(char.isdigit() for char in element_id))) and
            not any(social_term in element_id.lower() for social_term in ['username', 'user_', 'profile_'])):
            xpath_queries.append(f"//{tag_name}[@id='{element_id}']")
    
    # 2. XPath by generic text patterns (avoid user-specific content)
    if element.get_text(strip=True):
        text = element.get_text(strip=True)
        action_words = ['follow', 'following', 'unfollow', 'like', 'share', 'comment', 'login', 'sign', 'submit', 'home', 
                       'profile', 'search', 'menu', 'save', 'edit', 'delete', 'add', 'create', 'more', 'view', 'show', 
                       'hide', 'close', 'open', 'next', 'previous', 'back', 'forward', 'up', 'down', 'settings', 
                       'options', 'message', 'send', 'posts', 'story', 'stories', 'reels', 'tagged']
        if (len(text) > 1 and len(text) < 30 and 
            not text.replace('.', '').replace('M', '').replace('K', '').replace(',', '').isdigit() and
            not any(char.isdigit() for char in text) and not '@' in text and
            not 'followers' in text.lower() and not 'following' in text.lower() and not 'posts' in text.lower() and
            any(word in text.lower() for word in action_words)):
            escaped_text = text.replace("'", "\\'")
            xpath_queries.append(f"//{tag_name}[contains(text(), '{escaped_text}')]")
            xpath_queries.append(f"//{tag_name}[text()='{escaped_text}']")

    # 3. XPath by semantic attributes
    semantic_attrs = {'role': ['button', 'link', 'menu', 'dialog', 'tab', 'navigation', 'main'], 'type': ['button', 'submit', 'search'],
                      'aria-label': None, 'data-testid': None, 'name': None, 'placeholder': None, 'alt': None, 'title': None}
    for attr, valid_values in semantic_attrs.items():
        if element.get(attr):
            attr_value = element.get(attr)
            if valid_values is None or attr_value in valid_values:
                if len(attr_value) < 50:
                    if attr == 'alt' and 'profile picture' in attr_value.lower():
                        xpath_queries.append(f"//{tag_name}[contains(@alt, 'profile picture')]")
                    elif not any(char.isdigit() for char in attr_value) and not '@' in attr_value:
                        xpath_queries.append(f"//{tag_name}[@{attr}='{attr_value}']")

    # (Simplified remaining XPath logic for brevity, full logic from original is complex)
    if element.get('href') and not any(user_indicator in element.get('href').lower() for user_indicator in ['/@', '/user/', '/profile/']):
        xpath_queries.append(f"//{tag_name}[@href='{element.get('href')}']")

    # Fallback to class if needed
    if element.get('class') and not xpath_queries:
        semantic_patterns = ['btn', 'button', 'nav', 'menu', 'header', 'footer', 'post', 'like', 'share', 'follow']
        for cls in element.get('class'):
            if any(pattern in cls.lower() for pattern in semantic_patterns) and len(cls) < 25:
                xpath_queries.append(f"//{tag_name}[contains(@class, '{cls}')]")
                break
    
    seen = set()
    return [x for x in xpath_queries if not (x in seen or seen.add(x))][:5]

def extract_all_elements(html_content: str) -> dict:
    """Extract all elements and their xpath queries from any HTML content."""
    soup = BeautifulSoup(html_content, 'html.parser')
    elements_map = {}
    logger.info(f"Found {len(soup.find_all())} total HTML tags in the page")
    
    element_categories = {
        'buttons': ['button', '[role="button"]', 'input[type="button"]', 'input[type="submit"]'],
        'links': ['a[href]'], 'inputs': ['input', 'textarea', 'select'], 'forms': ['form'],
        'images': ['img'], 'headings': ['h1', 'h2', 'h3'],
        'like_buttons': ['[aria-label*="like" i]', '[data-testid*="like"]'],
        'share_buttons': ['[aria-label*="share" i]', '[data-testid*="share"]'],
        'follow_buttons': ['[aria-label*="follow" i]', '[data-testid*="follow"]', 'button:contains("Follow")'],
        'follower_counts': ['[href*="/followers"]'], 'following_counts': ['[href*="/following"]'],
        'tweet_content': ['[data-testid="tweetText"]']
    }
    
    for element_name, selectors in element_categories.items():
        xpath_list = []
        for selector in selectors:
            try:
                # This is a simplified selector logic for the merged file. The original was very complex.
                found_elements = soup.select(selector)
                for element in found_elements[:5]: # Limit to avoid excessive processing
                    xpaths = generate_xpath_for_element(element, soup)
                    for xpath in xpaths:
                        if xpath and xpath not in xpath_list:
                            xpath_list.append(xpath)
            except Exception as e:
                logger.debug(f"Error processing selector '{selector}': {e}")
                continue
        if xpath_list:
            elements_map[element_name] = xpath_list
            logger.info(f"Found {len(xpath_list)} xpaths for {element_name}")
            
    logger.info(f"Total element types found: {len(elements_map)}")
    return elements_map

# --- Feature: Knowledge Base Generation (from Code 2) & General Crawling ---

def get_page_title_from_html(html_content) -> str | None:
    """Return the page's <title> text, or None when it has no usable title."""
    if not html_content:
        return None
    try:
        soup_title = BeautifulSoup(html_content, 'html.parser')
        title_tag = soup_title.find('title')
        title = title_tag.string.strip() if title_tag and title_tag.string else ""
        return title or None
    except Exception:
        return None


CRAWL_FAILURE_CODES = {
    "site_unreachable", "blocked", "timed_out", "no_readable_content",
}


class CrawlServiceError(ConnectionError):
    """A crawl error with a stable, client-facing failure code."""
    def __init__(self, message: str, error_code: str = "site_unreachable"):
        super().__init__(message)
        self.error_code = error_code if error_code in CRAWL_FAILURE_CODES else "site_unreachable"


def crawl_failure_code(error) -> str:
    """Map network and crawl failures to the small public error-code vocabulary."""
    current = error
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (requests.exceptions.Timeout, TimeoutError)):
            return "timed_out"
        response = getattr(current, "response", None)
        status_code = getattr(response, "status_code", None)
        if status_code in (401, 403, 429):
            return "blocked"
        if status_code in (408, 504):
            return "timed_out"
        if status_code:
            return "site_unreachable"
        current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)

    message = str(error or "").lower()
    if "timed out" in message or "timeout" in message:
        return "timed_out"
    if re.search(r"\b(401|403|429)\b", message) or "robots.txt disallow" in message:
        return "blocked"
    if "no readable" in message or "no extractable" in message:
        return "no_readable_content"
    return "site_unreachable"


def _http_error_code(status_code: int) -> str:
    if status_code in (401, 403, 429):
        return "blocked"
    if status_code in (408, 504):
        return "timed_out"
    return "site_unreachable"

def fetch_url_html_content(url: str, for_lang_detect=False) -> str | None:
    headers = {'User-Agent': CRAWLER_USER_AGENT}
    try:
        if for_lang_detect:
            with SESSION.get(url, timeout=15, allow_redirects=True, stream=True) as r:
                r.raise_for_status()
                if 'text/html' not in r.headers.get('Content-Type', '').lower(): return None
                r.encoding = r.apparent_encoding or 'utf-8'
                html_chunk = ""
                for chunk in r.iter_content(chunk_size=1024, decode_unicode=True):
                    if chunk and len(html_chunk) < MAX_HTML_SNIPPET_FOR_LANG_DETECT:
                        html_chunk += chunk
                return html_chunk
        else:
            response = SESSION.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            response.raise_for_status()
            response.encoding = response.apparent_encoding or 'utf-8'
            return response.text[:MAX_HTML_CONTENT_LENGTH]
    except requests.exceptions.RequestException as req_err:
        logger.error(f"Error fetching HTML for {url}: {req_err}")
        if not for_lang_detect:
            raise CrawlServiceError(
                f"Failed to fetch URL content: {req_err}", crawl_failure_code(req_err)
            ) from req_err
    return None

def clean_text_from_html(html: str, url: str = None) -> str:
    """Convert raw HTML into clean, boilerplate-free main content (markdown/text).

    Order of preference: trafilatura (best) -> readability -> BeautifulSoup body strip.
    This replaces shipping megabytes of raw HTML to the LLM: it cuts input tokens ~5-10x
    and raises quality by handing the model clean signal instead of tag soup.
    """
    if not html:
        return ""
    # 1) trafilatura — strong boilerplate removal, language-agnostic (handles fa/ar RTL).
    if TRAFILATURA_AVAILABLE:
        for fmt in ("markdown", "txt"):
            try:
                extracted = trafilatura.extract(
                    html, url=url, output_format=fmt,
                    include_tables=True, include_comments=False,
                    include_links=False, favor_recall=True,
                )
            except TypeError:
                # Older trafilatura that doesn't accept this output_format — try the next.
                continue
            except Exception as e:
                logger.debug(f"trafilatura extract failed for {url}: {e}")
                break
            if extracted and len(extracted.strip()) > 40:
                return extracted.strip()[:MAX_CLEAN_TEXT_CHARS]
    # 2) readability fallback.
    if READABILITY_AVAILABLE:
        try:
            doc = ReadabilityDocument(html)
            summary_html = doc.summary(html_partial=True)
            soup = BeautifulSoup(summary_html, 'html.parser')
            text = soup.get_text(separator='\n', strip=True)
            if text and len(text.strip()) > 40:
                return text.strip()[:MAX_CLEAN_TEXT_CHARS]
        except Exception as e:
            logger.debug(f"readability extract failed for {url}: {e}")
    # 3) Last-resort BeautifulSoup strip.
    try:
        soup = BeautifulSoup(html, 'html.parser')
        for tag in soup(["script", "style", "nav", "footer", "header", "noscript",
                         "meta", "link", "svg", "form", "iframe"]):
            tag.decompose()
        body = soup.body or soup
        text = body.get_text(separator='\n', strip=True)
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text[:MAX_CLEAN_TEXT_CHARS]
    except Exception as e:
        logger.error(f"All clean-text extraction failed for {url}: {e}")
        return ""

def fetch_url_content(url: str) -> str:
    """Fetches and extracts clean text content from a URL."""
    headers = {'User-Agent': CRAWLER_USER_AGENT}
    try:
        response = SESSION.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        response.raise_for_status()
        if 'text/html' not in response.headers.get('Content-Type', '').lower():
            logger.warning(f"URL {url} is not HTML content.")
            return ""
        response.encoding = response.apparent_encoding or 'utf-8'
        soup = BeautifulSoup(response.text, 'html.parser')
        for script_or_style in soup(["script", "style"]):
            script_or_style.decompose()
        body_text = soup.body.get_text(separator='\n', strip=True) if soup.body else ""
        return body_text[:MAX_CONTENT_LENGTH]
    except requests.exceptions.RequestException as req_err:
        raise ConnectionError(f"Failed to fetch URL text content: {req_err}")


def get_sitemap_entries_from_xml(xml_content: str):
    """Parse one sitemap document.

    Returns (page_entries, nested_sitemap_urls) where each page entry is
    {'url', 'lastmod', 'priority'} — lastmod/priority feed page ranking later.
    """
    entries, nested = [], []
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError as e:
        logger.error(f"Failed to parse sitemap XML: {e}")
        return entries, nested

    def _local(tag):
        return tag.rsplit('}', 1)[-1].lower() if isinstance(tag, str) else ''

    for el in root.iter():
        name = _local(el.tag)
        if name not in ('url', 'sitemap'):
            continue
        loc, lastmod, priority = None, None, None
        for child in el:
            cname = _local(child.tag)
            text = (child.text or '').strip()
            if cname == 'loc':
                loc = text
            elif cname == 'lastmod':
                lastmod = text
            elif cname == 'priority':
                try:
                    priority = float(text)
                except ValueError:
                    pass
        if not loc:
            continue
        if name == 'sitemap':
            nested.append(loc)
        else:
            entries.append({'url': loc, 'lastmod': lastmod, 'priority': priority})
    return entries, nested

def find_sitemap_urls(base_url: str):
    """Collect page URLs from all reachable sitemaps.

    Returns (urls, meta_by_url) where meta_by_url maps url -> {'lastmod', 'priority'}.
    """
    logger.info(f"Attempting to find sitemaps for {base_url}")
    sitemap_paths_to_check = collections.deque()
    final_page_urls = set()
    meta_by_url = {}
    processed_sitemap_urls = set()
    try:
        robots_url = urljoin(base_url, "/robots.txt")
        response = SESSION.get(robots_url, timeout=10)
        if response.status_code == 200:
            for line in response.text.splitlines():
                if line.strip().lower().startswith("sitemap:"):
                    sitemap_url = line.split(":", 1)[1].strip()
                    if sitemap_url not in processed_sitemap_urls:
                        sitemap_paths_to_check.append(sitemap_url)
                        processed_sitemap_urls.add(sitemap_url)
    except requests.exceptions.RequestException: pass
    
    common_sitemaps = ["/sitemap.xml", "/sitemap_index.xml"]
    for common_path in common_sitemaps:
        sitemap_url = urljoin(base_url, common_path)
        if sitemap_url not in processed_sitemap_urls:
            sitemap_paths_to_check.append(sitemap_url)
            processed_sitemap_urls.add(sitemap_url)

    while sitemap_paths_to_check:
        sitemap_url = sitemap_paths_to_check.popleft()
        try:
            response = SESSION.get(sitemap_url, timeout=15)
            if response.status_code == 200:
                content_type = response.headers.get('Content-Type', '').lower()
                if 'xml' in content_type:
                    entries, nested = get_sitemap_entries_from_xml(response.text)
                    for sm_url in nested:
                        if sm_url not in processed_sitemap_urls:
                            sitemap_paths_to_check.append(sm_url)
                            processed_sitemap_urls.add(sm_url)
                    for entry in entries:
                        ext_url = entry['url']
                        if ext_url.endswith('.xml'):
                            if ext_url not in processed_sitemap_urls:
                                sitemap_paths_to_check.append(ext_url)
                                processed_sitemap_urls.add(ext_url)
                            continue
                        final_page_urls.add(ext_url)
                        if entry.get('lastmod') or entry.get('priority') is not None:
                            meta_by_url[ext_url] = {'lastmod': entry.get('lastmod'),
                                                    'priority': entry.get('priority')}
        except requests.exceptions.RequestException: pass
    logger.info(f"Found {len(final_page_urls)} unique page URLs from sitemaps.")
    return list(final_page_urls), meta_by_url


def simple_crawl_website(base_url, max_pages=10):
    logger.info(f"Starting simple crawl for {base_url}, max_pages={max_pages}")
    urls_to_visit = collections.deque([base_url])
    queued_urls = {base_url}
    visited_urls = set()
    found_pages_details = []
    base_domain = urlparse(base_url).netloc
    robots = get_robots_parser(base_url)
    while urls_to_visit and len(found_pages_details) < max_pages:
        current_url = urls_to_visit.popleft()
        if current_url in visited_urls or urlparse(current_url).netloc != base_domain:
            continue
        visited_urls.add(current_url)
        if not robots_allows(robots, current_url):
            logger.info(f"[Simple] Skipping robots-disallowed URL: {current_url}")
            continue
        if found_pages_details:
            time.sleep(CRAWL_DELAY_SECONDS)
        try:
            response = SESSION.get(current_url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            response.raise_for_status()
            if response.status_code == 200 and 'text/html' in response.headers.get('Content-Type', '').lower():
                found_pages_details.append({'url': current_url, 'status': 'found'})
                logger.info(f"[Simple] Found page ({len(found_pages_details)}/{max_pages}): {current_url}")
                soup = BeautifulSoup(response.text, 'html.parser')
                discovered_links = set()
                for link in soup.find_all('a', href=True):
                    absolute_url = urljoin(base_url, link['href'])
                    absolute_url = urlparse(absolute_url)._replace(fragment="").geturl()
                    if urlparse(absolute_url).netloc == base_domain:
                        discovered_links.add(absolute_url)
                for absolute_url in sorted(discovered_links):
                    if absolute_url not in visited_urls and absolute_url not in queued_urls:
                        urls_to_visit.append(absolute_url)
                        queued_urls.add(absolute_url)
        except requests.exceptions.RequestException as e:
            logger.error(f"[Simple] Error crawling URL {current_url}: {e}")
    return found_pages_details


def selenium_crawl_website(base_url, max_pages=10):
    if not SELENIUM_AVAILABLE: raise RuntimeError("Selenium is not available.")
    logger.info(f"Starting Selenium crawl for {base_url}, max_pages={max_pages}")
    urls_to_visit = collections.deque([base_url])
    queued_urls = {base_url}
    visited_urls = set()
    found_pages_details = []
    base_domain = urlparse(base_url).netloc
    robots = get_robots_parser(base_url)
    driver = None
    try:
        chrome_options = ChromeOptions()
        chrome_options.add_argument("--headless")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument(f"user-agent={CRAWLER_USER_AGENT}")
        service = ChromeService(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=chrome_options)
        driver.set_page_load_timeout(SELENIUM_PAGE_LOAD_TIMEOUT)
        while urls_to_visit and len(found_pages_details) < max_pages:
            current_url = urls_to_visit.popleft()
            if current_url in visited_urls or urlparse(current_url).netloc != base_domain:
                continue
            visited_urls.add(current_url)
            if not robots_allows(robots, current_url):
                logger.info(f"[Selenium] Skipping robots-disallowed URL: {current_url}")
                continue
            try:
                driver.get(current_url)
                WebDriverWait(driver, SELENIUM_PAGE_LOAD_TIMEOUT).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
                time.sleep(SELENIUM_RENDER_WAIT_SECONDS)
                page_title = driver.title.strip() or None
                page_html = driver.page_source
                found_pages_details.append({'url': current_url, 'title': page_title, 'status': 'found_by_selenium', 'html_source': page_html})
                logger.info(f"[Selenium] Found page ({len(found_pages_details)}/{max_pages}): {current_url}")
                discovered_links = set()
                links = driver.find_elements(By.TAG_NAME, 'a')
                for link in links:
                    href = link.get_attribute('href')
                    if href:
                        absolute_url = urljoin(current_url, href)
                        absolute_url = urlparse(absolute_url)._replace(fragment="").geturl()
                        if urlparse(absolute_url).netloc == base_domain:
                            discovered_links.add(absolute_url)
                for absolute_url in sorted(discovered_links):
                    if absolute_url not in visited_urls and absolute_url not in queued_urls:
                        urls_to_visit.append(absolute_url)
                        queued_urls.add(absolute_url)
            except (TimeoutException, WebDriverException) as e:
                logger.error(f"[Selenium] Error for URL {current_url}: {e}")
    finally:
        if driver: driver.quit()
    return found_pages_details

def capture_full_page_screenshot(url: str) -> str | None:
    """Capture a full page screenshot and return as base64 encoded string."""
    if not SELENIUM_AVAILABLE or not SCREENSHOT_AVAILABLE:
        logger.warning("Screenshot functionality not available - missing Selenium or PIL")
        return None
    
    driver = None
    try:
        chrome_options = ChromeOptions()
        chrome_options.add_argument("--headless")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--window-size=1920,1080")
        chrome_options.add_argument(f"user-agent={CRAWLER_USER_AGENT}")
        
        service = ChromeService(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=chrome_options)
        driver.set_page_load_timeout(SELENIUM_PAGE_LOAD_TIMEOUT)
        
        # Navigate to the URL
        driver.get(url)
        WebDriverWait(driver, SELENIUM_PAGE_LOAD_TIMEOUT).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(SELENIUM_RENDER_WAIT_SECONDS)
        
        # Get the full page height and set window size
        total_height = driver.execute_script("return document.body.scrollHeight")
        driver.set_window_size(1920, total_height)
        time.sleep(2)  # Wait for resize
        
        # Take screenshot
        screenshot_png = driver.get_screenshot_as_png()
        
        # Convert to base64
        screenshot_base64 = base64.b64encode(screenshot_png).decode('utf-8')
        logger.info(f"Successfully captured full page screenshot for {url}")
        return screenshot_base64
        
    except Exception as e:
        logger.error(f"Failed to capture screenshot for {url}: {e}")
        return None
    finally:
        if driver:
            driver.quit()


def fetch_page_html_via_selenium(url: str) -> str | None:
    """Fetch one page's rendered HTML with headless Chrome (for JS-heavy pages)."""
    if not SELENIUM_AVAILABLE:
        return None
    driver = None
    try:
        chrome_options = ChromeOptions()
        chrome_options.add_argument("--headless")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument(f"user-agent={CRAWLER_USER_AGENT}")
        service = ChromeService(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=chrome_options)
        driver.set_page_load_timeout(SELENIUM_PAGE_LOAD_TIMEOUT)
        driver.get(url)
        WebDriverWait(driver, SELENIUM_PAGE_LOAD_TIMEOUT).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(SELENIUM_RENDER_WAIT_SECONDS)
        return driver.page_source
    except (TimeoutException, WebDriverException) as e:
        logger.warning(f"Selenium fetch failed for {url}: {e}")
        return None
    finally:
        if driver:
            driver.quit()


def scrape_single_page(url: str, use_selenium: bool = False, include_screenshot: bool = False,
                       include_brand_kit: bool = False) -> dict:
    """Synchronously scrape and extract structured knowledge from a single URL."""
    # Validate URL accessibility
    try:
        head_resp = SESSION.head(url, timeout=10, allow_redirects=True)
        if head_resp.status_code >= 400:
            raise ValueError(f"URL returned HTTP {head_resp.status_code}")
    except requests.exceptions.RequestException as e:
        raise ConnectionError(f"URL is not accessible: {e}") from e

    # Fetch HTML
    if use_selenium:
        if not SELENIUM_AVAILABLE:
            raise RuntimeError("Selenium is not available on this server.")
        html = fetch_page_html_via_selenium(url)
    else:
        html = fetch_url_html_content(url)

    if not html:
        raise ValueError("Could not fetch HTML content from the URL.")

    # Optionally capture screenshot
    screenshot_b64 = None
    screenshot_captured = False
    if include_screenshot:
        screenshot_b64 = capture_full_page_screenshot(url)
        screenshot_captured = screenshot_b64 is not None

    cost = CostAccumulator()

    # Detect language
    html_snippet = html[:MAX_HTML_SNIPPET_FOR_LANG_DETECT]
    lang, lang_p, lang_c = detect_language_from_html_with_openai(html_snippet, url)
    cost.add(OPENAI_MODEL_CHEAP, lang_p, lang_c)

    # Get page title
    title = get_page_title_from_html(html)

    # Extract knowledge
    result, p_tokens, c_tokens = extract_knowledge_from_page_with_openai(html, url, title, lang, screenshot_b64)
    cost.add(OPENAI_MODEL_CHEAP, p_tokens, c_tokens)

    # Free deterministic extras: schema.org/OpenGraph facts, optionally the brand kit.
    structured = extract_structured_data(html, url)
    response = {
        "status": "success",
        "url": url,
        "title": result.get("title_suggestion", title),
        "detected_language": lang,
        "extracted_content": result.get("extracted_chunk", ""),
        "structured_data": structured,
        "screenshot_captured": screenshot_captured,
        "cost_estimation": cost.cost_estimation()
    }
    if include_brand_kit:
        try:
            response["brand_kit"] = build_brand_kit(
                html, url, screenshot_b64, structured,
                session=SESSION, url_validator=validate_public_http_url,
                reports_dir=REPORTS_DIR)
        except Exception as e:
            logger.warning(f"Brand kit extraction failed for {url}: {e}")
            response["brand_kit"] = None
    return response


# --- OpenAI Helper Functions (Feature-Specific) ---

# For Company Analysis
def analyze_single_page_with_openai(page_content: str, url: str, cost: CostAccumulator = None) -> str:
    prompt = f"Analyze ONLY the following text content from '{url}'. Describe the page's purpose. Be concise (1-2 sentences). Content: ```{page_content}```"
    content, _, _ = llm_chat([{"role": "user", "content": prompt}],
                             max_tokens=MAX_RESPONSE_TOKENS_PAGE, cost=cost,
                             input_text_for_count=prompt)
    return content

def summarize_company_with_openai(page_summaries: list[dict], root_url: str, cost: CostAccumulator = None) -> str:
    combined_text = f"Based on analyses of pages from {root_url}:\n\n" + "\n".join([f"- URL: {s['url']}\n  Summary: {s['description']}" for s in page_summaries])
    prompt = f"Synthesize these descriptions into a comprehensive overview of the company at {root_url}. Describe its main purpose, offerings, and mission. Summaries:\n{combined_text}"
    content, _, _ = llm_chat([{"role": "user", "content": prompt}],
                             max_tokens=MAX_RESPONSE_TOKENS_SUMMARY, cost=cost,
                             input_text_for_count=prompt)
    return content

# For Prospect Qualification
def qualify_prospect_with_openai(page_content: str, prospect_url: str, user_profile: str,
                                 user_personas: list[str], cost: CostAccumulator = None):
    personas_str = "\n".join([f"- {p}" for p in user_personas])
    prompt = f"""You are a B2B sales analyst. Determine if a company is a good potential customer based on their website.
    **My Business Profile:** {user_profile}
    **My Ideal Customer Personas:** {personas_str}
    **Prospect's Website to Analyze:** URL: {prospect_url}, Page Content: ```{page_content}```
    **Your Task:** Based *only* on the page content, analyze the prospect.
    1. Do they align with my business and personas?
    2. Provide a confidence score from 0 to 100.
    3. State the reasons for your assessment.
    **Output Format:** Respond with ONLY a valid JSON object:
    {{
      "is_potential_customer": boolean, "confidence_score": integer,
      "reasoning_for": "Why this company IS a good fit.",
      "reasoning_against": "Why this company might NOT be a good fit."
    }}"""
    content, _, _ = llm_chat([{"role": "user", "content": prompt}],
                             max_tokens=MAX_RESPONSE_TOKENS_PROSPECT, json_mode=True,
                             cost=cost, input_text_for_count=prompt)
    return parse_json_response(content)

# For Knowledge Base Generation
# --- Language detection (source-language aware) ---
LANGUAGE_NAMES = {
    "fa": "Persian (Farsi)", "ar": "Arabic", "en": "English", "fr": "French",
    "de": "German", "es": "Spanish", "it": "Italian", "tr": "Turkish",
    "ru": "Russian", "zh": "Chinese", "ja": "Japanese", "ko": "Korean",
    "hi": "Hindi", "ur": "Urdu", "pt": "Portuguese", "nl": "Dutch",
}

def language_name(code: str) -> str:
    """Human-readable language name for prompts (models follow names better than codes)."""
    return LANGUAGE_NAMES.get((code or "").lower(), (code or "the source language"))

_ARABIC_SCRIPT_RE = re.compile(
    '[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]')
_LATIN_LETTER_RE = re.compile(r'[A-Za-z]')
# Persian-specific letters (پ چ ژ ک گ ی) distinguish Farsi from Arabic.
_PERSIAN_SPECIFIC_RE = re.compile('[پچژکگی]')


def detect_language_deterministic(text: str, threshold: float = 0.15):
    """Script-based detection for Arabic-script languages (Persian/Arabic).

    Returns 'fa', 'ar', or None when not confidently Arabic-script. This is reliable for
    Farsi sites and immune to slow homepages / head-heavy HTML, so it runs before the LLM.
    """
    if not text:
        return None
    sample = text[:5000]
    arabic = len(_ARABIC_SCRIPT_RE.findall(sample))
    latin = len(_LATIN_LETTER_RE.findall(sample))
    if arabic + latin == 0:
        return None
    if arabic >= 20 and arabic / (arabic + latin) >= threshold:
        return "fa" if _PERSIAN_SPECIFIC_RE.search(sample) else "ar"
    return None


def _llm_detect_language(text: str, url: str) -> tuple[str, int, int]:
    """LLM language identification from clean visible text. Returns (code, p, c)."""
    if not openai_client:
        raise ConnectionError("Vercel AI Gateway client not initialized.")
    if not text or not text.strip():
        return DEFAULT_TARGET_LANGUAGE, 0, 0
    messages = [
        {"role": "developer", "content": (
            "You are a language identification tool. Your ONLY output is a single 2-letter "
            "ISO 639-1 language code. No explanations. Examples: en  fa  ar  fr  de  es  zh  tr"
        )},
        {"role": "user", "content": f"Identify the primary language of this text:\n\n{text}"},
    ]
    content, p_tokens, c_tokens = llm_chat(messages, model=OPENAI_MODEL_CHEAP,
                                           max_tokens=MAX_RESPONSE_TOKENS_LANG_DETECT,
                                           input_text_for_count=text)
    lang = content.strip().lower()
    if len(lang) == 2 and lang.isalpha():
        return lang, p_tokens, c_tokens
    return DEFAULT_TARGET_LANGUAGE, p_tokens, c_tokens


def detect_language_from_html_with_openai(html_snippet: str, url: str) -> tuple[str, int, int]:
    """Detect language from a page's CLEAN visible text (deterministic first, LLM fallback).

    Signature-compatible with the previous version (used by /api/scrape-page). The key fix:
    detect from extracted visible text, not raw HTML head/markup which skews toward English.
    """
    if not openai_client: raise ConnectionError("Vercel AI Gateway client not initialized.")
    if not html_snippet or not html_snippet.strip():
        return DEFAULT_TARGET_LANGUAGE, 0, 0
    clean = clean_text_from_html(html_snippet, url) or html_snippet
    det = detect_language_deterministic(clean)
    if det:
        return det, 0, 0
    return _llm_detect_language(clean[:6000], url)


def detect_site_language(main_page_html: str, base_url: str, candidate_pages: list,
                         cost: "CostAccumulator", prepared_pages: list = None) -> str:
    """Robustly determine the site's primary language from SOURCE content.

    Deterministic script detection first (reliable for Persian/Arabic and immune to slow or
    head-heavy homepages), then LLM on clean visible text, falling back across a few candidate
    pages before defaulting. Prevents silently defaulting Farsi sites to English.
    """
    if main_page_html:
        clean = clean_text_from_html(main_page_html, base_url)
        det = detect_language_deterministic(clean)
        if det:
            return det
        if clean and clean.strip():
            lang, p, c = _llm_detect_language(clean[:6000], base_url)
            cost.add(OPENAI_MODEL_CHEAP, p, c)
            if lang and lang != DEFAULT_TARGET_LANGUAGE:
                return lang

    page_sources = prepared_pages if prepared_pages is not None else candidate_pages
    for pg in (page_sources or [])[:4]:
        if "text" in pg:
            clean = pg.get("text", "")
        else:
            try:
                html = fetch_url_html_content(pg.get("url"))
            except Exception:
                html = None
            if not html:
                continue
            clean = clean_text_from_html(html, pg.get("url"))
        det = detect_language_deterministic(clean)
        if det:
            return det
        if clean and clean.strip():
            lang, p, c = _llm_detect_language(clean[:6000], pg.get("url"))
            cost.add(OPENAI_MODEL_CHEAP, p, c)
            if lang and lang != DEFAULT_TARGET_LANGUAGE:
                return lang

    return DEFAULT_TARGET_LANGUAGE

def identify_knowledge_rich_content_clusters(page_details: list[dict], root_url: str, lang: str) -> tuple[dict, int, int]:
    """Identify and prioritize knowledge-rich content clusters for chatbot training."""
    if not openai_client: raise ConnectionError("Vercel AI Gateway client not initialized.")
    
    # Prepare URL list for analysis - limit for better processing
    urls_list = [p['url'] for p in page_details]
    
    # Smart sampling with priority for knowledge content
    if len(urls_list) > 400:
        import random
        random.seed(42)
        
        # Define knowledge-rich URL patterns
        knowledge_patterns = [
            'how-to', 'guide', 'tutorial', 'tips', 'best-', 'what-is', 'vs-', 'comparison',
            'install', 'setup', 'troubleshoot', 'solve', 'fix', 'error', 'problem',
            'about', 'contact', 'policy', 'terms', 'faq', 'help', 'support', 'service',
            'branch', 'location', 'درباره', 'تماس', 'راهنما', 'آموزش', 'نصب', 'حل'
        ]
        
        # Categorize URLs by knowledge value
        high_value_urls = []
        medium_value_urls = []
        low_value_urls = []
        
        for url in urls_list:
            url_lower = url.lower()
            # Skip technical/admin pages
            if any(skip in url_lower for skip in ['wp-admin', 'wp-content', 'assets', 'css', 'js', 'images', 'cache', 'login', 'register', 'cart', 'checkout']):
                continue
            # High value: knowledge-rich content
            elif any(pattern in url_lower for pattern in knowledge_patterns):
                high_value_urls.append(url)
            # Medium value: category/brand pages
            elif any(cat in url_lower for cat in ['category', 'brand', 'tag', 'archive']):
                medium_value_urls.append(url)
            # Low value: individual products
            elif 'product' in url_lower:
                low_value_urls.append(url)
            else:
                medium_value_urls.append(url)
        
        # Sample intelligently: prioritize high-value content
        sampled_urls = []
        sampled_urls.extend(high_value_urls[:150])  # Take most high-value content
        remaining_slots = 400 - len(sampled_urls)
        
        if remaining_slots > 0 and medium_value_urls:
            sampled_urls.extend(random.sample(medium_value_urls, min(remaining_slots // 2, len(medium_value_urls))))
        
        remaining_slots = 400 - len(sampled_urls)
        if remaining_slots > 0 and low_value_urls:
            sampled_urls.extend(random.sample(low_value_urls, min(remaining_slots, len(low_value_urls))))
        
        urls_list = sampled_urls
        logger.info(f"Prioritized {len(urls_list)} URLs for knowledge analysis: {len(high_value_urls)} high-value, {len(medium_value_urls)} medium-value, {len(low_value_urls)} low-value")
    
    urls_text = "\n".join([f"- {url}" for url in urls_list])

    cluster_descriptions = {
        "educational_content": "Tutorial, how-to, and educational content",
        "buying_guides": "Comparison guides and recommendations",
        "technical_explanations": "Concept definitions and technical explanations",
        "troubleshooting_support": "Problem-solving and support content",
        "company_information": "About, contact, policies, and company info",
        "service_information": "Services, support processes, and features"
    }

    messages = [
        {
            "role": "developer",
            "content": (
                f"You are a URL categorization tool for chatbot knowledge extraction. "
                f"Classify URLs into knowledge-rich content clusters. "
                f"All descriptions and analysis_summary MUST be written in {lang}. "
                f"Output ONLY valid JSON matching the schema provided. "
                f"Strictly avoid individual product pages, cart, checkout, account, admin, and asset URLs."
            )
        },
        {
            "role": "user",
            "content": f"""Categorize these URLs from {root_url} into knowledge-rich clusters for chatbot training.

Clusters to identify:
1. educational_content — how-to guides, tutorials, installation, setup instructions
2. buying_guides — comparisons, decision guides, best-practices (not individual products)
3. technical_explanations — concept definitions, feature explanations, what-is pages
4. troubleshooting_support — problem-solving, error fixes, tips
5. company_information — about, contact, policies, terms, branch locations
6. service_information — services offered, support processes, warranty info

URLs:
{urls_text}

Output this JSON schema:
{{
    "educational_content": {{"urls": [...], "description": "{cluster_descriptions['educational_content']}"}},
    "buying_guides": {{"urls": [...], "description": "{cluster_descriptions['buying_guides']}"}},
    "technical_explanations": {{"urls": [...], "description": "{cluster_descriptions['technical_explanations']}"}},
    "troubleshooting_support": {{"urls": [...], "description": "{cluster_descriptions['troubleshooting_support']}"}},
    "company_information": {{"urls": [...], "description": "{cluster_descriptions['company_information']}"}},
    "service_information": {{"urls": [...], "description": "{cluster_descriptions['service_information']}"}},
    "priority_extraction_order": ["cluster_name1", ...],
    "total_knowledge_pages_identified": <number>,
    "analysis_summary": "<brief summary in {lang}>"
}}"""
        }
    ]

    content, p_tokens, c_tokens = llm_chat(messages, model=OPENAI_MODEL_CHEAP,
                                           max_tokens=MAX_RESPONSE_TOKENS_PAGE_SELECTION * 2,
                                           json_mode=True, input_text_for_count=urls_text)
    try:
        response_data = parse_json_response(content)
        return response_data, p_tokens, c_tokens
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        logger.error(f"Error parsing knowledge cluster analysis: {e}")
        logger.error(f"Raw response content: {content[:500]}...")
        
        # Enhanced fallback with knowledge-focused categorization
        knowledge_patterns = ['how-to', 'guide', 'tutorial', 'tips', 'best-', 'what-is', 'vs-', 'install', 'درباره', 'تماس', 'راهنما', 'آموزش']
        company_patterns = ['about', 'contact', 'policy', 'terms', 'branch', 'location', 'درباره', 'تماس', 'قوانین', 'شرایط']
        support_patterns = ['help', 'support', 'faq', 'troubleshoot', 'solve', 'fix', 'error', 'problem', 'پشتیبانی', 'راهنما']
        
        educational_urls = [url for url in urls_list if any(pattern in url.lower() for pattern in ['how-to', 'tutorial', 'guide', 'install', 'setup', 'آموزش', 'نصب'])]
        company_urls = [url for url in urls_list if any(pattern in url.lower() for pattern in company_patterns)]
        support_urls = [url for url in urls_list if any(pattern in url.lower() for pattern in support_patterns)]
        
        # Create language-appropriate descriptions
        if lang == 'fa':
            descriptions = {
                "educational_content": "محتوای آموزشی و راهنماهای فنی",
                "company_information": "اطلاعات شرکت و سیاست‌ها", 
                "troubleshooting_support": "محتوای پشتیبانی و عیب‌یابی",
                "buying_guides": "راهنماهای خرید شناسایی نشد در بازگشتی",
                "technical_explanations": "توضیحات فنی شناسایی نشد در بازگشتی",
                "service_information": "اطلاعات خدمات شناسایی نشد در بازگشتی",
                "analysis_summary": "تجزیه و تحلیل بازگشتی با استفاده از تطبیق الگو برای محتوای غنی از دانش"
            }
        else:
            descriptions = {
                "educational_content": "Educational and tutorial content",
                "company_information": "Company information and policies",
                "troubleshooting_support": "Support and troubleshooting content", 
                "buying_guides": "No buying guides identified in fallback",
                "technical_explanations": "No technical explanations identified in fallback",
                "service_information": "No service information identified in fallback",
                "analysis_summary": "Fallback analysis using pattern matching for knowledge-rich content"
            }
        
        fallback_response = {
            "educational_content": {"urls": educational_urls[:10], "description": descriptions["educational_content"]},
            "company_information": {"urls": company_urls[:10], "description": descriptions["company_information"]},
            "troubleshooting_support": {"urls": support_urls[:5], "description": descriptions["troubleshooting_support"]},
            "buying_guides": {"urls": [], "description": descriptions["buying_guides"]},
            "technical_explanations": {"urls": [], "description": descriptions["technical_explanations"]},
            "service_information": {"urls": [], "description": descriptions["service_information"]},
            "priority_extraction_order": ["company_information", "educational_content", "troubleshooting_support"],
            "total_knowledge_pages_identified": len(educational_urls) + len(company_urls) + len(support_urls),
            "analysis_summary": descriptions["analysis_summary"]
        }
        
        logger.info(f"Using fallback knowledge cluster analysis: {fallback_response['total_knowledge_pages_identified']} knowledge pages identified")
        return fallback_response, p_tokens, c_tokens

def extract_website_colors_with_openai(html_content: str, url: str, screenshot_base64: str = None) -> tuple[dict, int, int]:
    """Extract website background color and primary brand color using AI analysis."""
    if not openai_client: 
        raise ConnectionError("Vercel AI Gateway client not initialized.")
    
    # Include screenshot context if available
    screenshot_context = ""
    if screenshot_base64:
        screenshot_context = f"""
    
    VISUAL CONTEXT: A full-page screenshot of this website is available. Use this visual information to identify the main background color and primary brand color from the actual visual appearance of the website.
    
    Screenshot (base64): data:image/png;base64,{screenshot_base64[:100]}... [truncated for prompt length]
    """
    
    prompt = f"""Analyze the website at '{url}' to identify its color scheme.{screenshot_context}
    
    Your task is to identify:
    1. Main background color (most common background color used across the website)
    2. Primary brand color (the main color used for branding, buttons, links, headers, etc.)
    
    Look at the HTML content and visual context to determine these colors.
    
    Respond with a JSON object:
    {{
        "main_background_color": "hex color code (e.g., #ffffff)",
        "primary_brand_color": "hex color code (e.g., #007bff)",
        "background_color_description": "Brief description of the background color",
        "brand_color_description": "Brief description of the brand color and where it's used"
    }}
    
    HTML Content: ```{html_content[:5000]}```"""

    content, p_tokens, c_tokens = llm_chat([{"role": "user", "content": prompt}],
                                           model=OPENAI_MODEL_CHEAP, max_tokens=300,
                                           json_mode=True, input_text_for_count=prompt)
    try:
        return parse_json_response(content), p_tokens, c_tokens
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        logger.error(f"Error parsing color extraction response: {e}")
        # Return a fallback response
        return {
            "main_background_color": "#ffffff",
            "primary_brand_color": "#000000",
            "background_color_description": "Default white background (fallback)",
            "brand_color_description": "Default black text (fallback)"
        }, p_tokens, c_tokens

def extract_knowledge_from_page_with_openai(html_content: str, url: str, title: str, lang: str,
                                           screenshot_base64: str = None,
                                           cleaned_text: str = None) -> tuple[dict, int, int]:
    """Extract customer-relevant knowledge from one page into a structured chunk.

    Feeds CLEAN main-content text (not raw HTML) to the cheap model and classifies the page
    into exactly one canonical KB section, so the final document can be synthesised
    section-by-section instead of through a single token-capped compile call.
    Returns (data, prompt_tokens, completion_tokens) where data has keys:
    url, title_suggestion, primary_category, extracted_chunk.
    """
    if not openai_client: raise ConnectionError("Vercel AI Gateway client not initialized.")

    clean_text = cleaned_text if cleaned_text is not None else clean_text_from_html(html_content, url)
    section_keys_str = ", ".join(KB_SECTION_KEYS)
    lang_name = language_name(lang)

    user_text = f"""Extract ALL customer-relevant knowledge from this web page for a customer-support chatbot.

URL: {url}
Page Title: {title or ""}

Capture (when present): contact details (phones, emails, addresses, hours, social links),
company background/mission, policies (shipping, returns, refunds, warranty, privacy, terms),
services and procedures, FAQ as question+answer pairs, branch/location info, payment methods
and ordering/checkout steps, and any educational / how-to / troubleshooting content.

Rules:
- Preserve phone numbers, emails, addresses, prices and policy clauses VERBATIM.
- Represent FAQs as **Q:** / **A:** pairs, but ONLY include questions that have an actual answer in the content; skip any question with no answer.
- Ignore navigation menus, cookie banners and unrelated product-catalog listings.
- Write the extracted_chunk entirely in {lang_name}. If the source text is in another language, translate it INTO {lang_name}. Do NOT output English unless {lang_name} is English.
- If the page has no useful customer knowledge, return an empty string for extracted_chunk.

Classify the page's PRIMARY purpose into exactly one of: {section_keys_str}

Output ONLY this JSON:
{{
  "url": "{url}",
  "title_suggestion": "<short descriptive title in {lang_name}>",
  "primary_category": "<one of: {section_keys_str}>",
  "extracted_chunk": "<comprehensive Markdown in {lang_name} covering everything useful on this page>"
}}

PAGE CONTENT:
```{clean_text}```"""

    user_content_parts = [{"type": "text", "text": user_text}]
    if screenshot_base64:
        user_content_parts.insert(0, {
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{screenshot_base64}", "detail": "low"}
        })

    messages = [
        {
            "role": "developer",
            "content": (
                f"You are a precise knowledge-extraction engine for chatbot training data. "
                f"Write ALL output in {lang_name} (translate source content into {lang_name}; "
                f"never use English unless {lang_name} is English). "
                f"Keep contact details, prices and policy text verbatim. "
                f"Output ONLY valid JSON matching the schema provided."
            )
        },
        {"role": "user", "content": user_content_parts},
    ]

    content, p_tokens, c_tokens = llm_chat(messages, model=OPENAI_MODEL_CHEAP,
                                           max_tokens=MAX_RESPONSE_TOKENS_PAGE_EXTRACTION,
                                           json_mode=True, input_text_for_count=clean_text)
    try:
        data = parse_json_response(content)
        cat = str(data.get("primary_category", "")).strip().lower()
        if cat not in KB_SECTION_KEYS:
            cat = "additional"
        data["primary_category"] = cat
        data.setdefault("url", url)
        data.setdefault("title_suggestion", title)
        data.setdefault("extracted_chunk", "")
        return data, p_tokens, c_tokens
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        logger.error(f"Error parsing knowledge extraction response for {url}: {e}")
        return {
            "url": url,
            "title_suggestion": title,
            "primary_category": "additional",
            "extracted_chunk": ""
        }, p_tokens, c_tokens

# --- Report Helpers ---
def save_results_to_csv(job_id: str, results_data: list):
    """Save prospect qualification results to CSV."""
    if not results_data: return None
    os.makedirs(REPORTS_DIR, exist_ok=True)
    filepath = os.path.join(REPORTS_DIR, f"prospect_report_{job_id}.csv")
    headers = ['website', 'status', 'is_potential_customer', 'confidence_score', 'reasoning_for', 'reasoning_against', 'error']
    try:
        with open(filepath, 'w', newline='', encoding='utf-8') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=headers)
            writer.writeheader()
            for result in results_data:
                row = {
                    'website': result.get('url'), 'status': result.get('status'),
                    'is_potential_customer': result.get('analysis', {}).get('is_potential_customer', ''),
                    'confidence_score': result.get('analysis', {}).get('confidence_score', ''),
                    'reasoning_for': result.get('analysis', {}).get('reasoning_for', ''),
                    'reasoning_against': result.get('analysis', {}).get('reasoning_against', ''),
                    'error': result.get('error', '')
                }
                writer.writerow(row)
        logger.info(f"Successfully saved prospect report to {filepath}")
        return filepath
    except IOError as e:
        logger.error(f"Failed to write CSV report for job {job_id}: {e}")
        return None

def save_knowledge_base_report(job_id: str, url: str, knowledge_base: str, metadata: dict):
    """Save successful knowledge base generation to reports folder."""
    os.makedirs(REPORTS_DIR, exist_ok=True)
    
    # Create timestamp for filename
    from datetime import datetime
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # Extract domain name for filename
    from urllib.parse import urlparse
    domain = urlparse(url).netloc.replace('www.', '').replace('.', '_')
    
    # Save knowledge base as markdown
    kb_filename = f"knowledge_base_{domain}_{timestamp}_{job_id[:8]}.md"
    kb_filepath = os.path.join(REPORTS_DIR, kb_filename)
    
    try:
        with open(kb_filepath, 'w', encoding='utf-8') as f:
            f.write(f"# Knowledge Base Report\n\n")
            f.write(f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"**Job ID:** {job_id}\n")
            f.write(f"**Source URL:** {url}\n")
            f.write(f"**Language:** {metadata.get('detected_target_language', 'unknown')}\n")
            f.write(f"**Pages Processed:** {metadata.get('extracted_pages_count', 0)}\n")
            f.write(f"**Screenshot Captured:** {metadata.get('main_page_screenshot_captured', False)}\n")
            f.write(f"**Total Cost:** ${metadata.get('cost_estimation', {}).get('total_cost_usd', '0.00')}\n")
            if metadata.get('depth'):
                f.write(f"**Depth:** {metadata.get('depth')}\n")
            if metadata.get('models'):
                f.write(f"**Models:** cheap={metadata['models'].get('cheap')} / strong={metadata['models'].get('strong')}\n")
            qr = metadata.get('quality_report') or {}
            if qr:
                flags = [k for k in ('has_contact', 'has_email', 'has_phone', 'has_policies', 'has_faq', 'has_services') if qr.get(k)]
                missing = qr.get('missing') or []
                f.write(f"**Completeness:** present={', '.join(flags) or 'none'}"
                        + (f"; missing={', '.join(map(str, missing))}" if missing else "") + "\n")
            f.write("\n---\n\n")
            f.write(knowledge_base)
        
        # Save metadata as JSON
        metadata_filename = f"metadata_{domain}_{timestamp}_{job_id[:8]}.json"
        metadata_filepath = os.path.join(REPORTS_DIR, metadata_filename)
        
        full_metadata = {
            "job_id": job_id,
            "url": url,
            "generated_at": datetime.now().isoformat(),
            "knowledge_base_file": kb_filename,
            **metadata
        }
        
        with open(metadata_filepath, 'w', encoding='utf-8') as f:
            json.dump(full_metadata, f, indent=2, ensure_ascii=False)
        
        logger.info(f"Knowledge base report saved: {kb_filepath}")
        logger.info(f"Metadata saved: {metadata_filepath}")
        
        return kb_filepath, metadata_filepath
        
    except IOError as e:
        logger.error(f"Failed to save knowledge base report for job {job_id}: {e}")
        raise

# --- Background Job Runners ---

def run_company_analysis_job(job_id, url, max_pages, use_selenium):
    logger.info(f"Starting analysis job {job_id} for {url}")
    with jobs_lock: jobs[job_id].update({"status": "running", "started_at": time.time()})
    cost = CostAccumulator()

    try:
        crawl_func = selenium_crawl_website if use_selenium else simple_crawl_website
        found_pages = crawl_func(url, max_pages)

        page_summaries = []
        for i, page in enumerate(found_pages):
            with jobs_lock: jobs[job_id]["progress"] = f"Analyzing page {i+1}/{len(found_pages)}"
            try:
                content = fetch_url_content(page['url'])
                if content:
                    summary = analyze_single_page_with_openai(content, page['url'], cost)
                    page_summaries.append({'url': page['url'], 'description': summary})
            except Exception as e:
                logger.error(f"Failed to analyze page {page['url']}: {e}")

        with jobs_lock: jobs[job_id]["progress"] = "Summarizing company..."
        final_summary = summarize_company_with_openai(page_summaries, url, cost)

        with jobs_lock:
            jobs[job_id].update({
                "status": "completed",
                "results": {"company_summary": final_summary, "analyzed_pages": page_summaries},
                "cost_estimation": cost.cost_estimation(),
                "finished_at": time.time(),
            })
    except Exception as e:
        logger.error(f"Job {job_id} failed: {e}", exc_info=True)
        with jobs_lock:
            jobs[job_id].update({"status": "failed", "error": str(e), "finished_at": time.time()})

def run_prospect_qualification_job(job_id, user_profile, user_personas, prospect_urls):
    logger.info(f"Starting prospect qualification job {job_id}")
    with jobs_lock: jobs[job_id].update({"status": "running", "started_at": time.time()})
    cost = CostAccumulator()
    results = []

    try:
        for i, url in enumerate(prospect_urls):
            with jobs_lock: jobs[job_id]["progress"] = f"Qualifying {i+1}/{len(prospect_urls)}: {url}"
            result_entry = {"url": url, "status": "pending", "analysis": None, "error": None}
            try:
                page_content = fetch_url_content(url)
                if not page_content.strip(): raise ValueError("Fetched content is empty.")

                analysis = qualify_prospect_with_openai(page_content, url, user_profile, user_personas, cost)
                result_entry.update({"status": "completed", "analysis": analysis})
            except Exception as e:
                result_entry.update({"status": "failed", "error": str(e)})
            results.append(result_entry)

        csv_report_path = save_results_to_csv(job_id, results)
        with jobs_lock:
            jobs[job_id].update({
                "status": "completed",
                "results": results,
                "csv_report_path": csv_report_path,
                "cost_estimation": cost.cost_estimation(),
                "finished_at": time.time(),
            })
    except Exception as e:
        # Top-level guard so the job can never get stuck in "running"; keep partial results.
        logger.error(f"Prospect job {job_id} failed: {e}", exc_info=True)
        with jobs_lock:
            jobs[job_id].update({
                "status": "failed",
                "error": str(e),
                "results": results,
                "cost_estimation": cost.cost_estimation(),
                "finished_at": time.time(),
            })

# =====================================================================================
# Overhauled KB pipeline: discovery -> deterministic pre-filter and selection ->
# parallel clean-text extraction -> per-section map-reduce synthesis -> assembly -> audit.
# Designed to scale to 1000+ page sites without ever sending the catalogue to an LLM.
# =====================================================================================

# Deterministic URL filtering bounds cost on large sites BEFORE any LLM call.
_ASSET_EXTENSIONS = (
    '.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.ico', '.bmp', '.tiff',
    '.css', '.js', '.mjs', '.json', '.xml', '.rss', '.atom',
    '.pdf', '.zip', '.rar', '.gz', '.tar', '.7z',
    '.mp4', '.mp3', '.avi', '.mov', '.wmv', '.webm', '.wav', '.ogg',
    '.woff', '.woff2', '.ttf', '.eot', '.otf', '.webmanifest', '.map',
)
_TRANSACTIONAL_PATH_HINTS = (
    '/cart', '/checkout', '/basket', '/account', '/my-account', '/login', '/logout',
    '/signin', '/sign-in', '/signup', '/sign-up', '/register', '/wishlist', '/compare',
    '/admin', '/wp-admin', '/wp-login', '/wp-json', '/order-tracking', '/track-order',
    '/payment', '/سبد-خرید', '/تسویه', '/ورود', '/ثبت-نام', '/حساب-کاربری',
)
_ARCHIVE_PATH_HINTS = ('/tag/', '/tags/', '/author/', '/feed/', '/page/', '/comment',
                       '/برچسب/', '/نویسنده/')
_QUERY_JUNK_HINTS = ('add-to-cart', 'orderby=', 'filter_', 'filter=', 'replytocom=',
                     'add_to_wishlist', 'compare=', 'paged=', 'page=', 'sort=', 'pagenum')

_EMAIL_RE = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
_PHONE_RE = re.compile(r'(?:(?:\+|00)\d{1,3}[\s-]?)?(?:\(?\d{2,4}\)?[\s-]?){2,5}\d{2,4}')


def _looks_like_product_url(path_lower: str) -> bool:
    """Heuristic for individual product-detail pages (skipped for a knowledge base)."""
    return any(h in path_lower for h in ('/product/', '/products/', '/dp/', '/item/',
                                         '/sku/', '/-p-', '/buy/', '/محصول/', '/کالا/'))


# URL-path hints that almost always mark a knowledge page (English + Persian slugs).
_KNOWLEDGE_PATH_HINTS = (
    'about', 'contact', 'faq', 'policy', 'policies', 'terms', 'privacy', 'shipping',
    'delivery', 'return', 'refund', 'warranty', 'guarantee', 'support', 'help', 'guide',
    'how-to', 'howto', 'tutorial', 'service', 'payment', 'branch', 'location',
    'درباره', 'تماس', 'قوانین', 'شرایط', 'حریم', 'ارسال', 'بازگشت', 'گارانتی',
    'سوالات', 'پرسش', 'راهنما', 'خدمات', 'پرداخت', 'آموزش', 'نمایندگی', 'شعب',
)


def knowledge_path_score(url: str) -> float:
    """Deterministic knowledge-richness score for a URL path; >= 2.0 means an obvious
    knowledge page that can be selected without asking an LLM."""
    path = unquote(urlparse(url).path or '/').lower()
    score = sum(2.0 for h in _KNOWLEDGE_PATH_HINTS if h in path)
    depth = max(0, len([seg for seg in path.split('/') if seg]) - 1)
    return score - 0.25 * depth


def prefilter_candidate_urls(urls: list, base_domain: str, keep_product_sample: int = 15):
    """Deterministically drop non-knowledge URLs before any LLM call.

    Returns (kept_urls, dropped_counts). Product-detail URLs are capped to a small sample
    rather than fully dropped, so a product-only site still yields representative pages.
    """
    kept, dropped = [], collections.Counter()
    seen = set()
    product_sample = []
    for u in urls:
        if not u or not isinstance(u, str):
            dropped['invalid'] += 1
            continue
        norm = urlparse(u)._replace(fragment="").geturl()
        if norm in seen:
            dropped['duplicate'] += 1
            continue
        seen.add(norm)
        p = urlparse(norm)
        if p.scheme not in ('http', 'https'):
            dropped['non_http'] += 1
            continue
        if p.netloc != base_domain:
            dropped['offsite'] += 1
            continue
        path_lower = p.path.lower()
        query_lower = (p.query or '').lower()
        if path_lower.endswith(_ASSET_EXTENSIONS):
            dropped['asset'] += 1
            continue
        if any(h in path_lower for h in _TRANSACTIONAL_PATH_HINTS):
            dropped['transactional'] += 1
            continue
        if any(h in path_lower for h in _ARCHIVE_PATH_HINTS):
            dropped['archive'] += 1
            continue
        if query_lower and any(h in query_lower for h in _QUERY_JUNK_HINTS):
            dropped['faceted'] += 1
            continue
        if _looks_like_product_url(path_lower):
            product_sample.append(norm)
            dropped['product'] += 1
            continue
        kept.append(norm)
    # Re-admit a small representative sample of product pages.
    if product_sample:
        sample = product_sample[:keep_product_sample]
        kept.extend(sample)
        dropped['product'] -= len(sample)
        if dropped['product'] <= 0:
            dropped.pop('product', None)
    return kept, dict(dropped)


def discover_all_candidate_urls(base_url: str, use_selenium: bool = False):
    """Discover candidate page URLs: sitemap first (scales to huge sites), crawl fallback.

    Returns (candidate_urls_including_base, discovery_meta, sitemap_meta_by_url).
    """
    meta = {"sitemap_urls": 0, "crawled_urls": 0, "method": None, "capped": False}

    sitemap_urls, sitemap_meta = [], {}
    try:
        sitemap_urls, sitemap_meta = find_sitemap_urls(base_url)
    except Exception as e:
        logger.warning(f"Sitemap discovery failed for {base_url}: {e}")
    meta["sitemap_urls"] = len(sitemap_urls)

    candidates = list(sitemap_urls)
    if len(candidates) >= 5:
        meta["method"] = "sitemap"
    else:
        meta["method"] = "crawl"
        try:
            crawl_cap = min(MAX_PAGES_FOR_FALLBACK_DISCOVERY_CRAWL, MAX_DISCOVERY_URLS)
            if use_selenium and SELENIUM_AVAILABLE:
                crawled = selenium_crawl_website(base_url, max_pages=crawl_cap)
            else:
                crawled = simple_crawl_website(base_url, max_pages=crawl_cap)
            crawl_urls = [c['url'] for c in crawled]
            meta["crawled_urls"] = len(crawl_urls)
            candidates.extend(crawl_urls)
        except Exception as e:
            logger.warning(f"Fallback crawl failed for {base_url}: {e}")

    candidates.insert(0, base_url)

    if len(candidates) > MAX_DISCOVERY_URLS:
        meta["capped"] = True
        logger.info(f"Discovery cap hit: {len(candidates)} URLs -> {MAX_DISCOVERY_URLS}")
        base_norm = urlparse(base_url)._replace(fragment="").geturl()
        unique_candidates = set(url for url in candidates if isinstance(url, str) and url)
        unique_candidates.discard(base_norm)

        ranked_candidates = sorted(
            unique_candidates,
            key=lambda url: (-knowledge_path_score(url), url),
        )
        candidates = [base_norm] + ranked_candidates[:max(0, MAX_DISCOVERY_URLS - 1)]

    return candidates, meta, sitemap_meta


def select_knowledge_pages(base_url: str, candidate_urls: list, lang: str, page_budget: int,
                           cost: CostAccumulator, sitemap_meta: dict = None):
    """Pick the most knowledge-rich pages to deeply extract, within page_budget.

    Pipeline: deterministic pre-filter -> AI cluster categorisation (cheap model) ->
    priority-ordered selection. Always includes the site's main page. When the sitemap
    provides <priority>/<lastmod>, higher-priority / fresher pages are preferred in the
    deterministic ordering.
    Returns (selected_pages, selection_meta); each page is {url, cluster}.
    """
    base_domain = urlparse(base_url).netloc
    kept, dropped_counts = prefilter_candidate_urls(candidate_urls, base_domain)
    if sitemap_meta:
        # Prefer pages the site itself marks important. Two stable sorts: newest
        # lastmod first (ISO-8601 sorts lexicographically), then priority desc, so the
        # final order is priority-major with recency as tie-breaker.
        kept.sort(key=lambda u: (sitemap_meta.get(u) or {}).get('lastmod') or '', reverse=True)
        def _priority_of(u):
            p = (sitemap_meta.get(u) or {}).get('priority')
            return p if p is not None else 0.5
        kept.sort(key=_priority_of, reverse=True)
    logger.info(f"Pre-filter kept {len(kept)} of {len(candidate_urls)} URLs; dropped {dropped_counts}")

    selection_meta = {
        "candidates_total": len(candidate_urls),
        "after_prefilter": len(kept),
        "dropped_by_reason": dropped_counts,
        "clusters": None,
        "auto_selected_by_keyword": 0,
        "llm_selection_skipped": False,
    }

    base_norm = urlparse(base_url)._replace(fragment="").geturl()
    if base_norm not in kept:
        kept.insert(0, base_norm)

    selected = []
    seen = set()

    def _add(u, cluster):
        if u in seen:
            return
        seen.add(u)
        selected.append({"url": u, "cluster": cluster})

    _add(base_norm, "company_information")

    # Deterministic auto-selection: URLs whose path screams knowledge page
    # (about/contact/faq/policy/... incl. Persian slugs) skip the LLM entirely.
    for score, u in sorted(((knowledge_path_score(u), u) for u in kept), reverse=True):
        if score < 2.0 or len(selected) >= page_budget:
            break
        _add(u, "auto_keyword")
    selection_meta["auto_selected_by_keyword"] = max(0, len(selected) - 1)

    remaining = [u for u in kept if u not in seen]
    if remaining and len(selected) + len(remaining) <= page_budget:
        # Everything fits in the budget — AI ranking would change nothing but cost.
        for u in remaining:
            _add(u, "company_information")
        selection_meta["llm_selection_skipped"] = True
    elif remaining and len(selected) < page_budget:
        try:
            page_details = [{"url": u} for u in remaining]
            clusters, p, c = identify_knowledge_rich_content_clusters(page_details, base_url, lang)
            cost.add(OPENAI_MODEL_CHEAP, p, c)
            selection_meta["clusters"] = {
                k: len(v.get("urls", [])) for k, v in clusters.items() if isinstance(v, dict)
            }
            order = clusters.get("priority_extraction_order") or [
                "company_information", "service_information", "troubleshooting_support",
                "educational_content", "buying_guides", "technical_explanations",
            ]
            cluster_names = list(order) + [k for k in clusters if k not in order]
            for cname in cluster_names:
                cval = clusters.get(cname)
                if not isinstance(cval, dict):
                    continue
                for u in cval.get("urls", []):
                    if not isinstance(u, str):
                        continue
                    nu = urlparse(u)._replace(fragment="").geturl()
                    if urlparse(nu).netloc != base_domain:
                        continue
                    _add(nu, cname)
                    if len(selected) >= page_budget:
                        break
                if len(selected) >= page_budget:
                    break
        except Exception as e:
            logger.warning(f"AI page selection failed, falling back to pre-filtered order: {e}")

    # Backfill from kept URLs if the AI under-selected.
    if len(selected) < page_budget:
        for u in kept:
            _add(u, "company_information")
            if len(selected) >= page_budget:
                break

    return selected[:page_budget], selection_meta


def page_identity(url: str) -> tuple:
    """Key that treats http/https, default ports, host case, and a trailing slash as one page."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    port = parsed.port
    if port and port != (443 if parsed.scheme == "https" else 80):
        host = f"{host}:{port}"
    path = parsed.path.rstrip("/") or "/"
    return host, path, parsed.query


def fold_url_variants(urls: list, preferred_scheme: str = "https") -> list:
    """Keep one URL per page identity, preferring `preferred_scheme`, else the first seen."""
    chosen = {}
    for url in urls:
        key = page_identity(url)
        current = chosen.get(key)
        if current is None or (urlparse(current).scheme != preferred_scheme
                               and urlparse(url).scheme == preferred_scheme):
            chosen[key] = url
    return list(chosen.values())


def select_knowledge_pages_deterministically(base_url: str, candidate_urls: list,
                                              page_budget: int, sitemap_meta: dict = None):
    """Select a stable page set so a job fingerprint can be compared on later crawls."""
    base_domain = urlparse(base_url).netloc
    stable_candidates = sorted(
        url for url in candidate_urls if isinstance(url, str) and url
    )
    kept, dropped_counts = prefilter_candidate_urls(stable_candidates, base_domain)
    base_norm = urlparse(base_url)._replace(fragment="").geturl()
    base_identity = page_identity(base_norm)
    # Fold http/https and trailing-slash variants so one page never fills two slots.
    kept = [url for url in fold_url_variants(kept, urlparse(base_norm).scheme)
            if page_identity(url) != base_identity]

    # Rank by stable URL features only. Sitemap metadata can change without page text
    # changing, so it must not move a different URL into the budgeted fingerprint set.
    kept.sort(key=lambda url: (-knowledge_path_score(url), url))
    selected = [{"url": base_norm, "cluster": "company_information"}]
    for url in kept:
        if len(selected) >= page_budget:
            break
        selected.append({
            "url": url,
            "cluster": "auto_keyword" if knowledge_path_score(url) >= 2.0 else "company_information",
        })

    return selected[:page_budget], {
        "candidates_total": len(candidate_urls),
        "after_prefilter": len(kept) + 1,
        "dropped_by_reason": dropped_counts,
        "clusters": None,
        "auto_selected_by_keyword": sum(
            1 for page in selected[1:] if page["cluster"] == "auto_keyword"
        ),
        "llm_selection_skipped": True,
        "selection_method": "deterministic_fingerprint_stable",
    }


def select_specific_pages_deterministically(base_url: str, pages: list[dict], page_budget: int):
    """Apply a stable budget to explicitly supplied pages, preserving the homepage first."""
    base_norm = urlparse(base_url)._replace(fragment="").geturl()
    clusters = {}
    for page in pages or []:
        url = urlparse(page.get("url", ""))._replace(fragment="").geturl()
        if url:
            clusters.setdefault(url, page.get("cluster", "company_information"))
    base_identity = page_identity(base_norm)
    unique_urls = fold_url_variants(sorted(clusters), urlparse(base_norm).scheme)
    unique_pages = [
        {"url": base_norm if page_identity(url) == base_identity else url, "cluster": clusters[url]}
        for url in unique_urls
    ]
    ordered = sorted(unique_pages, key=lambda page: (
        page["url"] != base_norm,
        -knowledge_path_score(page["url"]),
        page["url"],
    ))
    return ordered[:page_budget]


SOURCE_FINGERPRINT_VERSION = "clean-text-v1"


def fingerprint_source_page(url: str, title: str, text: str) -> str:
    """Hash the fetched page title and cleaned source text, before model processing."""
    source = json.dumps(
        {"version": SOURCE_FINGERPRINT_VERSION, "title": title or "", "text": text or ""},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(source.encode("utf-8", "ignore")).hexdigest()


def fingerprint_source_pages(pages: list[dict], failed_urls: list = None) -> str:
    """Hash the page addresses, their source fingerprints, and the selected pages that failed.

    Failed pages count by address only, so a page that keeps failing (a dead link) leaves the
    fingerprint stable, while a page that starts or stops failing changes it. With no failures
    the payload is identical to the pre-failure-aware format, so existing fingerprints still match.
    """
    page_rows = []
    for page in pages:
        url = urlparse(page.get("url", ""))._replace(fragment="").geturl()
        page_rows.append({"url": url, "fingerprint": page.get("fingerprint", "")})
    page_rows.sort(key=lambda row: row["url"])
    fingerprint_payload = {"version": SOURCE_FINGERPRINT_VERSION, "pages": page_rows}
    failed = sorted({urlparse(url)._replace(fragment="").geturl() for url in failed_urls or [] if url})
    if failed:
        fingerprint_payload["failed"] = failed
    payload = json.dumps(
        fingerprint_payload,
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8", "ignore")).hexdigest()


def harvest_contact_candidates(texts: list) -> dict:
    """Regex-harvest emails/phones across all page texts as a contact safety-net."""
    emails, phones = set(), set()
    for t in texts:
        if not t:
            continue
        for m in _EMAIL_RE.findall(t):
            emails.add(m.strip())
        for m in _PHONE_RE.findall(t):
            digits = re.sub(r'\D', '', m)
            if 7 <= len(digits) <= 15:
                phones.add(m.strip())
    return {"emails": sorted(emails)[:30], "phones": sorted(phones)[:30]}


def extract_pages_parallel(pages: list, lang: str, cost: CostAccumulator,
                           main_page_url: str = None, main_page_screenshot: str = None,
                           job_id: str = None):
    """Fetch + clean + extract knowledge for many pages concurrently (cheap model).

    Pages whose HTML is substantial but whose clean text is tiny look JS-rendered;
    those are re-fetched once through headless Chrome (bounded per job) so SPA sites
    work without the caller having to guess `use_selenium` upfront.

    Returns (chunks, structured_facts_list) where chunks are
    {url, title_suggestion, primary_category, extracted_chunk} and the second item
    holds the free deterministic JSON-LD/OpenGraph harvest of every fetched page.
    """
    results = []
    structured_list = []
    results_lock = threading.Lock()
    total = len(pages)
    done = {"n": 0}
    selenium_budget = {"n": 0}
    stats = {"cache_hits": 0, "selenium_fallbacks": 0}

    def _work(page):
        url = page["url"]
        try:
            html = fetch_url_html_content(url)
            if not html:
                return None

            # JS-rendered page? Retry once through Selenium within the per-job budget.
            clean_probe = clean_text_from_html(html, url)
            if (SELENIUM_AVAILABLE and len(clean_probe) < SELENIUM_FALLBACK_MIN_TEXT
                    and len(html) > SELENIUM_FALLBACK_MIN_HTML):
                with results_lock:
                    allowed = selenium_budget["n"] < SELENIUM_FALLBACK_MAX_PAGES
                    if allowed:
                        selenium_budget["n"] += 1
                if allowed:
                    rendered = fetch_page_html_via_selenium(url)
                    if rendered and len(clean_text_from_html(rendered, url)) > len(clean_probe):
                        logger.info(f"Selenium fallback improved content for {url}")
                        html = rendered
                        clean_probe = clean_text_from_html(html, url)
                        with results_lock:
                            stats["selenium_fallbacks"] += 1

            try:
                sd = extract_structured_data(html, url)
                with results_lock:
                    structured_list.append(sd)
            except Exception as e:
                logger.debug(f"Structured-data harvest failed for {url}: {e}")

            title = get_page_title_from_html(html)
            if not title:
                slug = unquote(urlparse(url).path or "").strip("/").split("/")[-1]
                title = page.get("title") or (slug.replace("-", " ").replace("_", " ").title()
                                               if slug else urlparse(url).netloc)
            screenshot = main_page_screenshot if (main_page_url and url == main_page_url) else None

            content_hash = hashlib.sha256(clean_probe.encode('utf-8', 'ignore')).hexdigest()
            data = None
            if not screenshot:  # screenshot-augmented extractions aren't cache-equivalent
                data = load_cached_extraction(url, lang, content_hash)
            if data is not None:
                with results_lock:
                    stats["cache_hits"] += 1
                logger.info(f"Extraction cache hit for {url}")
            else:
                data, p, c = extract_knowledge_from_page_with_openai(html, url, title, lang, screenshot)
                cost.add(OPENAI_MODEL_CHEAP, p, c)
                if data and not screenshot:
                    store_cached_extraction(url, lang, content_hash, data, OPENAI_MODEL_CHEAP)
            if data and data.get("extracted_chunk", "").strip():
                data = dict(data)
                data.setdefault("cluster", page.get("cluster"))
                return data
            return None
        except Exception as e:
            logger.error(f"Failed to extract {url}: {e}")
            return None
        finally:
            with results_lock:
                done["n"] += 1
                if job_id:
                    update_job_progress(job_id, f"Extracting knowledge {done['n']}/{total} pages...")

    workers = max(1, min(KB_EXTRACTION_WORKERS, total))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_work, pg) for pg in pages]
        for fut in concurrent.futures.as_completed(futures):
            r = fut.result()
            if r:
                with results_lock:
                    results.append(r)
    if stats["cache_hits"]:
        logger.info(f"Extraction cache: {stats['cache_hits']}/{total} pages served for free")
    return results, structured_list, stats


def fetch_clean_pages_parallel(pages: list, use_selenium: bool = False,
                               prefetched_html: dict = None, job_id: str = None):
    """Fetch and clean source pages without any model calls; attach stable fingerprints."""
    prepared, structured_list, failures = [], [], []
    result_lock = threading.Lock()
    total = len(pages)
    done = {"n": 0}
    selenium_budget = {"n": 0}
    stats = {"selenium_fallbacks": 0}
    prefetched_html = prefetched_html or {}
    robots_by_origin = {}
    for page in pages:
        parsed = urlparse(page.get("url", ""))
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in robots_by_origin:
            try:
                robots_by_origin[origin] = get_robots_parser(origin)
            except Exception as error:
                logger.debug(f"Could not read robots.txt for {origin}: {error}")
                robots_by_origin[origin] = None

    def _work(page):
        url = page["url"]

        def _record_failure(message, error_code="no_readable_content"):
            with result_lock:
                failures.append({
                    "url": url,
                    "error": message,
                    "error_code": error_code,
                })

        try:
            parsed = urlparse(url)
            origin = f"{parsed.scheme}://{parsed.netloc}"
            if not robots_allows(robots_by_origin.get(origin), url):
                _record_failure("The URL is disallowed by robots.txt.", "blocked")
                return None

            html = prefetched_html.get(url)
            if html is None:
                html = fetch_url_html_content(url)
            if not html:
                _record_failure("No HTML content was returned for this page.")
                return None

            clean_text = clean_text_from_html(html, url)
            if (SELENIUM_AVAILABLE and len(clean_text) < SELENIUM_FALLBACK_MIN_TEXT
                    and len(html) > SELENIUM_FALLBACK_MIN_HTML):
                with result_lock:
                    allowed = selenium_budget["n"] < SELENIUM_FALLBACK_MAX_PAGES
                    if allowed:
                        selenium_budget["n"] += 1
                if allowed:
                    try:
                        rendered = fetch_page_html_via_selenium(url)
                        rendered_text = clean_text_from_html(rendered, url) if rendered else ""
                        if len(rendered_text) > len(clean_text):
                            html, clean_text = rendered, rendered_text
                            with result_lock:
                                stats["selenium_fallbacks"] += 1
                    except Exception as selenium_error:
                        logger.debug(f"Selenium fallback failed for {url}: {selenium_error}")

            if not clean_text.strip():
                _record_failure("The page contained no readable text after cleaning.")
                return None

            title = get_page_title_from_html(html) or page.get("title") or None
            source_fingerprint = fingerprint_source_page(url, title, clean_text)
            try:
                structured = extract_structured_data(html, url)
                with result_lock:
                    structured_list.append(structured)
            except Exception as structured_error:
                logger.debug(f"Structured-data harvest failed for {url}: {structured_error}")

            return {
                "url": url,
                "title": title,
                "fingerprint": source_fingerprint,
                "text": clean_text,
                "cluster": page.get("cluster", "company_information"),
            }
        except Exception as error:
            failure = {
                "url": url,
                "error": str(error),
                "error_code": getattr(error, "error_code", crawl_failure_code(error)),
            }
            with result_lock:
                failures.append(failure)
            logger.warning(f"Source page fetch failed for {url}: {error}")
            return None
        finally:
            with result_lock:
                done["n"] += 1
                if job_id:
                    update_job_progress(job_id, f"Reading source page {done['n']}/{total}...")

    workers = max(1, min(KB_EXTRACTION_WORKERS, total))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_work, page) for page in pages]
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            if result:
                prepared.append(result)

    # Output order follows selected URL order, not thread completion order.
    order = {page["url"]: index for index, page in enumerate(pages)}
    prepared.sort(key=lambda page: order.get(page["url"], len(order)))
    return prepared, structured_list, stats, failures


def extract_prepared_pages_parallel(pages: list, lang: str, cost: CostAccumulator,
                                    main_page_url: str = None, main_page_screenshot: str = None,
                                    job_id: str = None):
    """Run the cheap per-page model on already fetched source text."""
    results, result_lock = [], threading.Lock()
    total, done = len(pages), {"n": 0}
    stats = {"cache_hits": 0}

    def _work(page):
        url, title, text = page["url"], page["title"], page["text"]
        screenshot = main_page_screenshot if main_page_url and url == main_page_url else None
        try:
            data = None if screenshot else load_cached_extraction(url, lang, page["fingerprint"])
            if data is not None:
                with result_lock:
                    stats["cache_hits"] += 1
            else:
                data, prompt_tokens, completion_tokens = extract_knowledge_from_page_with_openai(
                    "", url, title, lang, screenshot, cleaned_text=text
                )
                cost.add(OPENAI_MODEL_CHEAP, prompt_tokens, completion_tokens)
                if data and not screenshot:
                    store_cached_extraction(url, lang, page["fingerprint"], data, OPENAI_MODEL_CHEAP)
            if data and data.get("extracted_chunk", "").strip():
                data = dict(data)
                data.setdefault("url", url)
                data.setdefault("title_suggestion", title)
                data.setdefault("cluster", page.get("cluster"))
                return data
            return None
        except Exception as error:
            logger.error(f"Failed to extract {url}: {error}")
            return None
        finally:
            with result_lock:
                done["n"] += 1
                if job_id:
                    update_job_progress(job_id, f"Extracting knowledge {done['n']}/{total} pages...")

    workers = max(1, min(KB_EXTRACTION_WORKERS, total))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_work, page) for page in pages]
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            if result:
                with result_lock:
                    results.append(result)
    if stats["cache_hits"]:
        logger.info(f"Extraction cache: {stats['cache_hits']}/{total} pages served for free")
    return results, stats


def _batch_chunks_by_tokens(texts: list, token_budget: int) -> list:
    """Split chunk texts into batches whose combined token count stays under budget."""
    batches, current, current_tokens = [], [], 0
    for t in texts:
        tt = count_tokens(t) or max(1, len(t) // 4)
        if current and current_tokens + tt > token_budget:
            batches.append(current)
            current, current_tokens = [], 0
        current.append(t)
        current_tokens += tt
    if current:
        batches.append(current)
    return batches


def synthesize_section(section_key: str, section_title: str, chunks: list, base_url: str,
                       lang: str, cost: CostAccumulator, extra_context: str = "") -> str:
    """Merge all page-chunks for one canonical section into a clean, deduplicated section.

    Uses the STRONG model. Large sections are map-reduced (summarise per batch, then merge)
    so output is never bottlenecked by a single token-capped call.
    """
    texts = [c.get("extracted_chunk", "").strip() for c in chunks if c.get("extracted_chunk", "").strip()]
    if not texts and not extra_context.strip():
        return ""

    def _synth(body: str, note: str, include_extra: bool) -> str:
        ctx = (extra_context.strip() + "\n\n") if (include_extra and extra_context.strip()) else ""
        messages = [
            {"role": "developer", "content": (
                f"You are a senior technical writer assembling the '{section_title}' section of a "
                f"customer-support knowledge base. Write the ENTIRE section in {language_name(lang)} "
                f"(translate any source content into {language_name(lang)}; never use English unless "
                f"that is the target language). Merge the source extracts into one clean, well-structured "
                f"Markdown section. Remove duplicate facts. Resolve conflicts by keeping the most complete "
                f"version. Preserve phone numbers, emails, addresses, prices and policy clauses VERBATIM. "
                f"Do NOT invent information. Do NOT restate the section title and do NOT mention source "
                f"URLs. Start directly with the content; use heading level #### and below for any "
                f"sub-headings. Output Markdown only (no JSON, no code fences)."
            )},
            {"role": "user", "content": (
                f"{note}\n\n{ctx}Source extracts:\n\n{body}"
            )},
        ]
        content, p, c = llm_chat(messages, model=OPENAI_MODEL_STRONG,
                                 max_tokens=MAX_RESPONSE_TOKENS_SECTION_SYNTH,
                                 cost=cost, input_text_for_count=body)
        return content.strip()

    batches = _batch_chunks_by_tokens(texts, SECTION_SYNTH_INPUT_TOKEN_BUDGET) if texts else [[]]
    if len(batches) <= 1:
        body = "\n\n---\n\n".join(texts)
        return _synth(body, "Combine these extracts into the final section.", True)

    # Map each batch into a partial section, then reduce (merge) the partials.
    partials = []
    for i, batch in enumerate(batches):
        body = "\n\n---\n\n".join(batch)
        partials.append(_synth(body, f"This is batch {i+1}/{len(batches)} of a large section; "
                                     f"produce a thorough partial section (to be merged with others).", False))
    merge_body = "\n\n---\n\n".join(p for p in partials if p)
    return _synth(merge_body, "Merge these partial sections into one final, deduplicated section.", True)


def assemble_final_kb(section_outputs: dict, base_url: str, lang: str, cost: CostAccumulator,
                      target_doc_tokens: int = TARGET_DOC_TOKENS):
    """Stitch synthesised sections into the final single-context document.

    Generates an intro/overview + TOC (strong model), assembles sections in canonical order,
    and enforces a soft size budget by compressing lowest-priority sections first.
    Returns (markdown_document, assembly_meta).
    """
    present = [(k, section_title(k, lang)) for k in KB_SECTION_KEYS if section_outputs.get(k, "").strip()]
    assembly_meta = {"sections_present": [k for k, _ in present], "compressed_sections": [], "trimmed": False}

    overview_src = "\n\n".join(section_outputs.get(k, "") for k in ("company_overview", "products_services")
                               if section_outputs.get(k, "").strip())
    toc_lines = "\n".join(f"- {title}" for _, title in present)

    intro = ""
    try:
        messages = [
            {"role": "developer", "content": (
                f"You are writing the short opening of a customer-support knowledge base. "
                f"Write the opening ENTIRELY in {language_name(lang)} (never use English unless that is "
                f"the target language). Write 2-4 sentences introducing the company and what this "
                f"document covers. Markdown only, no headings, no lists."
            )},
            {"role": "user", "content": f"Website: {base_url}\n\nOverview material:\n{overview_src[:6000]}\n\nSections covered:\n{toc_lines}"},
        ]
        intro, p, c = llm_chat(messages, model=OPENAI_MODEL_STRONG,
                               max_tokens=MAX_RESPONSE_TOKENS_ASSEMBLY, cost=cost,
                               input_text_for_count=overview_src)
    except Exception as e:
        logger.warning(f"Intro generation failed: {e}")

    working = dict(section_outputs)

    def _doc_tokens():
        return sum(count_tokens(working.get(k, "")) for k, _ in present)

    if _doc_tokens() > target_doc_tokens:
        for k in reversed(KB_SECTION_PRIORITY):  # compress lowest-priority sections first
            if _doc_tokens() <= target_doc_tokens:
                break
            text = working.get(k, "").strip()
            if not text or count_tokens(text) < 400:
                continue
            try:
                messages = [
                    {"role": "developer", "content": (
                        f"Condense the following knowledge-base section to its essential, "
                        f"customer-relevant facts. Write ENTIRELY in {language_name(lang)} (never use "
                        f"English unless that is the target language). Keep contact details, prices and "
                        f"policy specifics verbatim. Markdown only, using heading level #### and below."
                    )},
                    {"role": "user", "content": text},
                ]
                condensed, p, c = llm_chat(messages, model=OPENAI_MODEL_STRONG,
                                           max_tokens=MAX_RESPONSE_TOKENS_SECTION_SYNTH // 2,
                                           cost=cost, input_text_for_count=text)
                if condensed.strip():
                    working[k] = condensed.strip()
                    assembly_meta["compressed_sections"].append(k)
                    assembly_meta["trimmed"] = True
            except Exception as e:
                logger.warning(f"Compression of section {k} failed: {e}")

    parts = []
    if intro.strip():
        parts.append(intro.strip())
    parts.append("## " + ("فهرست مطالب" if lang == "fa" else "Table of Contents"))
    parts.append(toc_lines)
    for k, title in present:
        parts.append(f"## {title}")
        parts.append(working.get(k, "").strip())
    document = "\n\n".join(parts).strip()
    assembly_meta["final_doc_tokens"] = count_tokens(document)
    return document, assembly_meta


def check_kb_completeness(document: str, lang: str, contact_candidates: dict,
                          cost: CostAccumulator) -> dict:
    """Cheap critic: flag which support-critical dimensions are present/missing."""
    has_email = bool(_EMAIL_RE.search(document)) or bool(contact_candidates.get("emails"))
    has_phone = bool(contact_candidates.get("phones")) or bool(re.search(r'\d{3}[\s-]?\d{3,}', document))
    report = {"has_email": has_email, "has_phone": has_phone, "doc_tokens": count_tokens(document)}
    try:
        messages = [
            {"role": "developer", "content": (
                "You audit a customer-support knowledge base for completeness. Output ONLY JSON: "
                "{\"has_contact\":bool, \"has_policies\":bool, \"has_faq\":bool, "
                "\"has_services\":bool, \"missing\":[\"...\"], \"notes\":\"<short, in " + language_name(lang) + ">\"}."
            )},
            {"role": "user", "content": f"Knowledge base document:\n\n{document[:12000]}"},
        ]
        content, p, c = llm_chat(messages, model=OPENAI_MODEL_CHEAP,
                                 max_tokens=MAX_RESPONSE_TOKENS_COMPLETENESS, json_mode=True,
                                 cost=cost, input_text_for_count=document[:12000])
        audit = parse_json_response(content)
        if isinstance(audit, dict):
            report.update(audit)
    except Exception as e:
        logger.warning(f"Completeness check failed: {e}")
        report["notes"] = "completeness check unavailable"
    return report


def run_knowledge_base_job(job_id, base_url, max_pages_for_kb, use_selenium, specific_pages=None,
                           depth="deep", target_doc_tokens=TARGET_DOC_TOKENS,
                           previous_fingerprint=None):
    logger.info(f"Starting KB job {job_id} for {base_url} (depth={depth}, budget={max_pages_for_kb})")
    with jobs_lock:
        jobs[job_id].update({"status": "running", "started_at": time.time()})

    cost = CostAccumulator()
    main_page_screenshot = None
    page_budget = max(1, min(int(max_pages_for_kb or DEFAULT_KB_PAGE_BUDGET), MAX_KB_PAGE_BUDGET))
    source_pages, page_results, source_fingerprint = [], [], None
    fetch_failures = []

    try:
        # Fetch and clean source content before any model call. Fingerprint-aware refreshes
        # can therefore return immediately without extraction, synthesis, or audit calls.
        update_job_progress(job_id, "Fetching homepage...")
        homepage_failure = None
        try:
            homepage_robots = get_robots_parser(base_url)
        except Exception as error:
            logger.debug(f"Could not read homepage robots rules: {error}")
            homepage_robots = None
        if not robots_allows(homepage_robots, base_url):
            homepage_failure = CrawlServiceError(
                "The homepage URL is disallowed by robots.txt.", "blocked"
            )
            main_page_html = None
        else:
            try:
                main_page_html = fetch_url_html_content(base_url)
            except Exception as error:
                homepage_failure = error
                logger.warning(f"Homepage fetch failed: {error}")
                main_page_html = None

        discovery_meta, selection_meta = {}, {}
        if specific_pages:
            update_job_progress(job_id, "Discovering core website pages...")
            discovered = discover_core_pages_only(base_url, specific_pages)
            specified_pages = [{"url": page["url"], "cluster": page.get("type", "company_information")}
                               for page in discovered]
            selected = select_specific_pages_deterministically(base_url, specified_pages, page_budget)
            discovery_meta = {"method": "specified", "count": len(selected)}
            selection_meta = {
                "candidates_total": len(discovered),
                "selection_method": "specified_stable",
                "llm_selection_skipped": True,
            }
        elif depth == "core":
            update_job_progress(job_id, "Discovering core website pages...")
            discovered = discover_core_pages_only(base_url)
            selected = [{"url": page["url"], "cluster": page.get("type", "company_information")}
                        for page in discovered][:page_budget]
            discovery_meta = {"method": "core_patterns", "count": len(selected)}
        else:
            update_job_progress(job_id, "Discovering all pages (Sitemap)...")
            candidates, discovery_meta, sitemap_meta = discover_all_candidate_urls(base_url, use_selenium)
            selected, selection_meta = select_knowledge_pages_deterministically(
                base_url, candidates, page_budget, sitemap_meta=sitemap_meta
            )

        with jobs_lock:
            jobs[job_id]["initial_found_pages_count"] = len(selected)
        logger.info(f"Selected {len(selected)} pages for source fingerprinting")

        update_job_progress(job_id, "Reading source pages...")
        prefetched = {base_url: main_page_html} if main_page_html else {}
        source_pages, per_page_structured, source_stats, fetch_failures = fetch_clean_pages_parallel(
            selected, use_selenium=use_selenium, prefetched_html=prefetched, job_id=job_id
        )
        if not source_pages:
            root_failure = next((failure for failure in fetch_failures
                                 if failure.get("url") == base_url), None)
            error_code = (root_failure or {}).get("error_code")
            if not error_code and homepage_failure:
                error_code = getattr(homepage_failure, "error_code", crawl_failure_code(homepage_failure))
            if not error_code and fetch_failures:
                error_code = fetch_failures[0].get("error_code")
            error_code = error_code or "no_readable_content"
            raise CrawlServiceError(
                "No readable page content could be fetched from the selected URLs.", error_code
            )

        source_fingerprint = fingerprint_source_pages(
            source_pages, [failure.get("url") for failure in fetch_failures]
        )
        page_results = [
            {"url": page["url"], "title": page["title"], "fingerprint": page["fingerprint"],
             "text": page["text"]}
            for page in source_pages
        ]
        page_fingerprints = {page["url"]: page["fingerprint"] for page in page_results}

        # The fingerprint covers failed pages too, so a match means the same pages loaded
        # with the same content and the same pages failed as last time.
        if previous_fingerprint and hmac.compare_digest(
                source_fingerprint, previous_fingerprint.lower()):
            finished_at = time.time()
            update_job_progress(job_id, "Source pages are unchanged; skipping model processing.")
            with jobs_lock:
                jobs[job_id].update({
                    "status": "completed",
                    "unchanged": True,
                    "source_fingerprint": source_fingerprint,
                    "page_fingerprints": page_fingerprints,
                    "pages": page_results,
                    "page_failures": fetch_failures,
                    "source_pages_count": len(page_results),
                    "extracted_pages_count": 0,
                    "cost_estimation": cost.cost_estimation(),
                    "finished_at": finished_at,
                })
                if page_budget == 1:
                    jobs[job_id]["single_page_text"] = page_results[0]["text"]
            notify_job_callback(job_id)
            return

        update_job_progress(job_id, "Detecting language...")
        lang = detect_site_language(main_page_html, base_url, selected, cost,
                                    prepared_pages=source_pages)
        with jobs_lock:
            jobs[job_id]["detected_target_language"] = lang
        logger.info(f"Detected target language: {lang} ({language_name(lang)})")

        # Expensive or visual enrichment is deferred until after the unchanged check.
        update_job_progress(job_id, "Capturing main page screenshot...")
        main_page_screenshot = (capture_full_page_screenshot(base_url)
                                if robots_allows(homepage_robots, base_url) else None)
        if not main_page_screenshot:
            logger.warning("Could not capture main page screenshot - continuing without visual context")

        update_job_progress(job_id, "Extracting website colors...")
        website_colors = {
            "main_background_color": "#ffffff", "primary_brand_color": "#000000",
            "background_color_description": "Default fallback", "brand_color_description": "Default fallback",
        }
        homepage_structured = None
        brand_kit_data = {}
        if main_page_html:
            try:
                homepage_structured = extract_structured_data(main_page_html, base_url)
            except Exception as error:
                logger.debug(f"Homepage structured-data harvest failed: {error}")
            try:
                brand_kit_data = build_brand_kit(
                    main_page_html, base_url, main_page_screenshot, homepage_structured,
                    session=SESSION, url_validator=validate_public_http_url,
                    reports_dir=REPORTS_DIR)
            except Exception as error:
                logger.warning(f"Brand kit extraction failed: {error}")
        palette = (brand_kit_data or {}).get("palette") or {}
        if palette.get("primary"):
            website_colors = {
                "main_background_color": palette.get("background") or "#ffffff",
                "primary_brand_color": palette["primary"],
                "background_color_description": "Measured from the page screenshot (dominant background).",
                "brand_color_description": "Measured deterministically from CSS variables / theme-color / screenshot pixels.",
            }
        elif main_page_html:
            try:
                colors, prompt_tokens, completion_tokens = extract_website_colors_with_openai(
                    main_page_html, base_url, main_page_screenshot
                )
                website_colors = colors
                cost.add(OPENAI_MODEL_CHEAP, prompt_tokens, completion_tokens)
            except Exception as error:
                logger.error(f"Failed to extract website colors: {error}")

        # Per-page model extraction uses the exact cleaned source text that was fingerprinted.
        update_job_progress(job_id, "Extracting knowledge from pages...")
        chunks, extraction_stats = extract_prepared_pages_parallel(
            source_pages, lang, cost, main_page_url=base_url,
            main_page_screenshot=main_page_screenshot, job_id=job_id,
        )
        extraction_stats.update(source_stats)
        extraction_stats["fetch_failures"] = fetch_failures
        logger.info(f"Extraction complete: {len(chunks)} non-empty chunks from {len(source_pages)} fetched pages")

        if homepage_structured:
            per_page_structured.append(homepage_structured)
        structured_facts = merge_structured_facts(per_page_structured)
        structured_summary = structured_facts_summary_text(structured_facts)
        if structured_summary:
            logger.info(f"Structured facts harvested:\n{structured_summary}")

        contact_candidates = harvest_contact_candidates([c.get("extracted_chunk", "") for c in chunks])
        org = structured_facts.get("organization") or {}
        for email in org.get("emails", []):
            if email not in contact_candidates["emails"]:
                contact_candidates["emails"].append(email)
        for phone in org.get("phones", []):
            if phone not in contact_candidates["phones"]:
                contact_candidates["phones"].append(phone)
        section_chunks = {key: [] for key in KB_SECTION_KEYS}
        for chunk in chunks:
            category = chunk.get("primary_category", "additional")
            if category not in section_chunks:
                category = "additional"
            section_chunks[category].append(chunk)

        section_outputs = {}
        structured_faq = structured_facts.get("faq") or []
        for key in KB_SECTION_KEYS:
            section_parts = section_chunks.get(key, [])
            extra = ""
            if key == "contact":
                extra_parts = []
                if contact_candidates["emails"] or contact_candidates["phones"]:
                    extra_parts.append(
                        "Detected contact candidates (verify against the extracts and include only "
                        "genuine ones; never fabricate):\n"
                        f"Emails: {', '.join(contact_candidates['emails']) or 'none'}\n"
                        f"Phones: {', '.join(contact_candidates['phones']) or 'none'}")
                if structured_summary:
                    extra_parts.append(
                        "Authoritative facts from the site's own structured data "
                        "(schema.org markup — treat as ground truth):\n" + structured_summary)
                extra = "\n\n".join(extra_parts)
            elif key == "company_overview" and structured_summary:
                extra = ("Authoritative facts from the site's own structured data "
                         "(schema.org markup — treat as ground truth):\n" + structured_summary)
            elif key == "faq" and structured_faq:
                faq_lines = "\n".join(f"Q: {item['question']}\nA: {item['answer']}"
                                      for item in structured_faq[:40])
                extra = ("FAQ pairs published in the site's own schema.org FAQPage markup "
                         "(authoritative — include them, translated into the target language):\n"
                         + faq_lines)
            if not section_parts and not extra:
                continue
            update_job_progress(job_id, f"Writing section: {section_title(key, lang)}...")
            output = synthesize_section(key, section_title(key, lang), section_parts,
                                        base_url, lang, cost, extra)
            if output.strip():
                section_outputs[key] = output

        update_job_progress(job_id, "Compiling comprehensive knowledge base...")
        if section_outputs:
            final_kb, assembly_meta = assemble_final_kb(
                section_outputs, base_url, lang, cost, target_doc_tokens
            )
        else:
            final_kb = f"# Knowledge Base\n\nNo extractable customer knowledge was found on {base_url}."
            assembly_meta = {"sections_present": [], "compressed_sections": [], "trimmed": False,
                             "final_doc_tokens": count_tokens(final_kb)}

        update_job_progress(job_id, "Auditing knowledge base completeness...")
        quality_report = check_kb_completeness(final_kb, lang, contact_candidates, cost)
        cost_estimation = cost.cost_estimation()
        pages_by_cluster = collections.Counter(page.get("cluster", "unknown") for page in source_pages)
        discovered_count = (discovery_meta.get("sitemap_urls", 0)
                            + discovery_meta.get("crawled_urls", 0)
                            + discovery_meta.get("count", 0))
        comprehensive_analysis = {
            "total_pages_analyzed": len(source_pages),
            "core_pages_processed": len(chunks),
            "pages_by_type": dict(pages_by_cluster),
            "extracted_pages_count": len(chunks),
            "source_pages_count": len(source_pages),
            "discovery": discovery_meta,
            "selection": selection_meta,
            "sections": assembly_meta,
            "contact_candidates": contact_candidates,
            "structured_facts": structured_facts,
            "extraction_stats": extraction_stats,
            "source_fingerprint": source_fingerprint,
            "page_fingerprints": page_fingerprints,
            "analysis_summary": (f"Deep pipeline: discovered ~{discovered_count} URLs, selected "
                                 f"{len(selected)} pages, extracted {len(chunks)}, "
                                 f"synthesised {len(section_outputs)} sections."),
        }

        update_job_progress(job_id, "Knowledge base generation completed successfully.")
        finished_at = time.time()
        report_metadata = {
            "extracted_pages_count": len(chunks),
            "source_pages_count": len(page_results),
            "pages": page_results,
            "source_fingerprint": source_fingerprint,
            "page_fingerprints": page_fingerprints,
            "page_failures": fetch_failures,
            "main_page_screenshot_captured": main_page_screenshot is not None,
            "website_colors": website_colors,
            "brand_kit": brand_kit_data,
            "comprehensive_analysis": comprehensive_analysis,
            "cost_estimation": cost_estimation,
            "detected_target_language": lang,
            "quality_report": quality_report,
            "depth": depth,
            "models": {"cheap": OPENAI_MODEL_CHEAP, "strong": OPENAI_MODEL_STRONG},
            "finished_at": finished_at,
        }
        try:
            save_knowledge_base_report(job_id, base_url, final_kb, report_metadata)
            logger.info(f"Successfully saved knowledge base report for job {job_id}")
        except Exception as error:
            logger.error(f"Failed to save knowledge base report for job {job_id}: {error}")

        with jobs_lock:
            jobs[job_id].update({
                "status": "completed",
                "unchanged": False,
                "source_fingerprint": source_fingerprint,
                "page_fingerprints": page_fingerprints,
                "pages": page_results,
                "page_failures": fetch_failures,
                "source_pages_count": len(page_results),
                "final_knowledge_base": final_kb,
                "extracted_pages_count": len(chunks),
                "main_page_screenshot_captured": main_page_screenshot is not None,
                "website_colors": website_colors,
                "brand_kit": brand_kit_data,
                "comprehensive_analysis": comprehensive_analysis,
                "cost_estimation": cost_estimation,
                "quality_report": quality_report,
                "finished_at": finished_at,
            })
            if page_budget == 1 and page_results:
                jobs[job_id]["single_page_text"] = page_results[0]["text"]
        notify_job_callback(job_id)
    except Exception as error:
        logger.error(f"KB Job {job_id} failed: {error}", exc_info=True)
        error_code = getattr(error, "error_code", crawl_failure_code(error))
        update_job_progress(job_id, "Failed to generate knowledge base.")
        with jobs_lock:
            jobs[job_id].update({
                "status": "failed", "error": str(error), "error_code": error_code,
                "finished_at": time.time(),
            })
            if source_fingerprint:
                jobs[job_id]["source_fingerprint"] = source_fingerprint
            if page_results:
                jobs[job_id]["pages"] = page_results
                jobs[job_id]["page_fingerprints"] = {
                    page["url"]: page["fingerprint"] for page in page_results
                }
                jobs[job_id]["source_pages_count"] = len(page_results)
            if fetch_failures:
                jobs[job_id]["page_failures"] = fetch_failures
        notify_job_callback(job_id)


# --- API Endpoints ---

@app.route('/api/analyze-company', methods=['POST'])
@require_api_key
def start_company_analysis():
    data, err = get_json_body()
    if err: return err
    url = data.get('url')
    if not url: return jsonify({"error": "Valid 'url' is required"}), 400
    url_error = validate_public_http_url(url)
    if url_error: return jsonify({"error": f"Invalid 'url': {url_error}"}), 400
    try:
        max_pages = int(data.get('max_pages', 10))
    except (TypeError, ValueError):
        return jsonify({"error": "'max_pages' must be an integer"}), 400
    max_pages = max(1, min(max_pages, MAX_COMPANY_ANALYSIS_PAGES))

    job_id = str(uuid.uuid4())
    register_job(job_id, {"id": job_id, "job_type": "company_analysis", "status": "pending",
                          "created_at": time.time()})

    thread = threading.Thread(target=run_company_analysis_job, args=(
        job_id, url, max_pages, bool(data.get('use_selenium', False))
    ))
    thread.start()
    return jsonify({"message": "Company analysis job started.", "job_id": job_id}), 202

@app.route('/api/qualify-prospects', methods=['POST'])
@require_api_key
def start_prospect_qualification():
    data, err = get_json_body()
    if err: return err
    if not all(k in data for k in ['user_profile', 'user_personas', 'prospect_urls']):
        return jsonify({"error": "Missing required fields"}), 400
    prospect_urls = data['prospect_urls']
    if not isinstance(prospect_urls, list) or not prospect_urls:
        return jsonify({"error": "'prospect_urls' must be a non-empty list of URLs"}), 400
    if len(prospect_urls) > MAX_PROSPECT_URLS_PER_JOB:
        return jsonify({"error": f"'prospect_urls' is limited to {MAX_PROSPECT_URLS_PER_JOB} URLs per job"}), 400
    for u in prospect_urls:
        url_error = validate_public_http_url(u)
        if url_error:
            return jsonify({"error": f"Invalid prospect URL {u!r}: {url_error}"}), 400

    job_id = str(uuid.uuid4())
    register_job(job_id, {"id": job_id, "job_type": "prospect_qualification", "status": "pending",
                          "created_at": time.time()})

    thread = threading.Thread(target=run_prospect_qualification_job, args=(
        job_id, data['user_profile'], data['user_personas'], prospect_urls
    ))
    thread.start()
    return jsonify({"message": "Prospect qualification job started.", "job_id": job_id}), 202

@app.route('/api/generate-knowledge-base', methods=['POST'])
@require_api_key
def start_knowledge_base_generation():
    data, err = get_json_body()
    if err: return err
    url = data.get('url')
    if not url:
        return _submission_error("Valid 'url' is required")
    url_error = validate_public_http_url(url)
    if url_error:
        return _submission_error(f"Invalid 'url': {url_error}", url_validation_error_code(url_error))

    # Get specific pages if provided
    specific_pages = data.get('specific_pages')
    if 'specific_pages' in data:
        if not isinstance(specific_pages, list):
            return _submission_error("'specific_pages' must be a list of URLs")
        if not specific_pages:
            return _submission_error("'specific_pages' must contain at least one URL when provided")
    for sp in (specific_pages or []):
        url_error = validate_public_http_url(sp)
        if url_error:
            return _submission_error(f"Invalid specific page URL {sp!r}: {url_error}",
                                     url_validation_error_code(url_error))

    # Optional new params (safe defaults preserve prior behaviour for existing callers).
    depth = str(data.get('depth', 'deep')).lower()
    if depth not in ('deep', 'core'):
        depth = 'deep'
    try:
        target_doc_tokens = int(data.get('target_doc_tokens', TARGET_DOC_TOKENS))
    except (TypeError, ValueError):
        target_doc_tokens = TARGET_DOC_TOKENS
    target_doc_tokens = max(2000, min(target_doc_tokens, 120000))
    try:
        max_pages = int(data.get('max_pages', DEFAULT_KB_PAGE_BUDGET))
    except (TypeError, ValueError):
        return _submission_error("'max_pages' must be an integer")
    max_pages = max(1, min(max_pages, MAX_KB_PAGE_BUDGET))

    previous_fingerprint = data.get('previous_fingerprint', data.get('source_fingerprint'))
    if previous_fingerprint is not None:
        if not isinstance(previous_fingerprint, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', previous_fingerprint):
            return _submission_error("'previous_fingerprint' must be a 64-character SHA-256 hex digest")
        previous_fingerprint = previous_fingerprint.lower()

    callback_url = data.get('callback_url')
    if callback_url is not None:
        if not isinstance(callback_url, str) or len(callback_url) > 2048:
            return _submission_error("'callback_url' must be a valid URL no longer than 2048 characters")
        if not callback_url.strip():
            callback_url = None
        else:
            callback_url = callback_url.strip()
            callback_url_error = validate_public_http_url(callback_url)
            if callback_url_error:
                return _submission_error(f"Invalid 'callback_url': {callback_url_error}")

    # Validate accessibility before starting a job, except when robots.txt disallows
    # the base URL; the background crawler will then report a fixed blocked code.
    try:
        preflight_robots = get_robots_parser(url)
        if robots_allows(preflight_robots, url):
            test_response = SESSION.head(url, timeout=10, allow_redirects=True)
            status_code = test_response.status_code
            reason = test_response.reason
            if status_code >= 400:
                # Many sites reject or mishandle HEAD while serving GET normally, so a
                # refused HEAD is only a hint; the GET result decides.
                probe_response = SESSION.get(url, timeout=10, allow_redirects=True, stream=True)
                try:
                    status_code = probe_response.status_code
                    reason = probe_response.reason
                finally:
                    probe_response.close()
            if status_code >= 400:
                return jsonify({
                    "error": f"Website is not accessible. HTTP {status_code}: {reason}",
                    "status_code": status_code,
                    "error_code": _http_error_code(status_code),
                }), 400
        else:
            logger.info(f"Skipping preflight request for robots-disallowed URL: {url}")
    except requests.exceptions.Timeout as error:
        return jsonify({
            "error": f"Website is not accessible. Connection timed out: {error}",
            "error_code": "timed_out",
        }), 400
    except requests.exceptions.ConnectionError as error:
        return jsonify({
            "error": "Website is not accessible. Connection failed - the website may be down or the URL is incorrect.",
            "details": "Please check the URL and try again",
            "error_code": crawl_failure_code(error),
        }), 400
    except requests.exceptions.RequestException as e:
        return jsonify({
            "error": f"Website is not accessible. Request failed: {str(e)}",
            "details": "Please check the URL format and try again",
            "error_code": crawl_failure_code(e),
        }), 400
    
    job_id = str(uuid.uuid4())
    register_job(job_id, {"id": job_id, "job_type": "knowledge_base_generation", "status": "pending",
                          "created_at": time.time(), "callback_url": callback_url})

    thread = threading.Thread(target=run_knowledge_base_job,
                              args=(job_id, url, max_pages, bool(data.get('use_selenium', False)), specific_pages),
                              kwargs={"depth": depth, "target_doc_tokens": target_doc_tokens,
                                      "previous_fingerprint": previous_fingerprint})
    thread.start()
    return jsonify({"message": "Knowledge base generation job started.", "job_id": job_id,
                    "depth": depth, "page_budget": max_pages,
                    "max_pages_limit": MAX_KB_PAGE_BUDGET}), 202

@app.route('/api/analyze-html', methods=['POST'])
@require_api_key
def analyze_html():
    data, err = get_json_body()
    if err: return err
    html_content = data.get('html_content')
    if not html_content: return jsonify({"error": "'html_content' is required"}), 400
    
    try:
        elements_map = extract_all_elements(html_content)
        return jsonify({"status": "success", "elements": elements_map}), 200
    except Exception as e:
        return jsonify({"error": "Internal Server Error", "message": str(e)}), 500

@app.route('/api/analyze-html-file', methods=['POST'])
@require_api_key
def analyze_html_file():
    if 'html_file' not in request.files: return jsonify({"error": "No 'html_file' uploaded"}), 400
    file = request.files['html_file']
    try:
        html_content = file.read().decode('utf-8')
        elements_map = extract_all_elements(html_content)
        return jsonify({"status": "success", "filename": file.filename, "elements": elements_map}), 200
    except Exception as e:
        return jsonify({"error": "Internal Server Error", "message": str(e)}), 500

@app.route('/api/scrape-page', methods=['POST'])
@require_api_key
def scrape_page():
    data, err = get_json_body()
    if err: return err
    url = data.get('url')
    if not url:
        return jsonify({"error": "'url' is required"}), 400
    url_error = validate_public_http_url(url)
    if url_error:
        return jsonify({"error": f"Invalid 'url': {url_error}"}), 400

    use_selenium = bool(data.get('use_selenium', False))
    include_screenshot = bool(data.get('include_screenshot', False))
    include_brand_kit = bool(data.get('include_brand_kit', False))

    if not openai_client:
        return jsonify({"error": "Vercel AI Gateway client is not configured"}), 500

    try:
        result = scrape_single_page(url, use_selenium=use_selenium,
                                    include_screenshot=include_screenshot,
                                    include_brand_kit=include_brand_kit)
        return jsonify(result), 200
    except (ConnectionError, ValueError) as e:
        return jsonify({"error": "URL is not accessible", "details": str(e)}), 400
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Unexpected error in /api/scrape-page for {url}: {e}", exc_info=True)
        return jsonify({"error": "Internal Server Error", "details": str(e)}), 500


@app.route('/api/jobs/<job_id>', methods=['GET'])
@require_api_key
def get_job_status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job: return jsonify({"error": "Job ID not found."}), 404
    
    # To avoid sending huge KB in status checks, send a preview.
    job_copy = job.copy()
    job_copy.pop("callback_url", None)
    if "final_knowledge_base" in job_copy and job_copy["final_knowledge_base"]:
        job_copy["final_knowledge_base_preview"] = job_copy["final_knowledge_base"][:500] + "..."
        # Only send the full KB if the job is completed
        if job_copy.get("status") != "completed":
            del job_copy["final_knowledge_base"]
            
    return jsonify(job_copy), 200

@app.route('/api/jobs', methods=['GET'])
@require_api_key
def list_all_jobs():
    with jobs_lock:
        jobs_list = [{
            "job_id": jid, "job_type": j.get("job_type"), "status": j.get("status"),
            "created_at": j.get("created_at"), "finished_at": j.get("finished_at")
        } for jid, j in jobs.items()]
    return jsonify({"jobs": sorted(jobs_list, key=lambda x: x.get('created_at', 0), reverse=True)})

@app.route('/api/health', methods=['GET'])
def health_check():
    return jsonify({"status": "ok", "message": "API is running", "selenium_available": SELENIUM_AVAILABLE}), 200

# --- Main Execution ---
if __name__ == '__main__':
    if not EXPECTED_SERVICE_API_KEY or not openai_client:
        logger.error("FATAL: Service cannot start due to missing configuration.")
        exit(1)
    
    os.makedirs(REPORTS_DIR, exist_ok=True)
    logger.info("Multi-Purpose Analyzer API starting...")
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
