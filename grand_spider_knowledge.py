import os
import logging
import requests
import functools
import threading
import time
import uuid
import json
from flask import Flask, request, jsonify
from openai import OpenAI, APIError, RateLimitError, APITimeoutError, APIConnectionError
from dotenv import load_dotenv
from urllib.parse import urlparse, urljoin, quote
from bs4 import BeautifulSoup
import tiktoken
import xml.etree.ElementTree as ET
import collections

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
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(threadName)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- API Keys & OpenAI Client ---
EXPECTED_SERVICE_API_KEY = os.getenv("SERVICE_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not EXPECTED_SERVICE_API_KEY: logger.error("FATAL: SERVICE_API_KEY environment variable not set.")
if not OPENAI_API_KEY: logger.error("FATAL: OPENAI_API_KEY environment variable not set.")

try:
    if OPENAI_API_KEY:
        openai_client = OpenAI(api_key=OPENAI_API_KEY)
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
OPENAI_MODEL = "gpt-4.1-nano-2025-04-14"
DEFAULT_TARGET_LANGUAGE = "English"
MAX_RESPONSE_TOKENS_LANG_DETECT = 50
MAX_RESPONSE_TOKENS_PAGE_SELECTION = 2000
MAX_RESPONSE_TOKENS_KB_EXTRACTION = 4090
MAX_RESPONSE_TOKENS_KB_COMPILATION = 4090
CRAWLER_USER_AGENT = 'GrandSpiderKnowledgeBuilder/1.9 (+http://yourappdomain.com/bot)'
MAX_PAGES_FOR_KB_GENERATION = 15
MAX_URLS_FROM_SITEMAP_TO_PROCESS_TITLES = 200
MIN_DISCOVERED_PAGES_BEFORE_FALLBACK_CRAWL = 20
MAX_PAGES_FOR_FALLBACK_DISCOVERY_CRAWL = 30

try:
    try: TOKENIZER = tiktoken.encoding_for_model(OPENAI_MODEL)
    except KeyError:
        logger.warning(f"Tokenizer for model '{OPENAI_MODEL}' not found. Falling back to 'cl100k_base'.")
        TOKENIZER = tiktoken.get_encoding("cl100k_base")
except Exception as e:
    logger.error(f"Could not initialize tiktoken tokenizer: {e}. Token counting disabled.")
    TOKENIZER = None

PRICE_PER_INPUT_TOKEN_MILLION = 0.40
PRICE_PER_OUTPUT_TOKEN_MILLION = 1.20 # ASSUMED

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

jobs = {}; jobs_lock = threading.Lock()

# --- Authentication Decorator ---
def require_api_key(f):
    @functools.wraps(f)
    def decorated_function(*args, **kwargs):
        key = request.headers.get('api-key')
        if not EXPECTED_SERVICE_API_KEY: return jsonify({"error": "Service API key not configured."}), 500
        if not key: return jsonify({"error": "Missing 'api-key' header"}), 401
        if key != EXPECTED_SERVICE_API_KEY: return jsonify({"error": "Invalid API key"}), 401
        return f(*args, **kwargs)
    return decorated_function

# --- Helper Functions ---
def count_tokens(text: str) -> int:
    if TOKENIZER and text:
        try: return len(TOKENIZER.encode(text))
        except Exception: return 0
    return 0

def get_page_title_from_html(html_content):
    if not html_content: return "N/A"
    try:
        soup_title = BeautifulSoup(html_content, 'html.parser')
        title_tag = soup_title.find('title')
        if title_tag and title_tag.string: return title_tag.string.strip()
    except Exception: pass
    return "N/A"

def fetch_url_html_content(url: str, for_lang_detect=False) -> str | None:
    headers = {'User-Agent': CRAWLER_USER_AGENT}
    try:
        if for_lang_detect:
            with requests.get(url, headers=headers, timeout=15, allow_redirects=True, stream=True) as r:
                r.raise_for_status(); content_type = r.headers.get('Content-Type', '').lower()
                if 'text/html' not in content_type: return None
                r.encoding = r.apparent_encoding if r.encoding is None else r.encoding; html_chunk = ""
                for chunk in r.iter_content(chunk_size=1024, decode_unicode=True):
                    if chunk: html_chunk += chunk
                    if len(html_chunk) >= MAX_HTML_SNIPPET_FOR_LANG_DETECT: break
                return html_chunk[:MAX_HTML_SNIPPET_FOR_LANG_DETECT]
        else:
            response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            response.raise_for_status(); response.encoding = response.apparent_encoding
            return response.text[:MAX_HTML_CONTENT_LENGTH]
    except requests.exceptions.RequestException as req_err:
        logger.error(f"Error fetching content for {url}: {req_err}")
        if not for_lang_detect: raise ConnectionError(f"Failed to fetch URL content: {req_err}") from req_err
    return None

def get_sitemap_urls_from_xml(xml_content: str) -> list[str]:
    urls = []
    try:
        root = ET.fromstring(xml_content); namespaces = {'s': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
        for url_element in root.findall('.//s:loc', namespaces) or root.findall('.//loc'):
            if url_element.text: urls.append(url_element.text.strip())
        for sitemap_element in root.findall('.//s:sitemap/s:loc', namespaces) or root.findall('.//sitemap/loc'):
            if sitemap_element.text: urls.append(sitemap_element.text.strip())
    except ET.ParseError as e: logger.error(f"Failed to parse sitemap XML: {e}")
    except Exception as e: logger.error(f"Unexpected error parsing sitemap XML: {e}")
    return urls

def find_sitemap_urls(base_url: str) -> list[str]:
    logger.info(f"Attempting to find sitemaps for {base_url}")
    sitemap_paths_to_check = collections.deque(); final_page_urls = set(); processed_sitemap_urls = set()
    try:
        robots_url = urljoin(base_url, "/robots.txt")
        response = requests.get(robots_url, headers={'User-Agent': CRAWLER_USER_AGENT}, timeout=10)
        if response.status_code == 200:
            for line in response.text.splitlines():
                if line.strip().lower().startswith("sitemap:"):
                    sitemap_url = line.split(":", 1)[1].strip()
                    if sitemap_url not in processed_sitemap_urls: sitemap_paths_to_check.append(sitemap_url); processed_sitemap_urls.add(sitemap_url)
    except requests.exceptions.RequestException: pass
    common_sitemaps = ["/sitemap.xml", "/sitemap_index.xml", "/sitemap.php", "/sitemap.txt"]
    for common_path in common_sitemaps:
        sitemap_url = urljoin(base_url, common_path)
        if sitemap_url not in processed_sitemap_urls: sitemap_paths_to_check.append(sitemap_url); processed_sitemap_urls.add(sitemap_url)
    while sitemap_paths_to_check:
        sitemap_url = sitemap_paths_to_check.popleft()
        try:
            response = requests.get(sitemap_url, headers={'User-Agent': CRAWLER_USER_AGENT}, timeout=15)
            if response.status_code == 200:
                content_type = response.headers.get('Content-Type', '').lower(); sitemap_content = response.text
                if 'xml' in content_type or sitemap_url.endswith('.xml'):
                    extracted_urls = get_sitemap_urls_from_xml(sitemap_content)
                    for ext_url in extracted_urls:
                        if ext_url.endswith('.xml') and ext_url not in processed_sitemap_urls: sitemap_paths_to_check.append(ext_url); processed_sitemap_urls.add(ext_url)
                        elif not ext_url.endswith('.xml') and urlparse(ext_url).netloc == urlparse(base_url).netloc: final_page_urls.add(ext_url)
                elif 'text/plain' in content_type or sitemap_url.endswith('.txt'):
                    for line in sitemap_content.splitlines():
                        url_in_line = line.strip()
                        if url_in_line.startswith("http") and urlparse(url_in_line).netloc == urlparse(base_url).netloc: final_page_urls.add(url_in_line)
        except requests.exceptions.RequestException: pass
    logger.info(f"Found {len(final_page_urls)} unique page URLs from sitemaps for {base_url}.")
    return list(final_page_urls)

def fetch_titles_for_urls(urls_to_fetch_title: list[str], base_url_domain: str) -> list[dict]:
    pages_with_titles = []; threads = []; results_lock = threading.Lock(); MAX_TITLE_FETCH_THREADS = 5
    def fetch_single_title(url):
        try:
            with requests.get(url, headers={'User-Agent': CRAWLER_USER_AGENT}, timeout=10, stream=True, allow_redirects=True) as r:
                r.raise_for_status()
                if 'text/html' not in r.headers.get('Content-Type', '').lower(): return None
                html_chunk = ""; r.encoding = r.apparent_encoding if r.encoding is None else r.encoding
                for chunk in r.iter_content(chunk_size=1024*10, decode_unicode=True):
                    html_chunk += chunk
                    if '</title>' in html_chunk.lower() or len(html_chunk) > 20000: break
                title = get_page_title_from_html(html_chunk)
                with results_lock: pages_with_titles.append({'url': url, 'title': title, 'status': 'title_fetched', 'html_source': None})
        except requests.exceptions.RequestException: pass
    urls_to_process_for_title = urls_to_fetch_title[:MAX_URLS_FROM_SITEMAP_TO_PROCESS_TITLES]
    for url in urls_to_process_for_title:
        if urlparse(url).netloc != base_url_domain: continue
        thread = threading.Thread(target=fetch_single_title, args=(url,)); threads.append(thread); thread.start()
        if len(threads) >= MAX_TITLE_FETCH_THREADS:
            for t in threads: t.join(); threads = []
    for t in threads: t.join()
    logger.info(f"Finished fetching titles. Got titles for {len(pages_with_titles)} URLs.")
    return pages_with_titles

def probe_core_pages(base_url: str, target_language: str, existing_pages_map: dict) -> list[dict]:
    logger.info(f"Probing for standard core pages for {base_url} (lang: {target_language})")
    probed_pages_details_newly_found = []
    paths_to_try = list(STANDARD_CORE_PAGE_PATHS.get("english", [])); lang_key = target_language.lower()
    if lang_key != "english" and lang_key in STANDARD_CORE_PAGE_PATHS: paths_to_try.extend(list(STANDARD_CORE_PAGE_PATHS[lang_key]))
    elif lang_key != "english": paths_to_try.extend(["terms", "privacy", "contact", "about", "faq", "services"])
    unique_paths = sorted(list(set(paths_to_try)))
    for path in unique_paths:
        candidate_url = urljoin(base_url + ("/" if not base_url.endswith("/") else ""), path.lstrip("/"))
        if candidate_url in existing_pages_map:
            if not existing_pages_map[candidate_url].get('html_source'):
                try:
                    html_content = fetch_url_html_content(candidate_url)
                    if html_content:
                        existing_pages_map[candidate_url]['html_source'] = html_content
                        if existing_pages_map[candidate_url].get('title', "N/A") == "N/A": existing_pages_map[candidate_url]['title'] = get_page_title_from_html(html_content)
                except Exception: pass
            continue
        try:
            html_content = fetch_url_html_content(candidate_url)
            if html_content:
                title = get_page_title_from_html(html_content)
                page_data = {'url': candidate_url, 'title': title, 'status': 'found_by_probe', 'html_source': html_content}
                probed_pages_details_newly_found.append(page_data); existing_pages_map[candidate_url] = page_data
                logger.info(f"Successfully probed and found core page: {candidate_url} (Title: {title})")
        except ConnectionError: pass
        except Exception as e: logger.error(f"Unexpected error probing {candidate_url}: {e}")
    return probed_pages_details_newly_found

def simple_crawl_website(base_url, max_pages_to_discover=MAX_PAGES_FOR_FALLBACK_DISCOVERY_CRAWL):
    logger.info(f"[SimpleDiscoveryFallback] Starting crawl for {base_url}, max_pages={max_pages_to_discover}")
    visited_urls = set(); found_pages_details = []
    base_domain = urlparse(base_url).netloc
    headers = {'User-Agent': CRAWLER_USER_AGENT}; processed_count = 0; queue = collections.deque()
    try:
        main_page_html_for_links = fetch_url_html_content(base_url, for_lang_detect=True)
        if main_page_html_for_links:
            soup_main = BeautifulSoup(main_page_html_for_links, 'html.parser')
            for link in soup_main.find_all('a', href=True):
                href = link['href']; absolute_url = urljoin(base_url, href); absolute_url = urlparse(absolute_url)._replace(fragment="", query="").geturl()
                if urlparse(absolute_url).netloc == base_domain and absolute_url not in visited_urls and absolute_url not in queue: queue.append(absolute_url)
    except Exception as e: logger.error(f"[SimpleDiscoveryFallback] Could not get initial links from {base_url}: {e}")
    while queue and processed_count < max_pages_to_discover :
        current_url = queue.popleft()
        if current_url in visited_urls: continue
        visited_urls.add(current_url)
        page_title = "N/A"; page_html_content_for_links = None
        try:
            temp_resp = requests.get(current_url, headers=headers, timeout=10, allow_redirects=True, stream=True)
            temp_resp.raise_for_status(); content_type = temp_resp.headers.get('Content-Type', '').lower()
            if 'text/html' not in content_type: continue
            temp_resp.encoding = temp_resp.apparent_encoding; small_chunk_html = ""
            for chunk in temp_resp.iter_content(chunk_size=1024 * 20, decode_unicode=True):
                small_chunk_html += chunk
                if '</title>' in small_chunk_html.lower() or len(small_chunk_html) > 25000: break
            page_title = get_page_title_from_html(small_chunk_html); page_html_content_for_links = small_chunk_html; temp_resp.close()
        except requests.exceptions.RequestException: continue 
        except Exception: continue
        found_pages_details.append({'url': current_url, 'title': page_title, 'status': 'found_by_simple_fallback_crawl', 'html_source': None})
        processed_count += 1
        logger.debug(f"[SimpleDiscoveryFallback] Discovered page ({processed_count}/{max_pages_to_discover}): {current_url} (Title: {page_title})")
        if page_html_content_for_links and processed_count < max_pages_to_discover:
            soup_links = BeautifulSoup(page_html_content_for_links, 'html.parser')
            for link_tag in soup_links.find_all('a', href=True):
                href = link_tag['href']; absolute_url = urljoin(current_url, href); absolute_url = urlparse(absolute_url)._replace(fragment="", query="").geturl()
                if urlparse(absolute_url).netloc == base_domain and absolute_url not in visited_urls and absolute_url not in queue and \
                   len(queue) + processed_count < (max_pages_to_discover + 50): queue.append(absolute_url)
    logger.info(f"[SimpleDiscoveryFallback] Crawl finished. Discovered {len(found_pages_details)} new pages.")
    return found_pages_details

def selenium_crawl_website(base_url, max_pages_to_discover=MAX_PAGES_FOR_FALLBACK_DISCOVERY_CRAWL):
    if not SELENIUM_AVAILABLE: raise RuntimeError("Selenium not available.")
    logger.info(f"[SeleniumDiscovery] Starting crawl for {base_url}, max_pages={max_pages_to_discover}")
    visited_urls = set(); found_pages_details = []
    base_domain = urlparse(base_url).netloc; driver = None
    try:
        chrome_options = ChromeOptions(); chrome_options.add_argument("--headless"); chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage"); chrome_options.add_argument("--disable-gpu"); chrome_options.add_argument(f"user-agent={CRAWLER_USER_AGENT}")
        chrome_options.add_experimental_option('excludeSwitches', ['enable-logging'])
        service = ChromeService(ChromeDriverManager().install()); driver = webdriver.Chrome(service=service, options=chrome_options)
        driver.set_page_load_timeout(SELENIUM_PAGE_LOAD_TIMEOUT); queue = collections.deque()
        if base_url not in visited_urls : queue.append(base_url)
        processed_urls_count = 0
        while queue and processed_urls_count < max_pages_to_discover:
            current_url = queue.popleft()
            if current_url in visited_urls: continue
            if urlparse(current_url).netloc != base_domain: continue
            visited_urls.add(current_url)
            # logger.debug(f"[SeleniumDiscovery] Visiting: {current_url}") # Changed to debug
            try:
                driver.get(current_url); WebDriverWait(driver, SELENIUM_PAGE_LOAD_TIMEOUT).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
                if SELENIUM_RENDER_WAIT_SECONDS > 0: time.sleep(SELENIUM_RENDER_WAIT_SECONDS)
                try:
                    driver.execute_script("window.scrollTo(0, document.body.scrollHeight/2);"); time.sleep(0.5)
                    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);"); time.sleep(1)
                    driver.execute_script("window.scrollTo(0, 0);"); time.sleep(0.5)
                except Exception: pass
                page_title = driver.title.strip() if driver.title else "N/A"; page_html_source = driver.page_source
                found_pages_details.append({'url': current_url, 'title': page_title, 'status': 'found_by_selenium_discovery', 'html_source': page_html_source})
                processed_urls_count +=1
                logger.debug(f"[SeleniumDiscovery] Found page ({processed_urls_count}/{max_pages_to_discover}): {current_url} (Title: {page_title})") # DEBUG Level
                if processed_urls_count < max_pages_to_discover:
                    links = driver.find_elements(By.TAG_NAME, 'a')
                    for link in links:
                        href = link.get_attribute('href')
                        if href:
                            absolute_url = urljoin(current_url, href); absolute_url = urlparse(absolute_url)._replace(fragment="", query="").geturl()
                            if urlparse(absolute_url).netloc == base_domain and absolute_url not in visited_urls and absolute_url not in queue and \
                               len(queue) + processed_urls_count < (max_pages_to_discover + 50): queue.append(absolute_url)
            except Exception as page_err: logger.error(f"[SeleniumDiscovery] Error processing URL {current_url}: {page_err}")
    finally:
        if driver:
            try: driver.quit()
            except Exception: pass
    logger.info(f"[SeleniumDiscovery] Crawl finished. Discovered {len(found_pages_details)} pages (visited {processed_urls_count}).")
    return found_pages_details

# --- OpenAI Helper Functions ---
def detect_language_from_html_with_openai(html_snippet_param: str, url: str) -> tuple[str, int, int]:
    prompt_tokens, completion_tokens = 0, 0
    if not openai_client: raise ConnectionError("OpenAI client not initialized.")
    html_snippet = html_snippet_param if html_snippet_param is not None else ""
    if not html_snippet.strip():
        logger.warning(f"HTML snippet for language detection from {url} is empty. Defaulting language.")
        return DEFAULT_TARGET_LANGUAGE, prompt_tokens, completion_tokens
    html_for_prompt = html_snippet[:MAX_HTML_SNIPPET_FOR_LANG_DETECT]
    prompt = f"""
    From the following HTML snippet from the webpage {url}, extract the primary visible human language of the MAIN content.
    Ignore language of code, comments, or boilerplate text like "Accept Cookies" if possible, focus on the substantial text.
    Respond with the full name of the language in English (e.g., "English", "French", "Spanish", "Russian", "Farsi", "German").
    If the language is unclear, mixed, or no substantial text is found, respond with "Undetermined".

    HTML Snippet (first ~{len(html_for_prompt)} characters):
    \"\"\"
    {html_for_prompt}
    \"\"\"

    Primary language of main content:
    """
    prompt_tokens = count_tokens(prompt)
    try:
        completion_obj = openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": "You are a language detection AI. Respond with only the full English name of the language or 'Undetermined'."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=MAX_RESPONSE_TOKENS_LANG_DETECT, temperature=0.0
        )
        detected_language = completion_obj.choices[0].message.content.strip().capitalize()
        if completion_obj.usage:
            if completion_obj.usage.prompt_tokens: prompt_tokens = completion_obj.usage.prompt_tokens
            completion_tokens = completion_obj.usage.completion_tokens
        if not detected_language or detected_language == "Undetermined" or len(detected_language) > 20:
            logger.warning(f"Language detection for {url} was '{detected_language}'. Defaulting to {DEFAULT_TARGET_LANGUAGE}.")
            return DEFAULT_TARGET_LANGUAGE, prompt_tokens, completion_tokens
        logger.info(f"Detected language for {url} as: {detected_language}. Tokens (P/C): {prompt_tokens}/{completion_tokens}")
        return detected_language, prompt_tokens, completion_tokens
    except Exception as e:
        logger.error(f"Error during language detection for {url}: {e}", exc_info=True)
        return DEFAULT_TARGET_LANGUAGE, prompt_tokens, 0

