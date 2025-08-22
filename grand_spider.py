import os
import logging
import requests
import functools
import threading
import time
import uuid
import json
import csv
import collections
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
            timeout=30.0,
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
OPENAI_MODEL = "gpt-4.1-nano-2025-04-14"

# --- Token Limits for Different Tasks ---
# For Company Analysis & Prospecting (from Code 1)
MAX_RESPONSE_TOKENS_PAGE = 300
MAX_RESPONSE_TOKENS_SUMMARY = 500
MAX_RESPONSE_TOKENS_PROSPECT = 800
# For Knowledge Base Generation (from Code 2)
DEFAULT_TARGET_LANGUAGE = "English"
MAX_RESPONSE_TOKENS_LANG_DETECT = 50
MAX_RESPONSE_TOKENS_PAGE_SELECTION = 2000
MAX_RESPONSE_TOKENS_KB_EXTRACTION = 8000
MAX_RESPONSE_TOKENS_KB_COMPILATION = 8000

# --- Crawling & Discovery Constants ---
CRAWLER_USER_AGENT = 'GrandSpiderMultiPurposeAnalyzer/2.0 (+http://yourappdomain.com/bot)'
MAX_PAGES_FOR_KB_GENERATION = 20
MAX_URLS_FROM_SITEMAP_TO_PROCESS_TITLES = 200
MIN_DISCOVERED_PAGES_BEFORE_FALLBACK_CRAWL = 20
MAX_PAGES_FOR_FALLBACK_DISCOVERY_CRAWL = 30

# --- Directories ---
REPORTS_DIR = "reports"

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

# Using a unified pricing model, assuming GPT-4.1-nano pricing is consistent
# Prices are per 1 Million tokens
PRICE_PER_INPUT_TOKEN_MILLION = 0.40
PRICE_PER_OUTPUT_TOKEN_MILLION = 1.20 # NOTE: This is an assumed price, adjust if official numbers differ.

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


# --- OpenAI Helper Functions (Feature-Specific) ---

# For Company Analysis
def analyze_single_page_with_openai(page_content: str, url: str) -> str:
    if not openai_client: raise ConnectionError("OpenAI client not initialized.")
    prompt = f"Analyze ONLY the following text content from '{url}'. Describe the page's purpose. Be concise (1-2 sentences). Content: ```{page_content}```"
    completion = openai_client.chat.completions.create(model=OPENAI_MODEL, messages=[{"role": "user", "content": prompt}], max_tokens=MAX_RESPONSE_TOKENS_PAGE)
    return completion.choices[0].message.content.strip()

def summarize_company_with_openai(page_summaries: list[dict], root_url: str) -> str:
    if not openai_client: raise ConnectionError("OpenAI client not initialized.")
    combined_text = f"Based on analyses of pages from {root_url}:\n\n" + "\n".join([f"- URL: {s['url']}\n  Summary: {s['description']}" for s in page_summaries])
    prompt = f"Synthesize these descriptions into a comprehensive overview of the company at {root_url}. Describe its main purpose, offerings, and mission. Summaries:\n{combined_text}"
    completion = openai_client.chat.completions.create(model=OPENAI_MODEL, messages=[{"role": "user", "content": prompt}], max_tokens=MAX_RESPONSE_TOKENS_SUMMARY)
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
        max_tokens=MAX_RESPONSE_TOKENS_PROSPECT, response_format={"type": "json_object"}
    )
    result_json = json.loads(completion.choices[0].message.content)
    return result_json, completion.usage

# For Knowledge Base Generation
def detect_language_from_html_with_openai(html_snippet: str, url: str) -> tuple[str, int, int]:
    if not openai_client: raise ConnectionError("OpenAI client not initialized.")
    if not html_snippet or not html_snippet.strip(): return DEFAULT_TARGET_LANGUAGE, 0, 0
    prompt = f"""From this HTML from {url}, identify the primary visible human language of the MAIN content. Respond with the full English name of the language (e.g., "English", "Farsi"). HTML: ```{html_snippet}```"""
    p_tokens = count_tokens(prompt)
    completion = openai_client.chat.completions.create(model=OPENAI_MODEL, messages=[{"role": "user", "content": prompt}], max_tokens=MAX_RESPONSE_TOKENS_LANG_DETECT)
    lang = completion.choices[0].message.content.strip().capitalize()
    c_tokens = completion.usage.completion_tokens if completion.usage else 0
    return (lang, p_tokens, c_tokens) if lang and "Undetermined" not in lang else (DEFAULT_TARGET_LANGUAGE, p_tokens, c_tokens)

