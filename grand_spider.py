import os
import logging
import requests
import functools
import threading
import time
import uuid
import json
import csv
import re
import collections
import concurrent.futures
import xml.etree.ElementTree as ET
from flask import Flask, request, jsonify
from openai import OpenAI, APIError, RateLimitError, APITimeoutError, APIConnectionError
from dotenv import load_dotenv
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup
import tiktoken

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
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(threadName)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- API Keys & OpenAI Client ---
EXPECTED_SERVICE_API_KEY = os.getenv("SERVICE_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not EXPECTED_SERVICE_API_KEY:
    logger.error("FATAL: SERVICE_API_KEY environment variable not set.")
if not OPENAI_API_KEY:
    logger.error("FATAL: OPENAI_API_KEY environment variable not set.")

try:
    if OPENAI_API_KEY:
        openai_client = OpenAI(
            api_key=OPENAI_API_KEY,
            timeout=120.0,
            max_retries=3
        )
        logger.info("OpenAI client initialized successfully.")
    else:
        openai_client = None
        logger.error("OpenAI client could not be initialized: OPENAI_API_KEY is missing.")
except Exception as e:
    logger.error(f"Failed to initialize OpenAI client: {e}")
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
# Defaults keep BOTH on the cheap model, so cost/behaviour are identical to before until
# you opt in by setting OPENAI_MODEL_STRONG in the environment.
OPENAI_MODEL_CHEAP = os.getenv("OPENAI_MODEL_CHEAP", os.getenv("OPENAI_MODEL", "gpt-5-nano"))
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
MAX_RESPONSE_TOKENS_KB_EXTRACTION = 16000
MAX_RESPONSE_TOKENS_KB_COMPILATION = 16000

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

# --- Crawling & Discovery Constants ---
CRAWLER_USER_AGENT = 'GrandSpiderMultiPurposeAnalyzer/2.0 (+http://yourappdomain.com/bot)'
MAX_PAGES_FOR_KB_GENERATION = 20
MAX_URLS_FROM_SITEMAP_TO_PROCESS_TITLES = 200
MIN_DISCOVERED_PAGES_BEFORE_FALLBACK_CRAWL = 20
MAX_PAGES_FOR_FALLBACK_DISCOVERY_CRAWL = 30

# --- Directories ---
REPORTS_DIR = "reports"

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
    "Compiling comprehensive knowledge base...": "تدوین پایگاه دانش جامع...",
    # Overhauled deep pipeline
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
    """Single OpenAI chat call used by the overhauled KB pipeline.

    Returns (content_str, prompt_tokens, completion_tokens) and records usage into
    `cost` when provided. Uses the API-reported token usage when available.
    """
    if not openai_client:
        raise ConnectionError("OpenAI client not initialized.")
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

KB_WRITING_GUIDELINES_TEMPLATE = """
Guidelines for structuring the knowledge base in {target_language}:
1.  Introduction & Conclusion: Provide an overview and summary.
2.  Logical Flow: Organize thematically; synthesize related points.
3.  Clear Headings: Use Markdown H1 for major topics, H2/H3 for sub-topics (all in {target_language}).
4.  Clarity (in {target_language}): Use clear, active sentences. Explain jargon.
5.  Lists & Tables (in {target_language}): Represent steps as lists. Recreate tables.
6.  Examples & Edge Cases: Include if present (translate if needed).
7.  Q/A (in {target_language}): Represent FAQs clearly.
8.  LANGUAGE: Final KB MUST be entirely in {target_language}. Translate if needed.
9.  COMPLETENESS & ACCURACY: Reflect ALL relevant info. Ensure accuracy. For policy pages (Terms, Privacy, Returns), extract all sections, clauses, and specific conditions in full or summarize with extreme care to preserve legal/procedural meaning.
10. Format: Single, well-formatted Markdown document in {target_language}.
"""

STANDARD_CORE_PAGE_PATHS = {
    "english": ["about", "about-us", "company", "contact", "contact-us", "support", "help",
                "terms", "terms-and-conditions", "terms-of-service", "legal",
                "privacy", "privacy-policy",
                "shipping", "shipping-policy", "delivery",
                "returns", "return-policy", "refund-policy",
                "faq", "faqs", "how-to-order", "payment-methods", "services"],
    "persian": ["درباره-ما", "تماس-با-ما", "پشتیبانی", "راهنما", "شرایط", "قوانین-و-مقررات",
                "حریم-خصوصی", "سیاست-حفظ-حریم-خصوصی", "ارسال", "نحوه-ارسال",
                "بازگشت-کالا", "سوالات-متداول", "پرسش-های-متداول", "راهنمای-خرید", "خدمات"]
}

def discover_core_pages_only(base_url: str, specific_pages: list = None) -> list[dict]:
    """
    Discover only core website pages that are essential for chatbot knowledge.
    If specific_pages is provided, use those URLs directly.
    Otherwise, try to find common core pages.
    """
    discovered_pages = []
    
    # If specific pages are provided, use them directly
    if specific_pages:
        logger.info(f"Using provided specific pages: {len(specific_pages)} pages")
        for page_url in specific_pages:
            try:
                # Validate the URL is accessible
                response = requests.head(page_url, headers={'User-Agent': CRAWLER_USER_AGENT}, timeout=10, allow_redirects=True)
                if response.status_code < 400:
                    discovered_pages.append({
                        'url': page_url,
                        'title': 'N/A',
                        'type': 'specified_core_page'
                    })
                    logger.info(f"✓ Core page accessible: {page_url}")
                else:
                    logger.warning(f"✗ Core page not accessible (HTTP {response.status_code}): {page_url}")
            except requests.exceptions.RequestException as e:
                logger.warning(f"✗ Core page not accessible (Error): {page_url} - {e}")
        
        # Always include the main page
        if base_url not in [p['url'] for p in discovered_pages]:
            discovered_pages.insert(0, {
                'url': base_url,
                'title': 'Main Page',
                'type': 'main_page'
            })
        
        return discovered_pages
    
    # If no specific pages provided, try to discover core pages automatically
    logger.info(f"Auto-discovering core pages for {base_url}")
    
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
        try:
            response = requests.head(test_url, headers={'User-Agent': CRAWLER_USER_AGENT}, timeout=5, allow_redirects=True)
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
        if incoming_api_key != EXPECTED_SERVICE_API_KEY:
            log_key = incoming_api_key[:4] + '...' if incoming_api_key else 'None'
            logger.warning(f"Unauthorized access attempt: Invalid API key provided (starts with: {log_key}).")
            return jsonify({"error": "Unauthorized: Invalid API key"}), 401
        return f(*args, **kwargs)
    return decorated_function

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

def get_page_title_from_html(html_content):
    if not html_content: return "N/A"
    try:
        soup_title = BeautifulSoup(html_content, 'html.parser')
        title_tag = soup_title.find('title')
        return title_tag.string.strip() if title_tag and title_tag.string else "N/A"
    except Exception: return "N/A"

def fetch_url_html_content(url: str, for_lang_detect=False) -> str | None:
    headers = {'User-Agent': CRAWLER_USER_AGENT}
    try:
        if for_lang_detect:
            with requests.get(url, headers=headers, timeout=15, allow_redirects=True, stream=True) as r:
                r.raise_for_status()
                if 'text/html' not in r.headers.get('Content-Type', '').lower(): return None
                r.encoding = r.apparent_encoding or 'utf-8'
                html_chunk = ""
                for chunk in r.iter_content(chunk_size=1024, decode_unicode=True):
                    if chunk and len(html_chunk) < MAX_HTML_SNIPPET_FOR_LANG_DETECT:
                        html_chunk += chunk
                return html_chunk
        else:
            response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            response.raise_for_status()
            response.encoding = response.apparent_encoding or 'utf-8'
            return response.text[:MAX_HTML_CONTENT_LENGTH]
    except requests.exceptions.RequestException as req_err:
        logger.error(f"Error fetching HTML for {url}: {req_err}")
        if not for_lang_detect: raise ConnectionError(f"Failed to fetch URL content: {req_err}") from req_err
    return None

def preprocess_html_for_extraction(html: str) -> str:
    """Strip noise tags before sending to AI to save tokens."""
    soup = BeautifulSoup(html, 'html.parser')
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript", "meta", "link", "svg"]):
        tag.decompose()
    return str(soup)[:MAX_HTML_CONTENT_LENGTH]

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
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=True)
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