def select_relevant_pages_for_kb_with_openai(page_details: list[dict], root_url: str, max_pages_to_select: int, target_language: str) -> tuple[list[dict], int, int]:
    prompt_tokens, completion_tokens = 0,0
    if not openai_client: raise ConnectionError("OpenAI client not initialized.")
    if not page_details: return [], prompt_tokens, completion_tokens
    pages_list_str = "\n".join([f"- URL: {p['url']} (Title: {p.get('title', 'N/A')})" for p in page_details])
    prompt = f"""
    You are an AI assistant tasked with selecting CRUCIAL pages from THE PROVIDED LIST of URLs from {root_url}
    to build a foundational knowledge base about the COMPANY/ORGANIZATION, its core operations, policies, and how to interact with it.
    The goal is a comprehensive overview of essential information, not a list of every product or blog.

    From the "List of available pages (URL and Title)" below, select pages that fit the criteria.
    *** YOU MUST ONLY SELECT URLs THAT ARE PRESENT IN THE PROVIDED LIST. DO NOT INVENT OR ASSUME OTHER URLs EXIST. ***

    HIGHLY PRIORITIZE and AIM TO INCLUDE ALL FOUND instances of the following types of pages (up to the total page limit of {max_pages_to_select}):
    - Main landing/home page (often just the root domain).
    - "About Us" / "درباره ما" / Company Information / Our Mission / History.
    - "Contact Us" / "تماس با ما" / Customer Support / Help Center / Support Channels.
    - "Terms and Conditions" / "قوانین و مقررات" / Terms of Use / Legal Information.
    - "Privacy Policy" / "سیاست حفظ حریم خصوصی".
    - "Shipping Policy" / "قوانین ارسال" / Delivery Information / Shipping Details.
    - "Return Policy" / "قوانین بازگشت کالا" / Refund Policy / Exchange Policy.
    - "FAQ" / "سوالات متداول" / Frequently Asked Questions.
    - "How to Order" / "راهنمای خرید" / Payment Methods / Ordering Process / User Guides.
    - "Services" / "خدمات" (if these describe broad service categories or how core services work, NOT individual product/service listings).
    - Any other pages clearly related to company policies, customer support processes, or general operational information.

    STRONGLY DE-PRIORITIZE and AIM TO EXCLUDE (unless specifically asked for or if the site has very few other pages):
    - Individual Product Detail Pages (e.g., URLs containing '/product/', '/item/', '/shop/.../[product-name]').
    - Individual Blog Posts or Articles (unless the title VERY CLEARLY indicates it's a core policy, a comprehensive official guide to a main service, or the "About Us" story).
    - Product Category Listing Pages that primarily list products/blogs without substantial unique informational text.
    - Generic functional pages (Login, Register, Cart, Wishlist) unless they contain significant policy/help text.

    Your selection should focus on pages providing lasting, structural information.
    Aim to select up to {max_pages_to_select} of the MOST relevant pages fitting these criteria. If the list contains many suitable foundational pages, prioritize the ones listed above and select them up to the limit. If only a few truly foundational pages are present in the list, select only those few.

    List of available pages (URL and Title) --- CHOOSE ONLY FROM THIS LIST:
    {pages_list_str}

    Respond with a JSON array of objects. Each object must have "url" (string, from the list above) and optionally "reason" (string, explaining its importance for a foundational KB).
    The "reason" text MUST be in {target_language}.
    Return an empty array [] if no suitable pages are found in the list.
    """
    prompt_tokens = count_tokens(prompt)
    try:
        completion_obj = openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "system", "content": f"AI page selector for foundational company KB. Prioritize About, Contact, Policies, FAQ, core Services overviews. Exclude individual products/blogs. Select ONLY from provided URLs, up to the limit if suitable pages exist. Respond JSON. 'reason' MUST be in {target_language}."},
                      {"role": "user", "content": prompt}],
            max_tokens=MAX_RESPONSE_TOKENS_PAGE_SELECTION, temperature=0.1, response_format={"type": "json_object"})
        response_content = completion_obj.choices[0].message.content.strip()
        if completion_obj.usage:
            if completion_obj.usage.prompt_tokens: prompt_tokens = completion_obj.usage.prompt_tokens
            completion_tokens = completion_obj.usage.completion_tokens
        selected_pages_data = json.loads(response_content)
        selected_pages = []
        if isinstance(selected_pages_data, dict):
            processed_list = None
            for key in ["selected_pages", "pages", "relevant_pages", "urls"]:
                if key in selected_pages_data and isinstance(selected_pages_data[key], list):
                    processed_list = selected_pages_data[key]; break
            if processed_list is None and selected_pages_data:
                first_value = next(iter(selected_pages_data.values()), None)
                if isinstance(first_value, list): processed_list = first_value
            selected_pages = processed_list if processed_list is not None else []
        elif isinstance(selected_pages_data, list): selected_pages = selected_pages_data
        else: logger.error(f"OpenAI page selection returned unexpected format: {type(selected_pages_data)}")

        valid_selected_pages = []
        all_provided_urls = {p['url'] for p in page_details}
        for item in selected_pages:
            if isinstance(item, dict) and "url" in item:
                selected_url = item["url"]
                if selected_url not in all_provided_urls:
                    logger.warning(f"AI selected URL '{selected_url}' which was NOT in the provided list. Discarding.")
                    continue
                original_page_detail = next((p for p in page_details if p['url'] == selected_url), None)
                title = original_page_detail['title'] if original_page_detail else "N/A"
                valid_selected_pages.append({"url": selected_url, "title": title, "reason": item.get("reason", "N/A")})
            else: logger.warning(f"OpenAI page selection returned an invalid item in list: {item}")
        logger.info(f"OpenAI selected {len(valid_selected_pages)} pages for KB. Target: {target_language}. Tokens (P/C): {prompt_tokens}/{completion_tokens}")
        return valid_selected_pages[:max_pages_to_select], prompt_tokens, completion_tokens
    except json.JSONDecodeError as e:
        logger.error(f"OpenAI page selection JSON parsing error: {e}. Response: {response_content}", exc_info=True)
        return [], prompt_tokens, completion_tokens
    except Exception as e:
        logger.error(f"Error during OpenAI page selection for {root_url} (target: {target_language}): {e}", exc_info=True)
        raise