def analyze_all_urls_comprehensively(page_details: list[dict], root_url: str, lang: str) -> tuple[dict, int, int]:
    """Analyze ALL discovered URLs and categorize them comprehensively."""
    if not openai_client: raise ConnectionError("OpenAI client not initialized.")
    
    # Prepare URL list for analysis
    urls_list = [p['url'] for p in page_details]
    
    # If too many URLs, sample them intelligently
    if len(urls_list) > 500:
        # Take first 200, last 200, and random sample from middle
        first_part = urls_list[:200]
        last_part = urls_list[-200:]
        middle_part = urls_list[200:-200]
        if len(middle_part) > 0:
            import random
            random.seed(42)
            middle_sample = random.sample(middle_part, min(100, len(middle_part)))
            urls_list = first_part + middle_sample + last_part
    
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
        max_tokens=MAX_RESPONSE_TOKENS_PAGE_SELECTION * 2, response_format={"type": "json_object"}
    )
    c_tokens = completion.usage.completion_tokens if completion.usage else 0
    
    try:
        response_data = json.loads(completion.choices[0].message.content)
        return response_data, p_tokens, c_tokens
    except (json.JSONDecodeError, TypeError) as e:
        logger.error(f"Error parsing AI response for URL analysis: {e}")
        return {}, p_tokens, c_tokens

def generate_comprehensive_product_catalog(page_details: list[dict], root_url: str, lang: str) -> tuple[dict, int, int]:
    """Generate a comprehensive product catalog from all discovered pages."""
    if not openai_client: raise ConnectionError("OpenAI client not initialized.")
    
    # Filter for likely product pages
    product_urls = []
    for page in page_details:
        url = page['url'].lower()
        # Skip obvious non-product pages
        if any(skip in url for skip in ['/cart/', '/login/', '/register/', '/checkout/', '/account/', '/admin/', '/api/', '/wp-admin/', '/wp-content/', '/assets/', '/css/', '/js/', '/images/', '/uploads/', '/cache/', '/temp/', '/tmp/', '/about/', '/contact/', '/terms/', '/privacy/', '/help/', '/faq/']):
            continue
        product_urls.append(page['url'])
    
    # Sample if too many
    if len(product_urls) > 300:
        import random
        random.seed(42)
        product_urls = random.sample(product_urls, 300)
    
    urls_text = "\n".join([f"- {url}" for url in product_urls])
    
    prompt = f"""Analyze the following URLs from {root_url} and generate a comprehensive product catalog.

    Based on the URL patterns and structure, identify:
    1. Product categories and types
    2. Brands and manufacturers
    3. Product features and specifications
    4. Price ranges and availability (IMPORTANT: ALL PRICES MUST BE IN TOMAN/IRT, NOT DOLLARS)
    5. Any special offers or promotions

    CRITICAL PRICING REQUIREMENTS:
    - ALL prices MUST be in Toman (IRT) currency
    - Do NOT use dollar signs ($) or USD
    - Use تومان or Toman for all price references
    - Convert any dollar estimates to Toman (approximate rate: 1 USD ≈ 500,000 Toman)
    - Price ranges should be in format like: "50,000,000 - 100,000,000 تومان"

    IMPORTANT:
    - Analyze URL patterns to understand product structure
    - Identify product categories, brands, and types
    - Estimate the scope and variety of products
    - Note any special features or services
    - ALL PRICES IN TOMAN ONLY

    URLs to analyze:
    {urls_text}

    Respond with a comprehensive JSON object:
    {{
        "total_products_estimated": number,
        "product_categories": ["category1", "category2", ...],
        "brands": ["brand1", "brand2", ...],
        "product_types": ["type1", "type2", ...],
        "price_ranges": ["range1 تومان", "range2 تومان", ...],
        "special_features": ["feature1", "feature2", ...],
        "products": [
            {{
                "name": "Product Name",
                "category": "Category",
                "brand": "Brand",
                "estimated_price": "Price Range in Toman",
                "url_pattern": "URL Pattern"
            }}
        ]
    }}
    """
    
    p_tokens = count_tokens(prompt)
    completion = openai_client.chat.completions.create(
        model=OPENAI_MODEL, messages=[{"role": "user", "content": prompt}],
        max_tokens=MAX_RESPONSE_TOKENS_KB_EXTRACTION, response_format={"type": "json_object"}
    )
    c_tokens = completion.usage.completion_tokens if completion.usage else 0
    
    try:
        response_data = json.loads(completion.choices[0].message.content)
        return response_data, p_tokens, c_tokens
    except (json.JSONDecodeError, TypeError) as e:
        logger.error(f"Error parsing AI response for product catalog: {e}")
        return {}, p_tokens, c_tokens