def get_sitemap_urls_from_xml(xml_content: str) -> list[str]:
    urls = []
    try:
        root = ET.fromstring(xml_content)
        namespaces = {'s': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
        for url_element in root.findall('.//s:loc', namespaces) or root.findall('.//loc'):
            if url_element.text: urls.append(url_element.text.strip())
        for sitemap_element in root.findall('.//s:sitemap/s:loc', namespaces) or root.findall('.//sitemap/loc'):
            if sitemap_element.text: urls.append(sitemap_element.text.strip())
    except ET.ParseError as e: logger.error(f"Failed to parse sitemap XML: {e}")
    return urls

def find_sitemap_urls(base_url: str) -> list[str]:
    logger.info(f"Attempting to find sitemaps for {base_url}")
    sitemap_paths_to_check = collections.deque()
    final_page_urls = set()
    processed_sitemap_urls = set()
    try:
        robots_url = urljoin(base_url, "/robots.txt")
        response = requests.get(robots_url, headers={'User-Agent': CRAWLER_USER_AGENT}, timeout=10)
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
            response = requests.get(sitemap_url, headers={'User-Agent': CRAWLER_USER_AGENT}, timeout=15)
            if response.status_code == 200:
                content_type = response.headers.get('Content-Type', '').lower()
                if 'xml' in content_type:
                    extracted_urls = get_sitemap_urls_from_xml(response.text)
                    for ext_url in extracted_urls:
                        if ext_url.endswith('.xml') and ext_url not in processed_sitemap_urls:
                            sitemap_paths_to_check.append(ext_url)
                            processed_sitemap_urls.add(ext_url)
                        elif not ext_url.endswith('.xml'):
                            final_page_urls.add(ext_url)
        except requests.exceptions.RequestException: pass
    logger.info(f"Found {len(final_page_urls)} unique page URLs from sitemaps.")
    return list(final_page_urls)


def simple_crawl_website(base_url, max_pages=10):
    logger.info(f"Starting simple crawl for {base_url}, max_pages={max_pages}")
    urls_to_visit = {base_url}
    visited_urls = set()
    found_pages_details = []
    base_domain = urlparse(base_url).netloc
    headers = {'User-Agent': CRAWLER_USER_AGENT}
    while urls_to_visit and len(found_pages_details) < max_pages:
        current_url = urls_to_visit.pop()
        if current_url in visited_urls or urlparse(current_url).netloc != base_domain:
            continue
        visited_urls.add(current_url)
        try:
            response = requests.get(current_url, headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            response.raise_for_status()
            if response.status_code == 200 and 'text/html' in response.headers.get('Content-Type', '').lower():
                 found_pages_details.append({'url': current_url, 'status': 'found'})
                 logger.info(f"[Simple] Found page ({len(found_pages_details)}/{max_pages}): {current_url}")
                 soup = BeautifulSoup(response.text, 'html.parser')
                 for link in soup.find_all('a', href=True):
                    absolute_url = urljoin(base_url, link['href'])
                    absolute_url = urlparse(absolute_url)._replace(fragment="").geturl()
                    if urlparse(absolute_url).netloc == base_domain and absolute_url not in visited_urls:
                         urls_to_visit.add(absolute_url)
        except requests.exceptions.RequestException as e:
            logger.error(f"[Simple] Error crawling URL {current_url}: {e}")
    return found_pages_details


def selenium_crawl_website(base_url, max_pages=10):
    if not SELENIUM_AVAILABLE: raise RuntimeError("Selenium is not available.")
    logger.info(f"Starting Selenium crawl for {base_url}, max_pages={max_pages}")
    urls_to_visit = {base_url}
    visited_urls = set()
    found_pages_details = []
    base_domain = urlparse(base_url).netloc
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
            current_url = urls_to_visit.pop()
            if current_url in visited_urls or urlparse(current_url).netloc != base_domain:
                continue
            visited_urls.add(current_url)
            try:
                driver.get(current_url)
                WebDriverWait(driver, SELENIUM_PAGE_LOAD_TIMEOUT).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
                time.sleep(SELENIUM_RENDER_WAIT_SECONDS)
                page_title = driver.title.strip() or "N/A"
                page_html = driver.page_source
                found_pages_details.append({'url': current_url, 'title': page_title, 'status': 'found_by_selenium', 'html_source': page_html})
                logger.info(f"[Selenium] Found page ({len(found_pages_details)}/{max_pages}): {current_url}")
                links = driver.find_elements(By.TAG_NAME, 'a')
                for link in links:
                    href = link.get_attribute('href')
                    if href:
                        absolute_url = urljoin(current_url, href)
                        absolute_url = urlparse(absolute_url)._replace(fragment="").geturl()
                        if urlparse(absolute_url).netloc == base_domain and absolute_url not in visited_urls:
                            urls_to_visit.add(absolute_url)
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


def scrape_single_page(url: str, use_selenium: bool = False, include_screenshot: bool = False) -> dict:
    """Synchronously scrape and extract structured knowledge from a single URL."""
    # Validate URL accessibility
    try:
        head_resp = requests.head(url, headers={'User-Agent': CRAWLER_USER_AGENT}, timeout=10, allow_redirects=True)
        if head_resp.status_code >= 400:
            raise ValueError(f"URL returned HTTP {head_resp.status_code}")
    except requests.exceptions.RequestException as e:
        raise ConnectionError(f"URL is not accessible: {e}") from e

    # Fetch HTML
    if use_selenium:
        if not SELENIUM_AVAILABLE:
            raise RuntimeError("Selenium is not available on this server.")
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
            html = driver.page_source
        finally:
            if driver:
                driver.quit()
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

    # Detect language
    html_snippet = html[:MAX_HTML_SNIPPET_FOR_LANG_DETECT]
    lang, _, _ = detect_language_from_html_with_openai(html_snippet, url)

    # Get page title
    title = get_page_title_from_html(html)

    # Extract knowledge
    result, p_tokens, c_tokens = extract_knowledge_from_page_with_openai(html, url, title, lang, screenshot_b64)

    # Compute cost
    input_cost = (p_tokens / 1_000_000) * PRICE_PER_INPUT_TOKEN_MILLION
    output_cost = (c_tokens / 1_000_000) * PRICE_PER_OUTPUT_TOKEN_MILLION

    return {
        "status": "success",
        "url": url,
        "title": result.get("title_suggestion", title),
        "detected_language": lang,
        "extracted_content": result.get("extracted_chunk", ""),
        "screenshot_captured": screenshot_captured,
        "cost_estimation": {
            "total_cost_usd": f"{(input_cost + output_cost):.6f}",
            "prompt_tokens": p_tokens,
            "completion_tokens": c_tokens
        }
    }


# --- OpenAI Helper Functions (Feature-Specific) ---

# For Company Analysis
def analyze_single_page_with_openai(page_content: str, url: str) -> str:
    if not openai_client: raise ConnectionError("OpenAI client not initialized.")
    prompt = f"Analyze ONLY the following text content from '{url}'. Describe the page's purpose. Be concise (1-2 sentences). Content: ```{page_content}```"
    completion = openai_client.chat.completions.create(model=OPENAI_MODEL, messages=[{"role": "user", "content": prompt}], max_completion_tokens=MAX_RESPONSE_TOKENS_PAGE)
    return completion.choices[0].message.content.strip()

def summarize_company_with_openai(page_summaries: list[dict], root_url: str) -> str:
    if not openai_client: raise ConnectionError("OpenAI client not initialized.")
    combined_text = f"Based on analyses of pages from {root_url}:\n\n" + "\n".join([f"- URL: {s['url']}\n  Summary: {s['description']}" for s in page_summaries])
    prompt = f"Synthesize these descriptions into a comprehensive overview of the company at {root_url}. Describe its main purpose, offerings, and mission. Summaries:\n{combined_text}"
    completion = openai_client.chat.completions.create(model=OPENAI_MODEL, messages=[{"role": "user", "content": prompt}], max_completion_tokens=MAX_RESPONSE_TOKENS_SUMMARY)
    return completion.choices[0].message.content.strip()

# For Prospect Qualification
def qualify_prospect_with_openai(page_content: str, prospect_url: str, user_profile: str, user_personas: list[str]):
    if not openai_client: raise ConnectionError("OpenAI client not initialized.")
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
    completion = openai_client.chat.completions.create(
        model=OPENAI_MODEL, messages=[{"role": "user", "content": prompt}],
        max_completion_tokens=MAX_RESPONSE_TOKENS_PROSPECT, response_format={"type": "json_object"}
    )
    result_json = json.loads(completion.choices[0].message.content)
    return result_json, completion.usage

# For Knowledge Base Generation
def detect_language_from_html_with_openai(html_snippet: str, url: str) -> tuple[str, int, int]:
    if not openai_client: raise ConnectionError("OpenAI client not initialized.")
    if not html_snippet or not html_snippet.strip(): return DEFAULT_TARGET_LANGUAGE, 0, 0
    messages = [
        {
            "role": "developer",
            "content": (
                "You are a language identification tool. "
                "Your ONLY output is a single 2-letter ISO 639-1 language code. "
                "No explanations, no punctuation, no extra text. "
                "Examples of valid outputs: en  fa  ar  fr  de  es  zh  tr"
            )
        },
        {
            "role": "user",
            "content": f"Identify the primary language of the visible text content in this HTML:\n\n{html_snippet}"
        }
    ]
    p_tokens = count_tokens(html_snippet)
    completion = openai_client.chat.completions.create(model=OPENAI_MODEL, messages=messages, max_completion_tokens=MAX_RESPONSE_TOKENS_LANG_DETECT)
    lang = completion.choices[0].message.content.strip().lower()
    c_tokens = completion.usage.completion_tokens if completion.usage else 0
    # Validate it's a proper language code
    if len(lang) == 2 and lang.isalpha():
        return lang, p_tokens, c_tokens
    else:
        return DEFAULT_TARGET_LANGUAGE, p_tokens, c_tokens

def analyze_all_urls_comprehensively(page_details: list[dict], root_url: str, lang: str) -> tuple[dict, int, int]:
    """Analyze ALL discovered URLs and categorize them comprehensively."""
    if not openai_client: raise ConnectionError("OpenAI client not initialized.")
    
    # Prepare URL list for analysis
    urls_list = [p['url'] for p in page_details]
    
    # If too many URLs, sample them intelligently with priority for important pages
    if len(urls_list) > 300:  # Reduced limit for better reliability
        import random
        random.seed(42)
        
        # Prioritize important pages
        priority_urls = []
        regular_urls = []
        
        for url in urls_list:
            url_lower = url.lower()
            if any(keyword in url_lower for keyword in ['about', 'contact', 'home', 'index', 'service', 'policy', 'help', 'faq', 'support', 'terms', 'privacy']):
                priority_urls.append(url)
            elif not any(skip in url_lower for skip in ['wp-content', 'assets', 'css', 'js', 'images', 'uploads', 'cache', 'admin', 'login', 'register', 'cart', 'checkout']):
                regular_urls.append(url)
        
        # Take priority pages and sample regular pages
        sampled_urls = priority_urls[:50]  # Take up to 50 priority pages
        remaining_slots = 250 - len(sampled_urls)  # Leave room for 250 total URLs
        if remaining_slots > 0 and regular_urls:
            sampled_urls.extend(random.sample(regular_urls, min(remaining_slots, len(regular_urls))))
        
        urls_list = sampled_urls
        logger.info(f"Sampled {len(urls_list)} URLs for analysis ({len(priority_urls)} priority pages found)")
    
    urls_text = "\n".join([f"- {url}" for url in urls_list])
    
    prompt = f"""Analyze ALL the following URLs from {root_url} and categorize them comprehensively.

    Categorize each URL into the following categories:
    1. company_info_pages: About us, contact, company information, policies, terms, privacy, FAQ, help, support
    2. product_pages: Individual product pages, product categories, brand pages, shopping pages
    3. service_pages: Services offered, features, capabilities, solutions
    4. technical_pages: Admin, login, cart, checkout, account, API, technical pages
    5. asset_pages: Images, CSS, JS, media files, static assets
    6. other_pages: Any other pages that don't fit above categories

    IMPORTANT: 
    - Only include URLs that actually exist and are accessible
    - Do not guess or assume URL patterns
    - Be thorough and comprehensive
    - Consider the URL structure and patterns

    URLs to analyze:
    {urls_text}

    Respond with a JSON object containing arrays of URLs for each category:
    {{
        "company_info_pages": ["url1", "url2", ...],
        "product_pages": ["url1", "url2", ...],
        "service_pages": ["url1", "url2", ...],
        "technical_pages": ["url1", "url2", ...],
        "asset_pages": ["url1", "url2", ...],
        "other_pages": ["url1", "url2", ...]
    }}
    """
    
    p_tokens = count_tokens(prompt)
    completion = openai_client.chat.completions.create(
        model=OPENAI_MODEL, messages=[{"role": "user", "content": prompt}],
        max_completion_tokens=MAX_RESPONSE_TOKENS_PAGE_SELECTION * 2, response_format={"type": "json_object"}
    )
    c_tokens = completion.usage.completion_tokens if completion.usage else 0
    
    try:
        response_content = completion.choices[0].message.content.strip()
        # Try to extract JSON from the response if it's wrapped in markdown or other text
        if '```json' in response_content:
            json_start = response_content.find('```json') + 7
            json_end = response_content.find('```', json_start)
            if json_end != -1:
                response_content = response_content[json_start:json_end].strip()
        elif '```' in response_content:
            json_start = response_content.find('```') + 3
            json_end = response_content.rfind('```')
            if json_end != -1 and json_end > json_start:
                response_content = response_content[json_start:json_end].strip()
        
        response_data = json.loads(response_content)
        return response_data, p_tokens, c_tokens
    except (json.JSONDecodeError, TypeError) as e:
        logger.error(f"Error parsing AI response for URL analysis: {e}")
        logger.error(f"Raw response content: {completion.choices[0].message.content[:500]}...")
        
        # Return a fallback structure with improved basic categorization
        company_keywords = ['about', 'contact', 'home', 'index', 'درباره', 'تماس', 'قوانین', 'شرایط', 'privacy', 'policy', 'terms', 'faq', 'help', 'support']
        service_keywords = ['service', 'support', 'help', 'خدمات', 'پشتیبانی', 'راهنما']
        skip_keywords = ['login', 'register', 'cart', 'checkout', 'wp-admin', 'wp-content', 'assets', 'css', 'js', 'images']
        
        company_pages = [page['url'] for page in page_details[:20] if any(keyword in page['url'].lower() for keyword in company_keywords)]
        service_pages = [page['url'] for page in page_details[:20] if any(keyword in page['url'].lower() for keyword in service_keywords)]
        product_pages = [page['url'] for page in page_details[:15] if not any(keyword in page['url'].lower() for keyword in skip_keywords + company_keywords + service_keywords)]
        
        fallback_response = {
            "company_info_pages": company_pages,
            "product_pages": product_pages,
            "service_pages": service_pages,
            "categories_summary": "Unable to perform detailed analysis due to parsing error, using enhanced URL pattern matching with Persian keywords"
        }
        logger.info(f"Using fallback categorization: {len(fallback_response['company_info_pages'])} company, {len(fallback_response['product_pages'])} product, {len(fallback_response['service_pages'])} service pages")
        return fallback_response, p_tokens, c_tokens

# Product catalog generation removed - focusing on knowledge base creation for chatbots
# If categories are found during analysis, they will be mentioned in the knowledge base

def identify_knowledge_rich_content_clusters(page_details: list[dict], root_url: str, lang: str) -> tuple[dict, int, int]:
    """Identify and prioritize knowledge-rich content clusters for chatbot training."""
    if not openai_client: raise ConnectionError("OpenAI client not initialized.")
    
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

    p_tokens = count_tokens(urls_text)
    completion = openai_client.chat.completions.create(
        model=OPENAI_MODEL, messages=messages,
        max_completion_tokens=MAX_RESPONSE_TOKENS_PAGE_SELECTION * 2, response_format={"type": "json_object"}
    )
    c_tokens = completion.usage.completion_tokens if completion.usage else 0
    
    try:
        response_content = completion.choices[0].message.content.strip()
        # Try to extract JSON from the response if it's wrapped in markdown or other text
        if '```json' in response_content:
            json_start = response_content.find('```json') + 7
            json_end = response_content.find('```', json_start)
            if json_end != -1:
                response_content = response_content[json_start:json_end].strip()
        elif '```' in response_content:
            json_start = response_content.find('```') + 3
            json_end = response_content.rfind('```')
            if json_end != -1 and json_end > json_start:
                response_content = response_content[json_start:json_end].strip()
        
        response_data = json.loads(response_content)
        return response_data, p_tokens, c_tokens
    except (json.JSONDecodeError, TypeError) as e:
        logger.error(f"Error parsing knowledge cluster analysis: {e}")
        logger.error(f"Raw response content: {completion.choices[0].message.content[:500]}...")
        
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

def compile_comprehensive_knowledge_base(extracted_content: dict, knowledge_clusters: dict, base_url: str, lang: str) -> tuple[str, int, int]:
    """Compile a comprehensive knowledge base from knowledge clusters."""
    if not openai_client: raise ConnectionError("OpenAI client not initialized.")
    
    # Prepare content sections from knowledge clusters
    content_sections = {}
    
    for cluster_name, cluster_content in extracted_content.items():
        if cluster_content:
            cluster_text = "\n\n".join([chunk.get('extracted_chunk', '') for chunk in cluster_content])
            content_sections[cluster_name] = cluster_text
    
    # Prepare comprehensive prompt with knowledge cluster sections
    content_sections_text = ""
    for cluster_name, cluster_text in content_sections.items():
        section_title = cluster_name.replace('_', ' ').title()
        content_sections_text += f"\n\n{section_title.upper()}:\n{cluster_text}"
    
    prompt = f"""Create a COMPREHENSIVE and DETAILED knowledge base for {base_url} in {lang}.

    Use ALL the following information extracted from knowledge-rich content clusters to create the most complete knowledge base possible:

    {content_sections_text}

    KNOWLEDGE ANALYSIS SUMMARY:
    - Total knowledge pages identified: {knowledge_clusters.get('total_knowledge_pages_identified', 0)}
    - Analysis summary: {knowledge_clusters.get('analysis_summary', 'Knowledge cluster analysis completed')}
    - Content clusters processed: {', '.join(extracted_content.keys())}

    CRITICAL REQUIREMENTS FOR CHATBOT KNOWLEDGE BASE:
    1. Create a VERY DETAILED and COMPREHENSIVE knowledge base optimized for AI chatbot use
    2. Include ALL information from all knowledge clusters in a structured, searchable format
    3. Create SEPARATE, DETAILED SECTIONS for each knowledge cluster type
    4. Focus EXCLUSIVELY on educational content, company information, troubleshooting guides, and customer service information
    5. COMPLETELY AVOID product specifications, individual product details, or product catalogs
    6. Use proper Markdown formatting with clear hierarchical structure (##, ###, ####)
    7. Write entirely in {lang}
    8. Include comprehensive tables, lists, and structured information that chatbots can easily parse
    9. Be extremely detailed with step-by-step instructions, complete procedures, and actionable information
    10. Include all contact details, policies, procedures, and FAQ-type information
    11. Structure content for optimal chatbot knowledge retrieval and customer assistance
    12. Create in-depth sections for:
        - Educational tutorials and how-to guides (with complete step-by-step instructions)
        - Troubleshooting guides (with detailed problem-solving steps)
        - Company information and policies (comprehensive contact info, terms, procedures)
        - Technical explanations (detailed concept definitions and explanations)
        - Buying guides (general advice and comparison criteria, NOT specific products)
        - Service information (support processes, warranty info, service procedures)
    13. Make each section comprehensive enough to answer complex customer questions
    14. Focus on KNOWLEDGE that helps customers learn, understand, and solve problems
    15. Avoid any product pricing, product specifications, or individual product recommendations

    Create a professional, well-structured, comprehensive knowledge base document optimized for AI chatbot customer service use, with detailed separate sections for each knowledge cluster.
    """
    
    p_tokens = count_tokens(prompt)
    completion = openai_client.chat.completions.create(
        model=OPENAI_MODEL, messages=[{"role": "user", "content": prompt}],
        max_completion_tokens=MAX_RESPONSE_TOKENS_KB_COMPILATION
    )
    c_tokens = completion.usage.completion_tokens if completion.usage else 0

    return completion.choices[0].message.content.strip(), p_tokens, c_tokens
        
def extract_website_colors_with_openai(html_content: str, url: str, screenshot_base64: str = None) -> tuple[dict, int, int]:
    """Extract website background color and primary brand color using AI analysis."""
    if not openai_client: 
        raise ConnectionError("OpenAI client not initialized.")
    
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
    
    p_tokens = count_tokens(prompt)
    completion = openai_client.chat.completions.create(
        model=OPENAI_MODEL_CHEAP, messages=[{"role": "user", "content": prompt}],
        max_completion_tokens=300, response_format={"type": "json_object"}
    )
    c_tokens = completion.usage.completion_tokens if completion.usage else 0
    
    try:
        response_content = completion.choices[0].message.content.strip()
        # Handle potential markdown wrapping
        if '```json' in response_content:
            json_start = response_content.find('```json') + 7
            json_end = response_content.find('```', json_start)
            if json_end != -1:
                response_content = response_content[json_start:json_end].strip()
        
        return json.loads(response_content), p_tokens, c_tokens
    except (json.JSONDecodeError, TypeError) as e:
        logger.error(f"Error parsing color extraction response: {e}")
        # Return a fallback response
        return {
            "main_background_color": "#ffffff",
            "primary_brand_color": "#000000",
            "background_color_description": "Default white background (fallback)",
            "brand_color_description": "Default black text (fallback)"
        }, p_tokens, c_tokens

def extract_knowledge_from_page_with_openai(html_content: str, url: str, title: str, lang: str, screenshot_base64: str = None) -> tuple[dict, int, int]:
    """Extract customer-relevant knowledge from one page into a structured chunk.

    Feeds CLEAN main-content text (not raw HTML) to the cheap model and classifies the page
    into exactly one canonical KB section, so the final document can be synthesised
    section-by-section instead of through a single token-capped compile call.
    Returns (data, prompt_tokens, completion_tokens) where data has keys:
    url, title_suggestion, primary_category, extracted_chunk.
    """
    if not openai_client: raise ConnectionError("OpenAI client not initialized.")

    clean_text = clean_text_from_html(html_content, url)
    section_keys_str = ", ".join(KB_SECTION_KEYS)

    user_text = f"""Extract ALL customer-relevant knowledge from this web page for a customer-support chatbot.

URL: {url}
Page Title: {title}

Capture (when present): contact details (phones, emails, addresses, hours, social links),
company background/mission, policies (shipping, returns, refunds, warranty, privacy, terms),
services and procedures, FAQ as question+answer pairs, branch/location info, payment methods
and ordering/checkout steps, and any educational / how-to / troubleshooting content.

Rules:
- Preserve phone numbers, emails, addresses, prices and policy clauses VERBATIM.
- Represent FAQs as **Q:** / **A:** pairs, but ONLY include questions that have an actual answer in the content; skip any question with no answer.
- Ignore navigation menus, cookie banners and unrelated product-catalog listings.
- Write the extracted_chunk entirely in {lang} (translate if the source language differs).
- If the page has no useful customer knowledge, return an empty string for extracted_chunk.

Classify the page's PRIMARY purpose into exactly one of: {section_keys_str}

Output ONLY this JSON:
{{
  "url": "{url}",
  "title_suggestion": "<short descriptive title in {lang}>",
  "primary_category": "<one of: {section_keys_str}>",
  "extracted_chunk": "<comprehensive Markdown in {lang} covering everything useful on this page>"
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
                f"Output language: {lang}. Keep contact details, prices and policy text verbatim. "
                f"Output ONLY valid JSON matching the schema provided."
            )
        },
        {"role": "user", "content": user_content_parts},
    ]

    completion = openai_client.chat.completions.create(
        model=OPENAI_MODEL_CHEAP, messages=messages,
        max_completion_tokens=MAX_RESPONSE_TOKENS_PAGE_EXTRACTION, response_format={"type": "json_object"}
    )
    usage = completion.usage
    p_tokens = usage.prompt_tokens if usage else count_tokens(clean_text)
    c_tokens = usage.completion_tokens if usage else 0
    try:
        data = parse_json_response(completion.choices[0].message.content)
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

def compile_final_knowledge_base_with_openai(chunks: list[dict], url: str, lang: str) -> tuple[str, int, int]:
    if not openai_client: raise ConnectionError("OpenAI client not initialized.")
    guidelines = KB_WRITING_GUIDELINES_TEMPLATE.format(target_language=lang)
    chunks_text = "\n\n".join([f"--- Chunk from {c.get('url', 'N/A')} ---\nTitle: {c.get('title_suggestion', 'N/A')}\nContent:\n{c.get('extracted_chunk', 'N/A')}" for c in chunks])
    messages = [
        {
            "role": "developer",
            "content": (
                f"You are a technical writer creating a structured knowledge base in {lang}. "
                f"Synthesize multiple page extracts into a single, deduplicated Markdown document. "
                f"Remove duplicate information. Resolve conflicts by keeping the most complete version."
            )
        },
        {
            "role": "user",
            "content": f"""Compile these page extracts from {url} into one cohesive knowledge base.

Required document structure (use ## for each section that has data):
## Company Overview
## Contact Information
## Products & Services
## Policies (Shipping / Returns / Warranty / Privacy / Terms)
## FAQ
## How to Order / Payment Methods
## Support & Troubleshooting
## Additional Information

Rules:
- Write entirely in {lang}
- Deduplicate: merge repeated information, do not list the same fact twice
- Use tables where data is tabular
- Use numbered lists for step-by-step processes
- Include all phone numbers, addresses, emails verbatim
- Include all policy clauses verbatim (do not paraphrase legal text)

Guidelines:
{guidelines}

Page extracts:
{chunks_text}"""
        }
    ]
    p_tokens = count_tokens(chunks_text)
    completion = openai_client.chat.completions.create(
        model=OPENAI_MODEL, messages=messages, max_completion_tokens=MAX_RESPONSE_TOKENS_KB_COMPILATION
    )
    c_tokens = completion.usage.completion_tokens if completion.usage else 0
    return completion.choices[0].message.content.strip(), p_tokens, c_tokens


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
    with jobs_lock: jobs[job_id]["status"] = "running"
    
    try:
        crawl_func = selenium_crawl_website if use_selenium else simple_crawl_website
        found_pages = crawl_func(url, max_pages)
        
        page_summaries = []
        for i, page in enumerate(found_pages):
            with jobs_lock: jobs[job_id]["progress"] = f"Analyzing page {i+1}/{len(found_pages)}"
            try:
                content = fetch_url_content(page['url'])
                if content:
                    summary = analyze_single_page_with_openai(content, page['url'])
                    page_summaries.append({'url': page['url'], 'description': summary})
            except Exception as e:
                logger.error(f"Failed to analyze page {page['url']}: {e}")
        
        with jobs_lock: jobs[job_id]["progress"] = "Summarizing company..."
        final_summary = summarize_company_with_openai(page_summaries, url)
        
        with jobs_lock:
            jobs[job_id].update({
                "status": "completed",
                "results": {"company_summary": final_summary, "analyzed_pages": page_summaries}
            })
    except Exception as e:
        logger.error(f"Job {job_id} failed: {e}", exc_info=True)
        with jobs_lock: jobs[job_id].update({"status": "failed", "error": str(e)})

def run_prospect_qualification_job(job_id, user_profile, user_personas, prospect_urls):
    logger.info(f"Starting prospect qualification job {job_id}")
    with jobs_lock: jobs[job_id]["status"] = "running"
    
    results = []
    total_prompt_tokens, total_completion_tokens = 0, 0
    for i, url in enumerate(prospect_urls):
        with jobs_lock: jobs[job_id]["progress"] = f"Qualifying {i+1}/{len(prospect_urls)}: {url}"
        result_entry = {"url": url, "status": "pending", "analysis": None, "error": None}
        try:
            page_content = fetch_url_content(url)
            if not page_content.strip(): raise ValueError("Fetched content is empty.")
            
            analysis, usage = qualify_prospect_with_openai(page_content, url, user_profile, user_personas)
            result_entry.update({"status": "completed", "analysis": analysis})
            total_prompt_tokens += usage.prompt_tokens
            total_completion_tokens += usage.completion_tokens
        except Exception as e:
            result_entry.update({"status": "failed", "error": str(e)})
        results.append(result_entry)
    
    csv_report_path = save_results_to_csv(job_id, results)
    input_cost = (total_prompt_tokens / 1_000_000) * PRICE_PER_INPUT_TOKEN_MILLION
    output_cost = (total_completion_tokens / 1_000_000) * PRICE_PER_OUTPUT_TOKEN_MILLION
    
    with jobs_lock:
        jobs[job_id].update({
            "status": "completed",
            "results": results,
            "csv_report_path": csv_report_path,
            "cost_estimation": {
                "total_cost_usd": f"{(input_cost + output_cost):.6f}",
                "prompt_tokens": total_prompt_tokens,
                "completion_tokens": total_completion_tokens
            }
        })

# =====================================================================================
# Overhauled KB pipeline: discovery -> deterministic pre-filter -> AI selection ->
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

    Returns (candidate_urls_including_base, discovery_meta).
    """
    meta = {"sitemap_urls": 0, "crawled_urls": 0, "method": None, "capped": False}

    sitemap_urls = []
    try:
        sitemap_urls = find_sitemap_urls(base_url)
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
        candidates = candidates[:MAX_DISCOVERY_URLS]

    return candidates, meta


def select_knowledge_pages(base_url: str, candidate_urls: list, lang: str, page_budget: int,
                           cost: CostAccumulator):
    """Pick the most knowledge-rich pages to deeply extract, within page_budget.

    Pipeline: deterministic pre-filter -> AI cluster categorisation (cheap model) ->
    priority-ordered selection. Always includes the site's main page.
    Returns (selected_pages, selection_meta); each page is {url, cluster}.
    """
    base_domain = urlparse(base_url).netloc
    kept, dropped_counts = prefilter_candidate_urls(candidate_urls, base_domain)
    logger.info(f"Pre-filter kept {len(kept)} of {len(candidate_urls)} URLs; dropped {dropped_counts}")

    selection_meta = {
        "candidates_total": len(candidate_urls),
        "after_prefilter": len(kept),
        "dropped_by_reason": dropped_counts,
        "clusters": None,
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

    if len(kept) > 1:
        try:
            page_details = [{"url": u} for u in kept]
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
                           job_id: str = None) -> list:
    """Fetch + clean + extract knowledge for many pages concurrently (cheap model).

    Returns chunk dicts: {url, title_suggestion, primary_category, extracted_chunk}.
    """
    results = []
    results_lock = threading.Lock()
    total = len(pages)
    done = {"n": 0}

    def _work(page):
        url = page["url"]
        try:
            html = fetch_url_html_content(url)
            if not html:
                return None
            title = get_page_title_from_html(html) or page.get("title", "N/A")
            screenshot = main_page_screenshot if (main_page_url and url == main_page_url) else None
            data, p, c = extract_knowledge_from_page_with_openai(html, url, title, lang, screenshot)
            cost.add(OPENAI_MODEL_CHEAP, p, c)
            if data and data.get("extracted_chunk", "").strip():
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
    return results


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
                f"customer-support knowledge base, written entirely in {lang}. Merge the source "
                f"extracts into one clean, well-structured Markdown section. Remove duplicate facts. "
                f"Resolve conflicts by keeping the most complete version. Preserve phone numbers, "
                f"emails, addresses, prices and policy clauses VERBATIM. Do NOT invent information. "
                f"Do NOT restate the section title and do NOT mention source URLs. Start directly with "
                f"the content; use heading level #### and below for any sub-headings. "
                f"Output Markdown only (no JSON, no code fences)."
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
    present = [(k, KB_SECTION_TITLES[k]) for k in KB_SECTION_KEYS if section_outputs.get(k, "").strip()]
    assembly_meta = {"sections_present": [k for k, _ in present], "compressed_sections": [], "trimmed": False}

    overview_src = "\n\n".join(section_outputs.get(k, "") for k in ("company_overview", "products_services")
                               if section_outputs.get(k, "").strip())
    toc_lines = "\n".join(f"- {title}" for _, title in present)

    intro = ""
    try:
        messages = [
            {"role": "developer", "content": (
                f"You are writing the short opening of a customer-support knowledge base in {lang}. "
                f"Write 2-4 sentences introducing the company and what this document covers. "
                f"Markdown only, no headings, no lists."
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
                        f"Condense the following knowledge-base section in {lang} to its essential, "
                        f"customer-relevant facts. Keep contact details, prices and policy specifics "
                        f"verbatim. Markdown only, using heading level ### and below."
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
                "\"has_services\":bool, \"missing\":[\"...\"], \"notes\":\"<short, in " + lang + ">\"}."
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
                           depth="deep", target_doc_tokens=TARGET_DOC_TOKENS):
    logger.info(f"Starting KB job {job_id} for {base_url} (depth={depth}, budget={max_pages_for_kb})")
    with jobs_lock: jobs[job_id].update({"status": "running", "started_at": time.time()})

    cost = CostAccumulator()
    main_page_screenshot = None
    page_budget = max(1, min(int(max_pages_for_kb or DEFAULT_KB_PAGE_BUDGET), MAX_KB_PAGE_BUDGET))

    try:
        # 1. Language detection
        update_job_progress(job_id, "Detecting language...")
        main_page_html = fetch_url_html_content(base_url, for_lang_detect=True)
        lang, p, c = detect_language_from_html_with_openai(main_page_html, base_url)
        cost.add(OPENAI_MODEL_CHEAP, p, c)
        with jobs_lock: jobs[job_id]["detected_target_language"] = lang

        # 2. Main page screenshot (visual context for the main-page extraction)
        update_job_progress(job_id, "Capturing main page screenshot...")
        main_page_screenshot = capture_full_page_screenshot(base_url)
        if not main_page_screenshot:
            logger.warning("Could not capture main page screenshot - continuing without visual context")

        # 2.5 Website colors (kept for response-shape compatibility)
        update_job_progress(job_id, "Extracting website colors...")
        website_colors = {
            "main_background_color": "#ffffff", "primary_brand_color": "#000000",
            "background_color_description": "Default fallback", "brand_color_description": "Default fallback",
        }
        if main_page_html:
            try:
                colors, p, c = extract_website_colors_with_openai(main_page_html, base_url, main_page_screenshot)
                website_colors = colors
                cost.add(OPENAI_MODEL_CHEAP, p, c)
            except Exception as e:
                logger.error(f"Failed to extract website colors: {e}")

        # 3. Discovery + intelligent selection
        discovery_meta, selection_meta = {}, {}
        if specific_pages:
            update_job_progress(job_id, "Discovering core website pages...")
            discovered = discover_core_pages_only(base_url, specific_pages)
            selected = [{"url": d["url"], "cluster": d.get("type", "company_information")} for d in discovered][:page_budget]
            discovery_meta = {"method": "specified", "count": len(selected)}
        elif depth == "core":
            update_job_progress(job_id, "Discovering core website pages...")
            discovered = discover_core_pages_only(base_url)
            selected = [{"url": d["url"], "cluster": d.get("type", "company_information")} for d in discovered][:page_budget]
            discovery_meta = {"method": "core_patterns", "count": len(selected)}
        else:
            update_job_progress(job_id, "Discovering all pages (Sitemap)...")
            candidates, discovery_meta = discover_all_candidate_urls(base_url, use_selenium)
            update_job_progress(job_id, "Identifying knowledge-rich content clusters...")
            selected, selection_meta = select_knowledge_pages(base_url, candidates, lang, page_budget, cost)

        with jobs_lock: jobs[job_id]["initial_found_pages_count"] = len(selected)
        logger.info(f"Selected {len(selected)} knowledge pages for extraction")

        # 4. Parallel per-page extraction (clean text -> cheap model)
        update_job_progress(job_id, "Extracting knowledge from pages...")
        chunks = extract_pages_parallel(
            selected, lang, cost,
            main_page_url=base_url, main_page_screenshot=main_page_screenshot, job_id=job_id,
        )
        logger.info(f"Extraction complete: {len(chunks)} non-empty chunks from {len(selected)} pages")

        # 5. Contact safety-net + group chunks by canonical section
        contact_candidates = harvest_contact_candidates([c.get("extracted_chunk", "") for c in chunks])
        section_chunks = {k: [] for k in KB_SECTION_KEYS}
        for ch in chunks:
            cat = ch.get("primary_category", "additional")
            if cat not in section_chunks:
                cat = "additional"
            section_chunks[cat].append(ch)

        # 6. Per-section synthesis (strong model; map-reduce for large sections)
        section_outputs = {}
        for key in KB_SECTION_KEYS:
            chs = section_chunks.get(key, [])
            extra = ""
            if key == "contact" and (contact_candidates["emails"] or contact_candidates["phones"]):
                extra = ("Detected contact candidates (verify against the extracts and include only "
                         "genuine ones; never fabricate):\n"
                         f"Emails: {', '.join(contact_candidates['emails']) or 'none'}\n"
                         f"Phones: {', '.join(contact_candidates['phones']) or 'none'}")
            if not chs and not extra:
                continue
            update_job_progress(job_id, f"Writing section: {KB_SECTION_TITLES[key]}...")
            out = synthesize_section(key, KB_SECTION_TITLES[key], chs, base_url, lang, cost, extra)
            if out.strip():
                section_outputs[key] = out

        # 7. Assemble the final single-context document
        update_job_progress(job_id, "Compiling comprehensive knowledge base...")
        if section_outputs:
            final_kb, assembly_meta = assemble_final_kb(section_outputs, base_url, lang, cost, target_doc_tokens)
        else:
            final_kb = f"# Knowledge Base\n\nNo extractable customer knowledge was found on {base_url}."
            assembly_meta = {"sections_present": [], "compressed_sections": [], "trimmed": False,
                             "final_doc_tokens": count_tokens(final_kb)}

        # 8. Completeness audit (cheap critic)
        update_job_progress(job_id, "Auditing knowledge base completeness...")
        quality_report = check_kb_completeness(final_kb, lang, contact_candidates, cost)

        # 9. Cost + analysis summary
        cost_estimation = cost.cost_estimation()
        pages_by_cluster = collections.Counter(pg.get("cluster", "unknown") for pg in selected)
        discovered_count = (discovery_meta.get("sitemap_urls", 0)
                            + discovery_meta.get("crawled_urls", 0)
                            + discovery_meta.get("count", 0))
        comprehensive_analysis = {
            "total_pages_analyzed": len(selected),
            "core_pages_processed": len(chunks),
            "pages_by_type": dict(pages_by_cluster),
            "extracted_pages_count": len(chunks),
            "discovery": discovery_meta,
            "selection": selection_meta,
            "sections": assembly_meta,
            "contact_candidates": contact_candidates,
            "analysis_summary": (f"Deep pipeline: discovered ~{discovered_count} URLs, selected "
                                 f"{len(selected)} knowledge pages, extracted {len(chunks)}, "
                                 f"synthesised {len(section_outputs)} sections."),
        }

        update_job_progress(job_id, "Knowledge base generation completed successfully.")

        # 10. Persist report
        try:
            save_knowledge_base_report(job_id, base_url, final_kb, {
                "extracted_pages_count": len(chunks),
                "main_page_screenshot_captured": main_page_screenshot is not None,
                "website_colors": website_colors,
                "comprehensive_analysis": comprehensive_analysis,
                "cost_estimation": cost_estimation,
                "detected_target_language": lang,
                "quality_report": quality_report,
                "depth": depth,
                "models": {"cheap": OPENAI_MODEL_CHEAP, "strong": OPENAI_MODEL_STRONG},
                "finished_at": time.time(),
            })
            logger.info(f"Successfully saved knowledge base report for job {job_id}")
        except Exception as e:
            logger.error(f"Failed to save knowledge base report for job {job_id}: {e}")

        with jobs_lock:
            jobs[job_id].update({
                "status": "completed",
                "final_knowledge_base": final_kb,
                "extracted_pages_count": len(chunks),
                "main_page_screenshot_captured": main_page_screenshot is not None,
                "website_colors": website_colors,
                "comprehensive_analysis": comprehensive_analysis,
                "cost_estimation": cost_estimation,
                "quality_report": quality_report,
                "finished_at": time.time(),
            })
    except Exception as e:
        logger.error(f"KB Job {job_id} failed: {e}", exc_info=True)
        update_job_progress(job_id, "Failed to generate knowledge base.")
        with jobs_lock: jobs[job_id].update({"status": "failed", "error": str(e), "finished_at": time.time()})


# --- API Endpoints ---

@app.route('/api/analyze-company', methods=['POST'])
@require_api_key
def start_company_analysis():
    data = request.get_json()
    url = data.get('url')
    if not url: return jsonify({"error": "Valid 'url' is required"}), 400
    
    job_id = str(uuid.uuid4())
    jobs[job_id] = {"id": job_id, "job_type": "company_analysis", "status": "pending"}
    
    thread = threading.Thread(target=run_company_analysis_job, args=(
        job_id, url, int(data.get('max_pages', 10)), bool(data.get('use_selenium', False))
    ))
    thread.start()
    return jsonify({"message": "Company analysis job started.", "job_id": job_id}), 202

@app.route('/api/qualify-prospects', methods=['POST'])
@require_api_key
def start_prospect_qualification():
    data = request.get_json()
    if not all(k in data for k in ['user_profile', 'user_personas', 'prospect_urls']):
        return jsonify({"error": "Missing required fields"}), 400

    job_id = str(uuid.uuid4())
    jobs[job_id] = {"id": job_id, "job_type": "prospect_qualification", "status": "pending"}

    thread = threading.Thread(target=run_prospect_qualification_job, args=(
        job_id, data['user_profile'], data['user_personas'], data['prospect_urls']
    ))
    thread.start()
    return jsonify({"message": "Prospect qualification job started.", "job_id": job_id}), 202

@app.route('/api/generate-knowledge-base', methods=['POST'])
@require_api_key
def start_knowledge_base_generation():
    data = request.get_json()
    url = data.get('url')
    if not url: 
        return jsonify({"error": "Valid 'url' is required"}), 400
    
    # Get specific pages if provided
    specific_pages = data.get('specific_pages')
    if specific_pages and not isinstance(specific_pages, list):
        return jsonify({"error": "'specific_pages' must be a list of URLs"}), 400

    # Optional new params (safe defaults preserve prior behaviour for existing callers).
    depth = str(data.get('depth', 'deep')).lower()
    if depth not in ('deep', 'core'):
        depth = 'deep'
    try:
        target_doc_tokens = int(data.get('target_doc_tokens', TARGET_DOC_TOKENS))
    except (TypeError, ValueError):
        target_doc_tokens = TARGET_DOC_TOKENS
    target_doc_tokens = max(2000, min(target_doc_tokens, 120000))
    max_pages = int(data.get('max_pages', DEFAULT_KB_PAGE_BUDGET))

    # Validate URL accessibility before starting job
    try:
        test_response = requests.head(url, timeout=10, allow_redirects=True)
        if test_response.status_code >= 400:
            return jsonify({
                "error": f"Website is not accessible. HTTP {test_response.status_code}: {test_response.reason}",
                "status_code": test_response.status_code
            }), 400
    except requests.exceptions.ConnectionError:
        return jsonify({
            "error": "Website is not accessible. Connection failed - the website may be down or the URL is incorrect.",
            "details": "Please check the URL and try again"
        }), 400
    except requests.exceptions.Timeout:
        return jsonify({
            "error": "Website is not accessible. Connection timed out - the website is taking too long to respond.",
            "details": "The website may be slow or experiencing issues"
        }), 400
    except requests.exceptions.RequestException as e:
        return jsonify({
            "error": f"Website is not accessible. Request failed: {str(e)}",
            "details": "Please check the URL format and try again"
        }), 400
    
    job_id = str(uuid.uuid4())
    jobs[job_id] = {"id": job_id, "job_type": "knowledge_base_generation", "status": "pending",
                    "created_at": time.time()}

    thread = threading.Thread(target=run_knowledge_base_job,
                              args=(job_id, url, max_pages, bool(data.get('use_selenium', False)), specific_pages),
                              kwargs={"depth": depth, "target_doc_tokens": target_doc_tokens})
    thread.start()
    return jsonify({"message": "Knowledge base generation job started.", "job_id": job_id,
                    "depth": depth, "page_budget": max_pages}), 202

@app.route('/api/analyze-html', methods=['POST'])
@require_api_key
def analyze_html():
    data = request.get_json()
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
    data = request.get_json()
    url = data.get('url')
    if not url:
        return jsonify({"error": "'url' is required"}), 400

    use_selenium = bool(data.get('use_selenium', False))
    include_screenshot = bool(data.get('include_screenshot', False))

    if not openai_client:
        return jsonify({"error": "OpenAI client is not configured"}), 500

    try:
        result = scrape_single_page(url, use_selenium=use_selenium, include_screenshot=include_screenshot)
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