def extract_knowledge_from_page_with_openai(page_html_content: str, url: str, page_title: str, target_language: str) -> tuple[dict, int, int]:
    prompt_tokens, completion_tokens = 0, 0
    if not openai_client: raise ConnectionError("OpenAI client not initialized.")
    html_to_send = page_html_content[:MAX_HTML_CONTENT_LENGTH]
    if len(page_html_content) > MAX_HTML_CONTENT_LENGTH: logger.warning(f"HTML for {url} truncated for extraction.")
    prompt = f"""Analyze RAW HTML from '{url}' (Title: "{page_title}") for a KB.
CRITICAL:
1. LANGUAGE: 'title_suggestion' & 'extracted_chunk' (all Markdown) MUST be in {target_language}. If HTML content is different, TRANSLATE accurately to {target_language}.
2. COMPLETENESS: Extract ALL meaningful text, data, lists, tables, descriptions, features, FAQs, contacts, policies. Prioritize user-visible content. Parse HTML thoroughly. Recreate table structures accurately. For policy pages (like Terms, Privacy, Returns), ensure that all sections, clauses, and specific conditions are extracted in their entirety or summarized with extreme care to not lose legal or procedural meaning.
3. RAW HTML INPUT: You get raw HTML. Extract core info. Ignore scripts/styles unless metadata is key.
Guidelines: Preserve detail. Structure with Markdown (in {target_language}). Identify main concept.
JSON output: {{"url": "{url}", "title_suggestion": "Concise title in {target_language}", "extracted_chunk": "Detailed Markdown in {target_language}"}}
RAW HTML (possibly truncated): ```html\n{html_to_send}\n```"""
    prompt_tokens = count_tokens(prompt)
    try:
        completion_obj = openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "system", "content": f"AI extracting knowledge from RAW HTML. Respond JSON. All generated text MUST be in {target_language}, translating if needed. Be comprehensive, accurate, structured. Pay special attention to full detail for policy pages."},
                      {"role": "user", "content": prompt}],
            max_tokens=MAX_RESPONSE_TOKENS_KB_EXTRACTION, temperature=0.1, response_format={"type": "json_object"})
        response_content = completion_obj.choices[0].message.content.strip()
        if completion_obj.usage:
            if completion_obj.usage.prompt_tokens: prompt_tokens = completion_obj.usage.prompt_tokens
            completion_tokens = completion_obj.usage.completion_tokens
        extracted_data = json.loads(response_content)
        if not isinstance(extracted_data, dict) or "extracted_chunk" not in extracted_data or "title_suggestion" not in extracted_data:
            raise ValueError("Invalid JSON structure from OpenAI for KB extraction.")
        extracted_data["url"] = url
        logger.info(f"Extracted KB chunk from {url}. Target: {target_language}. Tokens (P/C): {prompt_tokens}/{completion_tokens}")
        return extracted_data, prompt_tokens, completion_tokens
    except Exception as e:
        logger.error(f"Error during OpenAI KB extraction for {url} (target: {target_language}): {e}", exc_info=True)
        if "context_length_exceeded" in str(e).lower(): raise RuntimeError(f"OpenAI context length exceeded for {url}.") from e
        raise RuntimeError(f"KB extraction failed: {e}") from e