def compile_comprehensive_knowledge_base(extracted_content: dict, product_catalog: dict, url_analysis: dict, base_url: str, lang: str) -> tuple[str, int, int]:
    """Compile a comprehensive knowledge base from all gathered information."""
    if not openai_client: raise ConnectionError("OpenAI client not initialized.")
    
    # Prepare content sections
    company_info_text = ""
    if extracted_content.get('company_info'):
        company_info_text = "\n\n".join([chunk.get('extracted_chunk', '') for chunk in extracted_content['company_info']])
    
    product_info_text = ""
    if extracted_content.get('product_info'):
        product_info_text = "\n\n".join([chunk.get('extracted_chunk', '') for chunk in extracted_content['product_info']])
    
    service_info_text = ""
    if extracted_content.get('service_info'):
        service_info_text = "\n\n".join([chunk.get('extracted_chunk', '') for chunk in extracted_content['service_info']])
    
    # Prepare comprehensive prompt
    prompt = f"""Create a COMPREHENSIVE and DETAILED knowledge base for {base_url} in {lang}.

    Use ALL the following information to create the most complete knowledge base possible:

    COMPANY INFORMATION:
    {company_info_text}

    PRODUCT INFORMATION:
    {product_info_text}

    SERVICE INFORMATION:
    {service_info_text}

    PRODUCT CATALOG ANALYSIS:
    {json.dumps(product_catalog, indent=2, ensure_ascii=False)}

    URL ANALYSIS SUMMARY:
    - Total pages analyzed: {len(url_analysis.get('company_info_pages', []) + url_analysis.get('product_pages', []) + url_analysis.get('service_pages', []))}
    - Company info pages: {len(url_analysis.get('company_info_pages', []))}
    - Product pages: {len(url_analysis.get('product_pages', []))}
    - Service pages: {len(url_analysis.get('service_pages', []))}

    CRITICAL REQUIREMENTS:
    1. Create a VERY DETAILED and COMPREHENSIVE knowledge base
    2. Include ALL information from all sources
    3. Organize into clear sections with proper headings
    4. Include product catalog, company info, services, policies
    5. Make it as complete and thorough as possible
    6. Use proper Markdown formatting
    7. Write entirely in {lang}
    8. Include tables, lists, and structured information
    9. Be comprehensive and detailed
    10. ALL PRICES MUST BE IN TOMAN (IRT) - NEVER USE DOLLARS
    11. Convert any dollar prices to Toman (1 USD ≈ 500,000 Toman)
    12. Use تومان or Toman for all price references
    13. Format prices like: "50,000,000 تومان" or "50,000,000 - 100,000,000 تومان"

    Create a professional, well-structured, comprehensive knowledge base document with ALL prices in Toman currency.
    """
    
    p_tokens = count_tokens(prompt)
    completion = openai_client.chat.completions.create(
        model=OPENAI_MODEL, messages=[{"role": "user", "content": prompt}],
        max_tokens=MAX_RESPONSE_TOKENS_KB_COMPILATION
    )
    c_tokens = completion.usage.completion_tokens if completion.usage else 0
    
    return completion.choices[0].message.content.strip(), p_tokens, c_tokens
        
def extract_knowledge_from_page_with_openai(html_content: str, url: str, title: str, lang: str) -> tuple[dict, int, int]:
    if not openai_client: raise ConnectionError("OpenAI client not initialized.")
    prompt = f"""Analyze RAW HTML from '{url}' (Title: "{title}").
    
    CRITICAL: ALL output text (title, chunk) MUST be in {lang}, TRANSLATING if needed.
    
    Extract COMPREHENSIVE information including:
    - All text content, headings, paragraphs
    - Tables, lists, contact information
    - Product details, specifications, prices
    - Company information, policies, terms
    - Contact details, addresses, phone numbers
    - Branch locations, store information
    - Services, features, benefits
    - Any structured data, metadata
    
    IMPORTANT PRICING REQUIREMENTS:
    - ALL prices MUST be in Toman (IRT) currency
    - Do NOT use dollar signs ($) or USD
    - Use تومان or Toman for all price references
    - Convert any dollar prices to Toman (1 USD ≈ 500,000 Toman)
    - Format prices like: "50,000,000 تومان" or "50,000,000 - 100,000,000 تومان"
    
    Be THOROUGH and extract EVERYTHING meaningful. Do not skip any important information.
    
    JSON output: {{
        "url": "{url}", 
        "title_suggestion": "Comprehensive title in {lang}", 
        "extracted_chunk": "Detailed, comprehensive Markdown content in {lang} with all information (prices in Toman only)"
    }}
    
    RAW HTML: ```{html_content[:MAX_HTML_CONTENT_LENGTH]}```"""
    p_tokens = count_tokens(prompt)
    completion = openai_client.chat.completions.create(
        model=OPENAI_MODEL, messages=[{"role": "user", "content": prompt}],
        max_tokens=MAX_RESPONSE_TOKENS_KB_EXTRACTION, response_format={"type": "json_object"}
    )
    c_tokens = completion.usage.completion_tokens if completion.usage else 0
    return json.loads(completion.choices[0].message.content), p_tokens, c_tokens