def compile_final_knowledge_base_with_openai(knowledge_chunks: list[dict], root_url: str, target_language: str) -> tuple[str, int, int]:
    prompt_tokens, completion_tokens = 0, 0
    if not openai_client: raise ConnectionError("OpenAI client not initialized.")
    if not knowledge_chunks: return "No knowledge chunks to compile.", prompt_tokens, completion_tokens
    current_kb_guidelines = KB_WRITING_GUIDELINES_TEMPLATE.format(target_language=target_language)
    combined_chunks_text = f"Knowledge chunks from {root_url} (ALREADY in target language: {target_language}):\n\n"
    for i, chunk_data in enumerate(knowledge_chunks):
        combined_chunks_text += f"--- Chunk {i+1} (URL: {chunk_data.get('url', 'N/A')}) ---\nTitle ({target_language}): {chunk_data.get('title_suggestion', 'N/A')}\nContent ({target_language}):\n{chunk_data.get('extracted_chunk', 'N/A')}\n--------------------\n\n"
    max_prompt_chars_compilation = 3000000
    if len(combined_chunks_text) > max_prompt_chars_compilation:
        logger.warning(f"Combined chunks for compilation ({len(combined_chunks_text)} chars) truncated to {max_prompt_chars_compilation}.")
        combined_chunks_text = combined_chunks_text[:max_prompt_chars_compilation] + "\n... [Chunks Truncated]"
    prompt = f"""Compile a comprehensive KB from 'knowledge chunks' from {root_url}. Chunks are pre-translated to {target_language}. Synthesize into a single Markdown document in {target_language}.
Guidelines:
{current_kb_guidelines}
Use "Suggested Title" for section headings. Organize logically. Preserve all distinct info. Create a coherent narrative with clear introduction and conclusion. Output single Markdown text. Start directly with content.
Combined Chunks (already in {target_language}):
{combined_chunks_text}
Final Compiled Knowledge Base in {target_language} (Markdown):"""
    prompt_tokens = count_tokens(prompt)
    try:
        completion_obj = openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "system", "content": f"Expert KB compiler. Output Markdown. Final KB MUST be in {target_language}. Chunks are pre-processed. Ensure comprehensive, flowing narrative with intro/conclusion. Pay special attention to detail from policy page chunks."},
                      {"role": "user", "content": prompt}],
            max_tokens=MAX_RESPONSE_TOKENS_KB_COMPILATION, temperature=0.3)
        final_kb = completion_obj.choices[0].message.content.strip()
        if completion_obj.usage:
            if completion_obj.usage.prompt_tokens: prompt_tokens = completion_obj.usage.prompt_tokens
            completion_tokens = completion_obj.usage.completion_tokens
        logger.info(f"Compiled final KB for {root_url}. Target: {target_language}. Tokens (P/C): {prompt_tokens}/{completion_tokens}")
        return final_kb, prompt_tokens, completion_tokens
    except Exception as e:
        logger.error(f"Error during OpenAI KB compilation for {root_url} (target: {target_language}): {e}", exc_info=True)
        if "context_length_exceeded" in str(e).lower():
            return f"Error: Could not compile KB in {target_language}. Combined content too large.", prompt_tokens, 0
        raise RuntimeError(f"KB compilation failed: {e}") from e