def compile_final_knowledge_base_with_openai(chunks: list[dict], url: str, lang: str) -> tuple[str, int, int]:
    if not openai_client: raise ConnectionError("OpenAI client not initialized.")
    guidelines = KB_WRITING_GUIDELINES_TEMPLATE.format(target_language=lang)
    chunks_text = "\n\n".join([f"--- Chunk from {c.get('url', 'N/A')} ---\nTitle: {c.get('title_suggestion', 'N/A')}\nContent:\n{c.get('extracted_chunk', 'N/A')}" for c in chunks])
    prompt = f"""Compile a COMPREHENSIVE and DETAILED knowledge base from these chunks from {url}. Chunks are already in {lang}.
    
    Guidelines:\n{guidelines}
    
    IMPORTANT REQUIREMENTS:
    - Create a VERY DETAILED and COMPREHENSIVE knowledge base
    - Include ALL information from all chunks
    - Organize information logically with clear sections
    - Include tables, lists, contact details, product information
    - Make it as complete and thorough as possible
    - Do not omit any important information
    - Create a professional, well-structured document
    
    Synthesize into a single, coherent, comprehensive Markdown document in {lang}.
    
    Combined Chunks:\n{chunks_text}"""
    p_tokens = count_tokens(prompt)
    completion = openai_client.chat.completions.create(model=OPENAI_MODEL, messages=[{"role": "user", "content": prompt}], max_tokens=MAX_RESPONSE_TOKENS_KB_COMPILATION)
    c_tokens = completion.usage.completion_tokens if completion.usage else 0
    return completion.choices[0].message.content.strip(), p_tokens, c_tokens


# --- CSV Report Helper (for Prospecting) ---
def save_results_to_csv(job_id: str, results_data: list):
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