# --- Background Job Runner ---
def run_knowledge_base_job(job_id, base_url, max_pages_for_kb_processing, use_selenium):
    thread_name = threading.current_thread().name
    logger.info(f"[{thread_name}] Starting KB job {job_id} for {base_url} (Selenium: {use_selenium}, Max Process Pages: {max_pages_for_kb_processing}).")
    start_time = time.time()
    
    total_prompt_tokens, total_completion_tokens = 0, 0
    target_language = DEFAULT_TARGET_LANGUAGE
    all_discovered_pages_map = {} 
    selected_pages_for_processing_info = []
    extracted_knowledge_chunks = []
    final_knowledge_base = None
    job_failed = False; error_message = None

    try:
        with jobs_lock: jobs[job_id].update({"status": "detecting_language", "started_at": start_time, "progress": f"Fetching main page {base_url} for language detection..."})
        main_page_html_for_lang_detect = None; main_page_title_for_lang_detect = "N/A"
        try:
            main_page_html_for_lang_detect = fetch_url_html_content(base_url, for_lang_detect=True)
            if main_page_html_for_lang_detect:
                main_page_title_for_lang_detect = get_page_title_from_html(main_page_html_for_lang_detect)
                all_discovered_pages_map[base_url] = {'url': base_url, 'title': main_page_title_for_lang_detect, 'status': 'lang_detect_fetch', 'html_source': None} 
        except Exception as e: logger.error(f"Failed to fetch main page {base_url} for lang detection: {e}")

        if main_page_html_for_lang_detect:
            lang_detect_result, p_tokens, c_tokens = detect_language_from_html_with_openai(main_page_html_for_lang_detect, base_url)
            target_language = lang_detect_result; total_prompt_tokens += p_tokens; total_completion_tokens += c_tokens
        else: logger.warning(f"No HTML for main page {base_url} for lang detect. Defaulting to {DEFAULT_TARGET_LANGUAGE}.")
        with jobs_lock: jobs[job_id]["detected_target_language"] = target_language
        logger.info(f"Determined target language: {target_language}")

        with jobs_lock: jobs[job_id].update({"status": "discovering_pages", "progress": f"Discovering pages for {base_url}..."})
        sitemap_urls = find_sitemap_urls(base_url)
        for url_from_sitemap in sitemap_urls:
            if url_from_sitemap not in all_discovered_pages_map:
                all_discovered_pages_map[url_from_sitemap] = {'url': url_from_sitemap, 'title': "N/A", 'status': 'sitemap_found', 'html_source': None}
        probe_core_pages(base_url, target_language, all_discovered_pages_map)
        urls_needing_titles = [data['url'] for url, data in all_discovered_pages_map.items() if data.get('status') == 'sitemap_found' and data.get('title', "N/A") == "N/A"]
        if urls_needing_titles:
            titled_pages = fetch_titles_for_urls(urls_needing_titles, urlparse(base_url).netloc)
            for page_data in titled_pages:
                if page_data['url'] in all_discovered_pages_map:
                    all_discovered_pages_map[page_data['url']]['title'] = page_data['title']
                    all_discovered_pages_map[page_data['url']]['status'] = 'sitemap_title_fetched'
        
        if len(all_discovered_pages_map) < MIN_DISCOVERED_PAGES_BEFORE_FALLBACK_CRAWL:
            logger.info(f"Discovered {len(all_discovered_pages_map)} pages via sitemap/probe; starting fallback general crawl.")
            with jobs_lock: jobs[job_id]["progress"] = f"Fallback crawl for {base_url}..."
            remaining_discovery_allowance = MAX_PAGES_FOR_FALLBACK_DISCOVERY_CRAWL
            fallback_crawl_list = []
            if use_selenium: fallback_crawl_list = selenium_crawl_website(base_url, max_pages_to_discover=remaining_discovery_allowance)
            else: fallback_crawl_list = simple_crawl_website(base_url, max_pages_to_discover=remaining_discovery_allowance)
            for page_data in fallback_crawl_list:
                if page_data['url'] not in all_discovered_pages_map: all_discovered_pages_map[page_data['url']] = page_data
                elif page_data.get('html_source') and not all_discovered_pages_map[page_data['url']].get('html_source'):
                    all_discovered_pages_map[page_data['url']]['html_source'] = page_data['html_source']
                    if page_data.get('title',"N/A") != "N/A": all_discovered_pages_map[page_data['url']]['title'] = page_data['title']
        
        logger.info(f"Total unique pages discovered: {len(all_discovered_pages_map)}")
        with jobs_lock: jobs[job_id].update({"initial_found_pages_count": len(all_discovered_pages_map), "initial_found_page_details": [], "crawler_used": f"sitemap_probe_then_{'selenium_fallback' if use_selenium and len(all_discovered_pages_map) < MIN_DISCOVERED_PAGES_BEFORE_FALLBACK_CRAWL else ('simple_fallback' if len(all_discovered_pages_map) < MIN_DISCOVERED_PAGES_BEFORE_FALLBACK_CRAWL else 'sitemap_probe_only')}"})

        if not all_discovered_pages_map:
            final_knowledge_base = "Could not generate KB: Discovery phase found no pages."; job_failed = True; error_message = final_knowledge_base
        else:
            pages_to_offer_ai_selection = [{'url': p['url'], 'title': p.get('title', 'N/A')} for p in all_discovered_pages_map.values()]
            with jobs_lock: jobs[job_id].update({"status": "selecting_pages", "progress": f"AI selecting from {len(pages_to_offer_ai_selection)} discovered pages..."})
            ai_selection_limit = min(max_pages_for_kb_processing, MAX_PAGES_FOR_KB_GENERATION)
            selected_page_url_title_list, p_tokens, c_tokens = select_relevant_pages_for_kb_with_openai(pages_to_offer_ai_selection, base_url, ai_selection_limit, target_language)
            total_prompt_tokens += p_tokens; total_completion_tokens += c_tokens
            for sel_info in selected_page_url_title_list:
                page_full_data = all_discovered_pages_map.get(sel_info['url'])
                if page_full_data:
                    page_full_data['reason_selected'] = sel_info.get('reason', 'N/A')
                    selected_pages_for_processing_info.append(page_full_data)
            
            logger.info(f"AI selected {len(selected_pages_for_processing_info)} pages for KB processing.")
            selected_pages_log_summary = [{'url':p['url'], 'title':p['title'], 'reason':p.get('reason_selected')} for p in selected_pages_for_processing_info]
            with jobs_lock: jobs[job_id].update({"pages_selected_for_kb_count": len(selected_pages_for_processing_info), "pages_selected_for_kb_details": selected_pages_log_summary })

            if not selected_pages_for_processing_info:
                final_knowledge_base = f"Could not generate KB (in {target_language}): AI selected no relevant pages."; job_failed = True; error_message = final_knowledge_base
            else:
                with jobs_lock: jobs[job_id].update({"status": "extracting_knowledge", "knowledge_extraction_details": []})
                for i, page_data_to_extract in enumerate(selected_pages_for_processing_info):
                    page_url = page_data_to_extract['url']; page_title_for_log = page_data_to_extract['title']
                    with jobs_lock:
                        jobs[job_id]["progress"] = f"Extracting page {i+1}/{len(selected_pages_for_processing_info)} into {target_language}: {page_url[:50]}..."
                        current_list = jobs[job_id].get("knowledge_extraction_details", [])
                        current_list.append({"url": page_url, "title": page_title_for_log, "status": "pending_extraction"})
                        jobs[job_id]["knowledge_extraction_details"] = current_list
                    extraction_status_update = {"url": page_url, "title": page_title_for_log, "status": "failed_extraction", "error": "HTML content unavailable"}
                    page_html_content = page_data_to_extract.get('html_source') 
                    if not page_html_content: 
                        logger.info(f"HTML for {page_url} needs full fetch for extraction...")
                        try: 
                            if use_selenium and page_data_to_extract.get('status') != 'found_by_probe':
                                 sel_fetch_list = selenium_crawl_website(page_url, max_pages_to_discover=1)
                                 if sel_fetch_list: page_html_content = sel_fetch_list[0].get('html_source')
                            else: page_html_content = fetch_url_html_content(page_url)
                        except Exception as fetch_err: extraction_status_update["error"] = f"Live fetch failed: {fetch_err}"
                    if page_html_content:
                        try:
                            if not page_html_content.strip(): raise ValueError("HTML content empty.")
                            kb_chunk_data, p_tokens, c_tokens = extract_knowledge_from_page_with_openai(page_html_content, page_url, page_title_for_log, target_language)
                            total_prompt_tokens += p_tokens; total_completion_tokens += c_tokens
                            extraction_status_update.update({"status": "extracted", "title_suggestion": kb_chunk_data.get("title_suggestion"), "extracted_chunk_preview": kb_chunk_data.get("extracted_chunk", "")[:200] + "..."})
                            if "error" in extraction_status_update: del extraction_status_update["error"]
                            extracted_knowledge_chunks.append(kb_chunk_data)
                        except Exception as page_err: extraction_status_update["error"] = str(page_err)
                    with jobs_lock: 
                        details_list = jobs[job_id]["knowledge_extraction_details"]
                        updated = False
                        for idx, item in enumerate(details_list):
                            if item["url"] == page_url and item.get("status") == "pending_extraction":
                                details_list[idx] = extraction_status_update; updated = True; break
                        if not updated: details_list.append(extraction_status_update)
                        jobs[job_id]["intermediate_knowledge_chunks_count"] = len(extracted_knowledge_chunks)
                if not extracted_knowledge_chunks:
                    final_knowledge_base = f"Could not generate KB (in {target_language}): Failed to extract from any pages."; job_failed = True; error_message = final_knowledge_base
                else:
                    with jobs_lock: jobs[job_id].update({"status": "compiling_kb", "progress": f"AI compiling {len(extracted_knowledge_chunks)} chunks into {target_language}..."})
                    final_knowledge_base, p_tokens, c_tokens = compile_final_knowledge_base_with_openai(extracted_knowledge_chunks, base_url, target_language)
                    total_prompt_tokens += p_tokens; total_completion_tokens += c_tokens
                    with jobs_lock: jobs[job_id]["final_knowledge_base"] = final_knowledge_base
    except Exception as e: 
        logger.error(f"[{thread_name}][{job_id}] Job failed: {e}", exc_info=True)
        job_failed = True; error_message = str(e)
        with jobs_lock:
             if job_id in jobs:
                jobs[job_id].update({"status": "failed", "error": error_message})
                if not jobs[job_id].get("final_knowledge_base") and final_knowledge_base: jobs[job_id]["final_knowledge_base"] = final_knowledge_base
    end_time = time.time(); duration = end_time - start_time
    final_status = "failed" if job_failed else "completed"
    input_cost = (total_prompt_tokens / 1000000) * PRICE_PER_INPUT_TOKEN_MILLION
    output_cost = (total_completion_tokens / 1000000) * PRICE_PER_OUTPUT_TOKEN_MILLION
    estimated_cost = input_cost + output_cost
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update({"status": final_status, "finished_at": end_time, "duration_seconds": round(duration, 2),"progress": "Job finished.", "total_prompt_tokens": total_prompt_tokens, "total_completion_tokens": total_completion_tokens, "estimated_cost_usd": round(estimated_cost, 6)})
            if final_status == "failed" and not jobs[job_id].get("error"): jobs[job_id]["error"] = error_message or "Unknown error"
            if final_knowledge_base is not None and not jobs[job_id].get("final_knowledge_base"): jobs[job_id]["final_knowledge_base"] = final_knowledge_base
            if "detected_target_language" not in jobs[job_id] and target_language != DEFAULT_TARGET_LANGUAGE: jobs[job_id]["detected_target_language"] = target_language
    logger.info(f"[{thread_name}][{job_id}] Job {job_id} finished: {final_status} (Target Lang: {target_language}). Tokens (P/C): {total_prompt_tokens}/{total_completion_tokens}. Cost: ~${estimated_cost:.6f}. Duration: {duration:.2f}s.")

# --- API Endpoints & Main Execution ---
@app.route('/api/generate-knowledge-base', methods=['POST'])
@require_api_key
def start_knowledge_base_generation():
    if not request.is_json: return jsonify({"error": "Bad Request", "message": "Request body must be JSON"}), 400
    data = request.get_json(); url = data.get('url')
    if not url or not url.startswith(('http://', 'https://')): return jsonify({"error": "Bad Request", "message": "Valid 'url' is required"}), 400
    max_pages_input = int(data.get('max_pages', MAX_PAGES_FOR_KB_GENERATION)) 
    max_pages_to_process_for_kb = min(max_pages_input, MAX_PAGES_FOR_KB_GENERATION)
    use_selenium = bool(data.get('use_selenium', False))
    if use_selenium and not SELENIUM_AVAILABLE: return jsonify({"error": "Bad Request", "message": "Selenium not available."}), 400
    if not openai_client: return jsonify({"error": "Service Unavailable", "message": "OpenAI service not configured."}), 503
    job_id = str(uuid.uuid4())
    logger.info(f"Received KB job {job_id} for URL: {url}, max pages to process for KB: {max_pages_to_process_for_kb}, use_selenium: {use_selenium}, model: {OPENAI_MODEL}")
    job_details = {
        "id": job_id, "url": url, "requested_max_pages_to_process": max_pages_input, "effective_max_pages_for_kb": max_pages_to_process_for_kb, 
        "use_selenium": use_selenium, "status": "pending", "progress": "Job accepted, pending start.", "created_at": time.time(),
        "crawler_used": None, "initial_found_pages_count": 0, "initial_found_page_details": [],
        "detected_target_language": None, "pages_selected_for_kb_count": 0, "pages_selected_for_kb_details": [],
        "knowledge_extraction_details": [], "intermediate_knowledge_chunks_count": 0,
        "final_knowledge_base": None, "error": None, "started_at": None, "finished_at": None, "duration_seconds": None,
        "total_prompt_tokens": 0, "total_completion_tokens": 0, "estimated_cost_usd": 0.0
    }
    with jobs_lock: jobs[job_id] = job_details
    thread = threading.Thread(target=run_knowledge_base_job, args=(job_id, url, max_pages_to_process_for_kb, use_selenium), name=f"KBJob-{job_id[:6]}")
    thread.start()
    return jsonify({"message": "KB generation job started (language will be auto-detected).", "job_id": job_id, "status_url": f"/api/knowledge-base-jobs/{job_id}"}), 202