def run_knowledge_base_job(job_id, base_url, max_pages_for_kb, use_selenium):
    logger.info(f"Starting KB job {job_id} for {base_url}")
    with jobs_lock: jobs[job_id].update({"status": "running", "started_at": time.time()})
    
    total_prompt_tokens, total_completion_tokens = 0, 0
    try:
        # 1. Language Detection
        with jobs_lock: jobs[job_id]["progress"] = "Detecting language..."
        main_page_html = fetch_url_html_content(base_url, for_lang_detect=True)
        lang, p, c = detect_language_from_html_with_openai(main_page_html, base_url)
        total_prompt_tokens += p; total_completion_tokens += c
        with jobs_lock: jobs[job_id]["detected_target_language"] = lang

        # 2. Comprehensive Page Discovery
        with jobs_lock: jobs[job_id]["progress"] = "Discovering all pages (Sitemap)..."
        sitemap_urls = find_sitemap_urls(base_url)
        discovered_pages = [{'url': url, 'title': 'N/A'} for url in sitemap_urls]
        if len(discovered_pages) < MIN_DISCOVERED_PAGES_BEFORE_FALLBACK_CRAWL:
             with jobs_lock: jobs[job_id]["progress"] = "Performing fallback crawl..."
             fallback_pages = selenium_crawl_website(base_url, MAX_PAGES_FOR_FALLBACK_DISCOVERY_CRAWL) if use_selenium else simple_crawl_website(base_url, MAX_PAGES_FOR_FALLBACK_DISCOVERY_CRAWL)
             existing_urls = {p['url'] for p in discovered_pages}
             discovered_pages.extend([p for p in fallback_pages if p['url'] not in existing_urls])
        with jobs_lock: jobs[job_id]["initial_found_pages_count"] = len(discovered_pages)

        # 3. Comprehensive Analysis - Send ALL URLs to AI for categorization
        with jobs_lock: jobs[job_id]["progress"] = "Analyzing all discovered pages..."
        all_urls_analysis, p, c = analyze_all_urls_comprehensively(discovered_pages, base_url, lang)
        total_prompt_tokens += p; total_completion_tokens += c
        with jobs_lock: jobs[job_id]["comprehensive_url_analysis"] = all_urls_analysis

        # 4. Extract Content from Key Pages
        with jobs_lock: jobs[job_id]["progress"] = "Extracting content from key pages..."
        extracted_content = {}
        
        # Extract from main pages (company info, contact, etc.)
        if all_urls_analysis.get('company_info_pages'):
            company_content = []
            for page_url in all_urls_analysis['company_info_pages'][:5]:  # Limit to 5 pages
                try:
                    html = fetch_url_html_content(page_url)
                    if html:
                        chunk, p, c = extract_knowledge_from_page_with_openai(html, page_url, 'Company Info', lang)
                        company_content.append(chunk)
                        total_prompt_tokens += p; total_completion_tokens += c
                except Exception as e: logger.error(f"Failed to extract from {page_url}: {e}")
            extracted_content['company_info'] = company_content

        # Extract from product pages
        if all_urls_analysis.get('product_pages'):
            product_content = []
            for page_url in all_urls_analysis['product_pages'][:10]:  # Limit to 10 pages
                try:
                    html = fetch_url_html_content(page_url)
                    if html:
                        chunk, p, c = extract_knowledge_from_page_with_openai(html, page_url, 'Product Info', lang)
                        product_content.append(chunk)
                        total_prompt_tokens += p; total_completion_tokens += c
                except Exception as e: logger.error(f"Failed to extract from {page_url}: {e}")
            extracted_content['product_info'] = product_content

        # Extract from service pages
        if all_urls_analysis.get('service_pages'):
            service_content = []
            for page_url in all_urls_analysis['service_pages'][:5]:  # Limit to 5 pages
                try:
                    html = fetch_url_html_content(page_url)
                    if html:
                        chunk, p, c = extract_knowledge_from_page_with_openai(html, page_url, 'Service Info', lang)
                        service_content.append(chunk)
                        total_prompt_tokens += p; total_completion_tokens += c
                except Exception as e: logger.error(f"Failed to extract from {page_url}: {e}")
            extracted_content['service_info'] = service_content

        # 5. Generate Comprehensive Product Catalog
        with jobs_lock: jobs[job_id]["progress"] = "Generating comprehensive product catalog..."
        product_catalog, p, c = generate_comprehensive_product_catalog(discovered_pages, base_url, lang)
        total_prompt_tokens += p; total_completion_tokens += c
        with jobs_lock: jobs[job_id]["product_catalog"] = product_catalog

        # 6. Compile Final Comprehensive Knowledge Base
        with jobs_lock: jobs[job_id]["progress"] = "Compiling comprehensive knowledge base..."
        final_kb, p, c = compile_comprehensive_knowledge_base(extracted_content, product_catalog, all_urls_analysis, base_url, lang)
        total_prompt_tokens += p; total_completion_tokens += c

        input_cost = (total_prompt_tokens / 1_000_000) * PRICE_PER_INPUT_TOKEN_MILLION
        output_cost = (total_completion_tokens / 1_000_000) * PRICE_PER_OUTPUT_TOKEN_MILLION

        with jobs_lock:
            jobs[job_id].update({
                "status": "completed", 
                "final_knowledge_base": final_kb,
                "comprehensive_analysis": {
                    "total_pages_analyzed": len(discovered_pages),
                    "company_info_pages": len(all_urls_analysis.get('company_info_pages', [])),
                    "product_pages": len(all_urls_analysis.get('product_pages', [])),
                    "service_pages": len(all_urls_analysis.get('service_pages', [])),
                    "total_products_identified": len(product_catalog.get('products', []))
                },
                "cost_estimation": {
                    "total_cost_usd": f"{(input_cost + output_cost):.6f}",
                    "prompt_tokens": total_prompt_tokens,
                    "completion_tokens": total_completion_tokens
                }
            })
    except Exception as e:
        logger.error(f"KB Job {job_id} failed: {e}", exc_info=True)
        with jobs_lock: jobs[job_id].update({"status": "failed", "error": str(e)})


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
    if not url: return jsonify({"error": "Valid 'url' is required"}), 400
    
    job_id = str(uuid.uuid4())
    jobs[job_id] = {"id": job_id, "job_type": "knowledge_base_generation", "status": "pending"}

    thread = threading.Thread(target=run_knowledge_base_job, args=(
        job_id, url, int(data.get('max_pages', MAX_PAGES_FOR_KB_GENERATION)), bool(data.get('use_selenium', False))
    ))
    thread.start()
    return jsonify({"message": "Knowledge base generation job started.", "job_id": job_id}), 202

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