@app.route('/api/knowledge-base-jobs/<job_id>', methods=['GET'])
@require_api_key
def get_knowledge_base_job_status(job_id):
    with jobs_lock: job = jobs.get(job_id)
    if not job: return jsonify({"error": "Not Found", "message": "Job ID not found."}), 404
    job_copy = job.copy()
    if job_copy.get("final_knowledge_base") and len(job_copy["final_knowledge_base"]) > 1000:
        job_copy["final_knowledge_base_preview"] = job_copy["final_knowledge_base"][:1000] + "\n... (KB content truncated in status preview)"
        if job_copy["status"] != "completed": del job_copy["final_knowledge_base"]
    # initial_found_page_details is now an empty list in the job store, keeping status small
    return jsonify(job_copy), 200

@app.route('/api/knowledge-base-jobs', methods=['GET'])
@require_api_key
def list_knowledge_base_jobs():
    jobs_list = []
    with jobs_lock:
        for job_id, job_data in jobs.items():
            jobs_list.append({
                "job_id": job_id, "url": job_data.get("url"), "status": job_data.get("status"),
                "progress": job_data.get("progress"), "crawler_used": job_data.get("crawler_used"),
                "detected_target_language": job_data.get("detected_target_language"),
                "total_prompt_tokens": job_data.get("total_prompt_tokens"),
                "total_completion_tokens": job_data.get("total_completion_tokens"),
                "estimated_cost_usd": job_data.get("estimated_cost_usd"),
                "created_at": job_data.get("created_at"), "finished_at": job_data.get("finished_at"),
                "duration_seconds": job_data.get("duration_seconds"), "error": job_data.get("error"),
                "initial_found_pages_count": job_data.get("initial_found_pages_count"), # Keep the count
                "pages_selected_for_kb_count": job_data.get("pages_selected_for_kb_count"),
                "effective_max_pages_for_kb": job_data.get("effective_max_pages_for_kb") })
    jobs_list.sort(key=lambda x: x.get('created_at', 0), reverse=True)
    return jsonify({"total_jobs": len(jobs_list), "jobs": jobs_list})

@app.route('/api/health', methods=['GET'])
def health_check():
    health_status = {"status": "ok", "message": "Knowledge Base Generator API is running", "model_in_use": OPENAI_MODEL}
    status_code = 200
    if not EXPECTED_SERVICE_API_KEY: health_status.update({"service_api_key_status": "missing", "status": "error"}); status_code = 503
    else: health_status["service_api_key_status"] = "configured"
    if not openai_client: health_status.update({"openai_client_status": "not_initialized", "status": "error"}); status_code = 503
    else: health_status["openai_client_status"] = "initialized"
    health_status["selenium_support"] = "available" if SELENIUM_AVAILABLE else "not_available"
    if not SELENIUM_AVAILABLE: health_status["notes"] = health_status.get("notes", "") + " Selenium crawls will fail. "
    health_status["max_html_chars_per_page"] = MAX_HTML_CONTENT_LENGTH
    health_status["default_target_language"] = DEFAULT_TARGET_LANGUAGE
    health_status["tokenizer_available"] = TOKENIZER is not None
    health_status["max_discovery_crawl_pages_fallback"] = MAX_PAGES_FOR_FALLBACK_DISCOVERY_CRAWL
    health_status["min_pages_before_fallback_crawl"] = MIN_DISCOVERED_PAGES_BEFORE_FALLBACK_CRAWL
    return jsonify(health_status), status_code

@app.errorhandler(404)
def not_found(error): return jsonify({"error": "Not Found", "message": "Endpoint does not exist."}), 404
@app.errorhandler(405)
def method_not_allowed(error): return jsonify({"error": "Method Not Allowed"}), 405
@app.errorhandler(500)
def internal_server_error(error):
    logger.error(f"Internal Server Error: {error}", exc_info=True)
    return jsonify({"error": "Internal Server Error", "message": "An unexpected server error."}), 500

if __name__ == '__main__':
    if not EXPECTED_SERVICE_API_KEY or not openai_client:
        logger.error("FATAL: Service cannot start due to missing API key or OpenAI client. Check .env and logs.")
        exit(1)
    if TOKENIZER is None:
        logger.warning("Tokenizer could not be initialized. Token counting and cost estimation will NOT be accurate.")
    
    logger.info("Knowledge Base Generator API starting...")
    logger.info(f"Service API Key: Configured")
    logger.info(f"Using OpenAI Model: {OPENAI_MODEL}")
    logger.info(f"Pricing (Input/Output per 1M tokens): ${PRICE_PER_INPUT_TOKEN_MILLION:.2f} / ${PRICE_PER_OUTPUT_TOKEN_MILLION:.2f} (Output price is an assumption)")
    logger.info(f"Max HTML characters per page for analysis: {MAX_HTML_CONTENT_LENGTH}")
    logger.info(f"Max pages for fallback discovery crawl: {MAX_PAGES_FOR_FALLBACK_DISCOVERY_CRAWL}")
    logger.info(f"Max pages selected for KB processing per job: {MAX_PAGES_FOR_KB_GENERATION}")
    logger.info(f"Default target language if detection fails: {DEFAULT_TARGET_LANGUAGE}")
    logger.info(f"Selenium Support Available: {SELENIUM_AVAILABLE}")
    if not SELENIUM_AVAILABLE: logger.warning("Running without Selenium support.")
    app.run(host='0.0.0.0', port=5000, debug=False)