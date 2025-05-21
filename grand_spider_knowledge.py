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
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup
import tiktoken # Import tiktoken

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
MAX_HTML_SNIPPET_FOR_LANG_DETECT = 15000
OPENAI_MODEL = "gpt-4.1-nano-2025-04-14"
DEFAULT_TARGET_LANGUAGE = "English"

MAX_RESPONSE_TOKENS_LANG_DETECT = 50
MAX_RESPONSE_TOKENS_PAGE_SELECTION = 1500 # Increased slightly
MAX_RESPONSE_TOKENS_KB_EXTRACTION = 4090
MAX_RESPONSE_TOKENS_KB_COMPILATION = 4090

CRAWLER_USER_AGENT = 'GrandSpiderKnowledgeBuilder/1.5 (+http://yourappdomain.com/bot)'
MAX_PAGES_FOR_KB_GENERATION = 15

# --- Tokenizer and Pricing ---
try:
    try:
        TOKENIZER = tiktoken.encoding_for_model(OPENAI_MODEL)
    except KeyError:
        logger.warning(f"Tokenizer for model '{OPENAI_MODEL}' not found. Falling back to 'cl100k_base'. Token counts will be approximate.")
        TOKENIZER = tiktoken.get_encoding("cl100k_base")
except Exception as e:
    logger.error(f"Could not initialize tiktoken tokenizer: {e}. Token counting will be disabled.")
    TOKENIZER = None

# PRICING FOR OPENAI_MODEL ("gpt-4.1-nano-2025-04-14")
PRICE_PER_INPUT_TOKEN_MILLION = 0.40
PRICE_PER_OUTPUT_TOKEN_MILLION = 1.20 # ASSUMED - PLEASE UPDATE IF INCORRECT

KB_WRITING_GUIDELINES_TEMPLATE = """
Here are guidelines for structuring the final knowledge base in {target_language}:
1.  Introduction & Conclusion: Start with a brief overview of the company/website and end with a summary.
2.  Logical Flow: Organize extracted information thematically. Synthesize related points from different pages into coherent sections.
3.  Single Idea per Chunk (within sections): Break down complex topics from source pages into digestible paragraphs or sub-sections.
4.  Clear, Consistent Headings: Use Markdown H1 for major topics (often derived from page titles or key concepts) and H2/H3 for sub-topics, features, or FAQs (all in {target_language}).
5.  Write Like You Talk (in {target_language}): Use clear, concise, active sentences. Explain industry-critical jargon if not self-evident from source.
6.  Use Lists & Tables (in {target_language}): Faithfully represent procedural steps as lists. Recreate tables for comparisons or structured data.
7.  Include Examples & Edge Cases: If the source content provides examples or specific scenarios, include them (translated to {target_language} if needed).
8.  Embed Q/A: If source content has FAQs, represent this clearly (e.g., '**Q:**' and '**A:**' in {target_language}).
9.  LANGUAGE: The final knowledge base MUST be entirely in {target_language}. If source chunks are in different languages, translate them to {target_language} during synthesis.
10. COMPLETENESS & ACCURACY: Reflect ALL relevant information from the provided source chunks. Ensure extracted details (names, numbers, features) are accurate.
11. Format: Present the entire knowledge base as a single, well-formatted Markdown document in {target_language}.
"""

# --- Job Management (Thread-Safe) ---
jobs = {}
jobs_lock = threading.Lock()

# --- Authentication Decorator ---
def require_api_key(f):
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

# --- Helper: Count Tokens ---
def count_tokens(text: str) -> int:
    if TOKENIZER and text is not None:
        try:
            return len(TOKENIZER.encode(text))
        except Exception as e:
            logger.error(f"Error tokenizing text: {e}. Text (first 50 chars): '{text[:50]}'")
            return 0 # Or handle more gracefully
    return 0

# --- Crawler Logic ---
def simple_crawl_website(base_url, max_pages=10):
    logger.info(f"[Simple] Starting crawl for {base_url}, max_pages={max_pages}")
    urls_to_visit = {base_url}; visited_urls = set(); found_pages_details = []
    base_domain = urlparse(base_url).netloc
    headers = {'User-Agent': CRAWLER_USER_AGENT}
    while urls_to_visit and len(found_pages_details) < max_pages:
        current_url = urls_to_visit.pop()
        if current_url in visited_urls: continue
        if urlparse(current_url).netloc != base_domain: continue
        visited_urls.add(current_url)
        logger.debug(f"[Simple] Visiting: {current_url}")
        page_html_content = None
        try:
            response = requests.get(current_url, headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            response.raise_for_status()
            content_type = response.headers.get('Content-Type', '').lower()
            if response.status_code == 200 and 'text/html' in content_type:
                page_title = "N/A"
                try:
                    response.encoding = response.apparent_encoding
                    page_html_content = response.text
                    soup_title = BeautifulSoup(page_html_content, 'html.parser')
                    title_tag = soup_title.find('title')
                    if title_tag and title_tag.string: page_title = title_tag.string.strip()
                except Exception as e_parse: logger.warning(f"[Simple] Error parsing title/content for {current_url}: {e_parse}")
                
                store_html = page_html_content if current_url == base_url else None
                found_pages_details.append({'url': current_url, 'title': page_title, 'status': 'found', 'html_source': store_html})
                logger.info(f"[Simple] Found page ({len(found_pages_details)}/{max_pages}): {current_url} (Title: {page_title})")
                
                if page_html_content:
                    soup_links = BeautifulSoup(page_html_content, 'html.parser')
                    for link in soup_links.find_all('a', href=True):
                        href = link['href']
                        absolute_url = urljoin(base_url, href)
                        absolute_url = urlparse(absolute_url)._replace(fragment="").geturl()
                        if urlparse(absolute_url).netloc == base_domain and \
                           absolute_url not in visited_urls and absolute_url not in urls_to_visit:
                             urls_to_visit.add(absolute_url)
            else: logger.warning(f"[Simple] Skipping non-HTML/non-200: {current_url} (Status: {response.status_code}, Type: {content_type})")
        except requests.exceptions.Timeout: logger.error(f"[Simple] Timeout crawling URL: {current_url}")
        except requests.exceptions.RequestException as e: logger.error(f"[Simple] Error crawling URL {current_url}: {e}")
        except Exception as e: logger.error(f"[Simple] Unexpected error for {current_url}: {e}", exc_info=True)
    logger.info(f"[Simple] Crawl finished for {base_url}. Found {len(found_pages_details)} pages.")
    return found_pages_details

def selenium_crawl_website(base_url, max_pages=10):
    if not SELENIUM_AVAILABLE: raise RuntimeError("Selenium not available.")
    logger.info(f"[Selenium] Starting crawl for {base_url}, max_pages={max_pages}")
    urls_to_visit = {base_url}; visited_urls = set(); found_pages_details = []
    base_domain = urlparse(base_url).netloc
    driver = None
    try:
        chrome_options = ChromeOptions()
        chrome_options.add_argument("--headless"); chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage"); chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument(f"user-agent={CRAWLER_USER_AGENT}")
        chrome_options.add_experimental_option('excludeSwitches', ['enable-logging'])
        service = ChromeService(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=chrome_options)
        driver.set_page_load_timeout(SELENIUM_PAGE_LOAD_TIMEOUT)

        while urls_to_visit and len(found_pages_details) < max_pages:
            current_url = urls_to_visit.pop()
            if current_url in visited_urls: continue
            if urlparse(current_url).netloc != base_domain: continue
            visited_urls.add(current_url)
            logger.debug(f"[Selenium] Visiting: {current_url}")
            try:
                driver.get(current_url)
                WebDriverWait(driver, SELENIUM_PAGE_LOAD_TIMEOUT).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
                if SELENIUM_RENDER_WAIT_SECONDS > 0:
                    logger.debug(f"[Selenium] Waiting {SELENIUM_RENDER_WAIT_SECONDS}s for SPA content on {current_url}...")
                    time.sleep(SELENIUM_RENDER_WAIT_SECONDS)
                try:
                    logger.debug(f"[Selenium] Scrolling page down for {current_url}")
                    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                    time.sleep(1)
                    driver.execute_script("window.scrollTo(0, document.body.scrollHeight/2);")
                    time.sleep(0.5)
                    driver.execute_script("window.scrollTo(0, 0);")
                    time.sleep(0.5)
                except Exception as scroll_err: logger.warning(f"[Selenium] Could not scroll page {current_url}: {scroll_err}")

                page_title = driver.title.strip() if driver.title else "N/A"
                page_html_source = driver.page_source

                found_pages_details.append({'url': current_url, 'title': page_title, 'status': 'found', 'html_source': page_html_source})
                logger.info(f"[Selenium] Found page ({len(found_pages_details)}/{max_pages}): {current_url} (Title: {page_title}, HTML length: {len(page_html_source)})")

                links = driver.find_elements(By.TAG_NAME, 'a')
                for link in links:
                    href = link.get_attribute('href')
                    if href:
                        absolute_url = urljoin(base_url, href)
                        absolute_url = urlparse(absolute_url)._replace(fragment="").geturl()
                        if urlparse(absolute_url).netloc == base_domain and \
                           absolute_url not in visited_urls and absolute_url not in urls_to_visit:
                            urls_to_visit.add(absolute_url)
            except TimeoutException: logger.error(f"[Selenium] Timeout loading URL: {current_url}")
            except WebDriverException as e: logger.error(f"[Selenium] WebDriver error for {current_url}: {e}")
            except Exception as e: logger.error(f"[Selenium] Unexpected error for {current_url}: {e}", exc_info=True)
    finally:
        if driver:
            try: driver.quit()
            except Exception: pass
    logger.info(f"[Selenium] Crawl finished. Found {len(found_pages_details)} pages.")
    return found_pages_details

def fetch_url_html_content(url: str) -> str:
    headers = {'User-Agent': CRAWLER_USER_AGENT}
    try:
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        response.raise_for_status()
        content_type = response.headers.get('Content-Type', '').lower()
        if 'text/html' not in content_type:
            logger.warning(f"URL {url} is non-HTML: {content_type}. Fetching content anyway.")
        response.encoding = response.apparent_encoding
        return response.text[:MAX_HTML_CONTENT_LENGTH]
    except requests.exceptions.Timeout:
        logger.error(f"Timeout fetching content for KB: {url}")
        raise TimeoutError(f"Request timed out after {REQUEST_TIMEOUT} seconds.")
    except requests.exceptions.RequestException as req_err:
        logger.error(f"Error fetching content for KB {url}: {req_err}")
        raise ConnectionError(f"Failed to fetch URL content: {req_err}")
    except Exception as e:
         logger.error(f"Unexpected error fetching content for {url}: {e}", exc_info=True)
         raise ConnectionError(f"An unexpected error occurred while fetching content for the URL.")

# --- OpenAI Helper Functions with Token Counting ---
def detect_language_from_html_with_openai(html_snippet: str, url: str) -> tuple[str, int, int]:
    """Returns (detected_language, prompt_tokens, completion_tokens)"""
    prompt_tokens, completion_tokens = 0, 0
    if not openai_client: raise ConnectionError("OpenAI client not initialized.")
    if not html_snippet.strip():
        logger.warning(f"HTML snippet for language detection from {url} is empty. Defaulting language.")
        return DEFAULT_TARGET_LANGUAGE, prompt_tokens, completion_tokens

    soup = BeautifulSoup(html_snippet, 'html.parser')
    for script_or_style in soup(["script", "style"]):
        script_or_style.decompose()
    text_snippet = soup.get_text(separator=' ', strip=True)
    text_snippet = ' '.join(text_snippet.split())
    if not text_snippet:
        logger.warning(f"Text snippet for language detection (after cleaning) from {url} is empty. Defaulting.")
        return DEFAULT_TARGET_LANGUAGE, prompt_tokens, completion_tokens
    text_snippet_for_prompt = text_snippet[:MAX_HTML_SNIPPET_FOR_LANG_DETECT // 2]

    prompt = f"""Analyze the following text snippet from {url}. Identify the primary human language used in this text. Respond with the full name of the language in English (e.g., "English", "French", "Spanish", "Russian", "Farsi", "German"). If the language is unclear or a mix of too many, respond with "Undetermined". Text Snippet: \"\"\"{text_snippet_for_prompt}\"\"\" Primary language:"""
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
            if completion_obj.usage.prompt_tokens: prompt_tokens = completion_obj.usage.prompt_tokens # Prefer OpenAI's count
            completion_tokens = completion_obj.usage.completion_tokens
        
        if not detected_language or detected_language == "Undetermined":
            logger.warning(f"Language detection for {url} was undetermined. Defaulting to {DEFAULT_TARGET_LANGUAGE}.")
            return DEFAULT_TARGET_LANGUAGE, prompt_tokens, completion_tokens
        logger.info(f"Detected language for {url} as: {detected_language}. Tokens (P/C): {prompt_tokens}/{completion_tokens}")
        return detected_language, prompt_tokens, completion_tokens
    except Exception as e:
        logger.error(f"Error during language detection for {url}: {e}", exc_info=True)
        logger.warning(f"Defaulting language to {DEFAULT_TARGET_LANGUAGE} due to error.")
        return DEFAULT_TARGET_LANGUAGE, prompt_tokens, 0

def select_relevant_pages_for_kb_with_openai(page_details: list[dict], root_url: str, max_pages_to_select: int, target_language: str) -> tuple[list[dict], int, int]:
    """Returns (selected_pages_list, prompt_tokens, completion_tokens)"""
    prompt_tokens, completion_tokens = 0, 0
    if not openai_client: raise ConnectionError("OpenAI client not initialized.")
    if not page_details: return [], prompt_tokens, completion_tokens
    
    pages_list_str = "\n".join([f"- URL: {p['url']} (Title: {p.get('title', 'N/A')})" for p in page_details])

    prompt = f"""
    You are an AI assistant tasked with selecting the MOST CRUCIAL pages from the website {root_url}
    to build a foundational knowledge base about the COMPANY/ORGANIZATION, its core operations, policies, and how to interact with it.
    The goal is NOT to detail every single product or blog post, but to capture essential, overarching information.

    From the list of available pages (URL and Title) below, select up to {max_pages_to_select} pages.

    ABSOLUTELY PRIORITIZE pages like:
    - "About Us" / "درباره ما" / Company Information / Our Mission / History
    - "Contact Us" / "تماس با ما" / Customer Support / Help Center / Support Channels
    - "Services" / "خدمات" (if these describe broad service categories or how services work, NOT individual product/service listings)
    - "Shipping Policy" / "قوانین ارسال" / Delivery Information / Shipping Details
    - "Return Policy" / "قوانین بازگشت کالا" / Refund Policy / Exchange Policy
    - "Terms and Conditions" / "قوانین و مقررات" / Terms of Use / Legal Information
    - "Privacy Policy" / "سیاست حفظ حریم خصوصی"
    - "FAQ" / "سوالات متداول" / Frequently Asked Questions
    - "How to Order" / "راهنمای خرید" / Payment Methods / Ordering Process / User Guides

    STRONGLY DE-PRIORITIZE and AIM TO EXCLUDE or SEVERELY LIMIT (e.g., select at most 1-2 representative examples ONLY IF no other priority pages exist AND these examples are crucial for understanding a core offering):
    - Individual Product Detail Pages (pages focused on one specific item out of many, often identifiable by URL patterns like '/product/...').
    - Individual Blog Posts or Articles (unless the title or a brief description VERY CLEARLY indicates it's a core policy, a comprehensive official guide to a main service, or the "About Us" story in article form).
    - Product Category Listing Pages that primarily just list many products or blogs without substantial unique informational text themselves (unless they are the *only* way to understand the main product/service *categories* offered).
    - Generic functional pages like "Login", "Register", "Shopping Cart", "Wishlist", "User Account Dashboard" unless they contain significant policy text or detailed help information beyond their basic function.

    Your selection should focus on pages that provide lasting, structural information about the business and its operations, which would be useful for a customer support chatbot or a general company overview.

    List of available pages (URL and Title):
    {pages_list_str}

    Respond with a JSON array of objects. Each object must have a "url" (string, required) and optionally a "reason" (string, explaining *why this page is crucial for a foundational KB according to the strict criteria above*).
    The "reason" text MUST be in {target_language}.
    Select NO MORE THAN {max_pages_to_select} pages. If fewer than {max_pages_to_select} highly relevant pages are found according to these strict criteria, select only those that strictly meet the criteria.
    Return an empty array [] if no pages meet these criteria.
    """
    prompt_tokens = count_tokens(prompt)
    try:
        completion_obj = openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": f"You are an AI page selector for foundational company/organizational knowledge. Focus strictly on policies, about, contact, core operational guides, and main service overviews. Strictly limit/exclude individual products & most blogs. Respond JSON. 'reason' MUST be in {target_language}."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=MAX_RESPONSE_TOKENS_PAGE_SELECTION, 
            temperature=0.1,
            response_format={"type": "json_object"}
        )
        response_content = completion_obj.choices[0].message.content.strip()
        if completion_obj.usage: 
            if completion_obj.usage.prompt_tokens: prompt_tokens = completion_obj.usage.prompt_tokens
            completion_tokens = completion_obj.usage.completion_tokens
        
        selected_pages_data = json.loads(response_content)
        
        selected_pages = []
        if isinstance(selected_pages_data, dict):
            processed_list = None
            # Check for common list keys first
            for key in ["selected_pages", "pages", "relevant_pages", "urls"]:
                if key in selected_pages_data and isinstance(selected_pages_data[key], list):
                    processed_list = selected_pages_data[key]
                    break
            # If no common key found, check if the first value in the dict is a list
            if processed_list is None and selected_pages_data:
                first_value = next(iter(selected_pages_data.values()), None)
                if isinstance(first_value, list):
                    processed_list = first_value
            selected_pages = processed_list if processed_list is not None else []
        elif isinstance(selected_pages_data, list): 
            selected_pages = selected_pages_data
        else: 
            logger.error(f"OpenAI page selection returned unexpected format: {type(selected_pages_data)}. Response: {response_content}")
        
        valid_selected_pages = []
        for item in selected_pages:
            if isinstance(item, dict) and "url" in item:
                original_page_detail = next((p for p in page_details if p['url'] == item['url']), None)
                title = original_page_detail['title'] if original_page_detail else "N/A" 
                valid_selected_pages.append({"url": item["url"], "title": title, "reason": item.get("reason", "N/A")})
            else:
                logger.warning(f"OpenAI page selection returned an invalid item in list: {item}")
        
        logger.info(f"OpenAI selected {len(valid_selected_pages)} pages for KB. Target: {target_language}. Tokens (P/C): {prompt_tokens}/{completion_tokens}")
        return valid_selected_pages[:max_pages_to_select], prompt_tokens, completion_tokens
    except json.JSONDecodeError as e:
        logger.error(f"OpenAI page selection JSON parsing error: {e}. Response: {response_content}", exc_info=True)
        return [], prompt_tokens, completion_tokens 
    except (APIError, APITimeoutError, RateLimitError, APIConnectionError) as e:
        logger.error(f"OpenAI API error (page selection) for {root_url} (target: {target_language}): {e}")
        raise ConnectionError(f"OpenAI API error during page selection: {e}") from e
    except Exception as e:
        logger.error(f"Unexpected error (OpenAI page selection) for {root_url} (target: {target_language}): {e}", exc_info=True)
        raise RuntimeError(f"Unexpected error during AI page selection: {e}") from e

def extract_knowledge_from_page_with_openai(page_html_content: str, url: str, page_title: str, target_language: str) -> tuple[dict, int, int]:
    """Returns (extracted_data_dict, prompt_tokens, completion_tokens)"""
    prompt_tokens, completion_tokens = 0, 0
    if not openai_client: raise ConnectionError("OpenAI client not initialized.")
    
    html_to_send = page_html_content[:MAX_HTML_CONTENT_LENGTH]
    if len(page_html_content) > MAX_HTML_CONTENT_LENGTH:
        logger.warning(f"HTML for {url} (len {len(page_html_content)}) truncated to {MAX_HTML_CONTENT_LENGTH} for extraction.")

    prompt = f"""Analyze RAW HTML from '{url}' (Title: "{page_title}") for a KB.
CRITICAL:
1. LANGUAGE: 'title_suggestion' & 'extracted_chunk' (all Markdown) MUST be in {target_language}. If HTML content is different, TRANSLATE accurately to {target_language}.
2. COMPLETENESS: Extract ALL meaningful text, data, lists, tables, descriptions, features, FAQs, contacts, policies. Prioritize user-visible content. Parse HTML thoroughly. Recreate table structures accurately.
3. RAW HTML INPUT: You get raw HTML. Extract core info. Ignore scripts/styles unless metadata is key.
Guidelines: Preserve detail. Structure with Markdown (in {target_language}). Identify main concept.
JSON output: {{"url": "{url}", "title_suggestion": "Concise title in {target_language}", "extracted_chunk": "Detailed Markdown in {target_language}"}}
RAW HTML (possibly truncated): ```html\n{html_to_send}\n```"""
    prompt_tokens = count_tokens(prompt)
    try:
        completion_obj = openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": f"You are an AI that extracts detailed knowledge from RAW HTML. Respond JSON. All generated text MUST be in {target_language}, translating if needed. Be comprehensive, accurate, and structured."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=MAX_RESPONSE_TOKENS_KB_EXTRACTION, temperature=0.1, response_format={"type": "json_object"}
        )
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
    except json.JSONDecodeError as e:
        logger.error(f"OpenAI KB extraction JSON parsing error for {url}: {e}. Response: {response_content}", exc_info=True)
        raise RuntimeError(f"OpenAI KB extraction failed: invalid JSON. {e}") from e
    except (APIError, APITimeoutError, RateLimitError, APIConnectionError) as e:
        logger.error(f"OpenAI API error (KB extraction) for {url} (target: {target_language}): {e}")
        if "context_length_exceeded" in str(e).lower():
            raise RuntimeError(f"OpenAI context length exceeded for {url}.") from e
        raise ConnectionError(f"OpenAI API error: {e}") from e
    except Exception as e:
        logger.error(f"Unexpected error (OpenAI KB extraction) for {url} (target: {target_language}): {e}", exc_info=True)
        raise RuntimeError(f"Unexpected error during AI knowledge extraction: {e}") from e

def compile_final_knowledge_base_with_openai(knowledge_chunks: list[dict], root_url: str, target_language: str) -> tuple[str, int, int]:
    """Returns (final_kb_string, prompt_tokens, completion_tokens)"""
    prompt_tokens, completion_tokens = 0, 0
    if not openai_client: raise ConnectionError("OpenAI client not initialized.")
    if not knowledge_chunks: return "No knowledge chunks to compile.", prompt_tokens, completion_tokens

    current_kb_guidelines = KB_WRITING_GUIDELINES_TEMPLATE.format(target_language=target_language)
    combined_chunks_text = f"Knowledge chunks from {root_url} (ALREADY in target language: {target_language}):\n\n"
    for i, chunk_data in enumerate(knowledge_chunks):
        combined_chunks_text += f"--- Chunk {i+1} (Source URL: {chunk_data.get('url', 'N/A')}) ---\nTitle ({target_language}): {chunk_data.get('title_suggestion', 'N/A')}\nContent ({target_language}):\n{chunk_data.get('extracted_chunk', 'N/A')}\n--------------------\n\n"
    
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
            messages=[
                {"role": "system", "content": f"Expert KB compiler. Output Markdown. Final KB MUST be in {target_language}. Chunks are pre-processed into {target_language}. Ensure comprehensive, flowing narrative with intro/conclusion."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=MAX_RESPONSE_TOKENS_KB_COMPILATION, temperature=0.3,
        )
        final_kb = completion_obj.choices[0].message.content.strip()
        if completion_obj.usage:
            if completion_obj.usage.prompt_tokens: prompt_tokens = completion_obj.usage.prompt_tokens
            completion_tokens = completion_obj.usage.completion_tokens
        
        logger.info(f"Compiled final KB for {root_url}. Target: {target_language}. Tokens (P/C): {prompt_tokens}/{completion_tokens}")
        return final_kb, prompt_tokens, completion_tokens
    except (APIError, APITimeoutError, RateLimitError, APIConnectionError) as e:
        logger.error(f"OpenAI API error (KB compilation) for {root_url} (target: {target_language}): {e}")
        if "context_length_exceeded" in str(e).lower():
            error_msg = f"Error: Could not compile KB in {target_language}. Combined content too large for AI model."
            return error_msg, prompt_tokens, 0
        raise ConnectionError(f"OpenAI API error: {e}") from e
    except Exception as e:
        logger.error(f"Unexpected error (OpenAI KB compilation) for {root_url} (target: {target_language}): {e}", exc_info=True)
        raise RuntimeError(f"Unexpected error during final AI KB compilation: {e}") from e

# --- Background Job Runner ---
def run_knowledge_base_job(job_id, url, max_pages_to_process, use_selenium):
    thread_name = threading.current_thread().name
    logger.info(f"[{thread_name}] Starting KB job {job_id} for {url} (Selenium: {use_selenium}, Max Pages: {max_pages_to_process}). Model: {OPENAI_MODEL}.")
    start_time = time.time()
    
    total_prompt_tokens = 0; total_completion_tokens = 0
    target_language = DEFAULT_TARGET_LANGUAGE
    initial_found_pages_details_from_crawl = [] 
    selected_pages_for_kb = []
    extracted_knowledge_chunks = []
    final_knowledge_base = None
    job_failed = False
    error_message = None

    try:
        logger.info(f"[{thread_name}][{job_id}] Step 1: Crawling {url}...")
        with jobs_lock: jobs[job_id].update({"status": "crawling", "started_at": start_time, "crawler_used": "selenium" if use_selenium else "simple", "progress": "Initializing crawl..."})
        if use_selenium: initial_found_pages_details_from_crawl = selenium_crawl_website(url, max_pages_to_process)
        else: initial_found_pages_details_from_crawl = simple_crawl_website(url, max_pages_to_process)
        logger.info(f"[{thread_name}][{job_id}] Crawl complete. Found {len(initial_found_pages_details_from_crawl)} pages.")
        initial_page_summaries_for_job_log = [{'url': p['url'], 'title': p['title']} for p in initial_found_pages_details_from_crawl]
        with jobs_lock: jobs[job_id].update({"initial_found_pages_count": len(initial_found_pages_details_from_crawl), "initial_found_page_details": initial_page_summaries_for_job_log})

        if not initial_found_pages_details_from_crawl:
            final_knowledge_base = "Could not generate KB: Initial crawl found no pages."; job_failed = True; error_message = final_knowledge_base
        else:
            main_page_data = next((p for p in initial_found_pages_details_from_crawl if p['url'] == url), None)
            if main_page_data and main_page_data.get('html_source'):
                logger.info(f"[{thread_name}][{job_id}] Step 1.5: Detecting language from main page {url}...")
                with jobs_lock: jobs[job_id]["progress"] = f"Detecting language from {url}..."
                html_snippet = main_page_data['html_source'][:MAX_HTML_SNIPPET_FOR_LANG_DETECT]
                lang_detect_result, p_tokens, c_tokens = detect_language_from_html_with_openai(html_snippet, url)
                target_language = lang_detect_result
                total_prompt_tokens += p_tokens; total_completion_tokens += c_tokens
            else: logger.warning(f"[{thread_name}][{job_id}] No HTML for main page {url} for lang detect. Defaulting to {DEFAULT_TARGET_LANGUAGE}.")
            with jobs_lock: jobs[job_id]["detected_target_language"] = target_language
            
            pages_for_selection_prompt = [{'url': p['url'], 'title': p['title']} for p in initial_found_pages_details_from_crawl]
            logger.info(f"[{thread_name}][{job_id}] Step 2: AI selecting relevant pages (target lang: {target_language})...")
            with jobs_lock: jobs[job_id].update({"status": "selecting_pages", "progress": f"AI selecting from {len(pages_for_selection_prompt)} pages..."})
            selected_pages_for_kb, p_tokens, c_tokens = select_relevant_pages_for_kb_with_openai(pages_for_selection_prompt, url, max_pages_to_process, target_language)
            total_prompt_tokens += p_tokens; total_completion_tokens += c_tokens
            logger.info(f"[{thread_name}][{job_id}] AI selected {len(selected_pages_for_kb)} pages for KB.")
            with jobs_lock: jobs[job_id].update({"pages_selected_for_kb_count": len(selected_pages_for_kb), "pages_selected_for_kb_details": selected_pages_for_kb})

            if not selected_pages_for_kb:
                final_knowledge_base = f"Could not generate KB (in {target_language}): AI selected no relevant pages."; job_failed = True; error_message = final_knowledge_base
            else:
                logger.info(f"[{thread_name}][{job_id}] Step 3: Extracting knowledge from {len(selected_pages_for_kb)} pages (target lang: {target_language})...")
                with jobs_lock: jobs[job_id].update({"status": "extracting_knowledge", "knowledge_extraction_details": []})
                for i, sel_page_info in enumerate(selected_pages_for_kb):
                    page_url_to_extract = sel_page_info['url']
                    original_crawled_page = next((p for p in initial_found_pages_details_from_crawl if p['url'] == page_url_to_extract), None)
                    page_title_for_log = original_crawled_page.get('title', 'N/A') if original_crawled_page else sel_page_info.get('title', 'N/A')
                    with jobs_lock:
                        jobs[job_id]["progress"] = f"Extracting page {i+1}/{len(selected_pages_for_kb)} into {target_language}: {page_url_to_extract[:50]}..."
                        current_list = jobs[job_id].get("knowledge_extraction_details", [])
                        current_list.append({"url": page_url_to_extract, "title": page_title_for_log, "status": "pending_extraction"})
                        jobs[job_id]["knowledge_extraction_details"] = current_list
                    
                    extraction_status_update = {"url": page_url_to_extract, "title": page_title_for_log, "status": "failed_extraction", "error": "Page data not found"}
                    page_html_content_for_extraction = None
                    if original_crawled_page and original_crawled_page.get('html_source'):
                        page_html_content_for_extraction = original_crawled_page['html_source']
                    elif original_crawled_page: 
                        try: page_html_content_for_extraction = fetch_url_html_content(page_url_to_extract)
                        except Exception as fetch_err: extraction_status_update["error"] = f"Fetch fail: {fetch_err}"
                    
                    if page_html_content_for_extraction:
                        try:
                            if not page_html_content_for_extraction.strip(): raise ValueError("HTML content empty.")
                            kb_chunk_data, p_tokens, c_tokens = extract_knowledge_from_page_with_openai(page_html_content_for_extraction, page_url_to_extract, page_title_for_log, target_language)
                            total_prompt_tokens += p_tokens; total_completion_tokens += c_tokens
                            extraction_status_update.update({"status": "extracted", "title_suggestion": kb_chunk_data.get("title_suggestion"), "extracted_chunk_preview": kb_chunk_data.get("extracted_chunk", "")[:200] + "..."})
                            if "error" in extraction_status_update: del extraction_status_update["error"]
                            extracted_knowledge_chunks.append(kb_chunk_data)
                        except Exception as page_err: extraction_status_update["error"] = str(page_err)
                    with jobs_lock: 
                        details_list = jobs[job_id]["knowledge_extraction_details"]
                        updated = False
                        for idx, item in enumerate(details_list):
                            if item["url"] == page_url_to_extract and item["status"] == "pending_extraction":
                                details_list[idx] = extraction_status_update; updated = True; break
                        if not updated: details_list.append(extraction_status_update) # Should not be hit if pending added correctly
                        jobs[job_id]["intermediate_knowledge_chunks_count"] = len(extracted_knowledge_chunks)
                
                if not extracted_knowledge_chunks:
                    final_knowledge_base = f"Could not generate KB (in {target_language}): Failed to extract from any pages."; job_failed = True; error_message = final_knowledge_base
                else:
                    logger.info(f"[{thread_name}][{job_id}] Step 4: Compiling final KB from {len(extracted_knowledge_chunks)} chunks (target lang: {target_language})...")
                    with jobs_lock: jobs[job_id].update({"status": "compiling_kb", "progress": f"AI compiling {len(extracted_knowledge_chunks)} chunks into {target_language}..."})
                    final_knowledge_base, p_tokens, c_tokens = compile_final_knowledge_base_with_openai(extracted_knowledge_chunks, url, target_language)
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
            jobs[job_id].update({
                "status": final_status, "finished_at": end_time, "duration_seconds": round(duration, 2),
                "progress": "Job finished.", "total_prompt_tokens": total_prompt_tokens,
                "total_completion_tokens": total_completion_tokens, "estimated_cost_usd": round(estimated_cost, 6)
            })
            if final_status == "failed" and not jobs[job_id].get("error"): jobs[job_id]["error"] = error_message or "Unknown error"
            if final_knowledge_base is not None and not jobs[job_id].get("final_knowledge_base"): jobs[job_id]["final_knowledge_base"] = final_knowledge_base
            if "detected_target_language" not in jobs[job_id] and target_language != DEFAULT_TARGET_LANGUAGE: jobs[job_id]["detected_target_language"] = target_language
    logger.info(f"[{thread_name}][{job_id}] Job {job_id} finished: {final_status} (Target Lang: {target_language}). Tokens (P/C): {total_prompt_tokens}/{total_completion_tokens}. Cost: ~${estimated_cost:.6f}. Duration: {duration:.2f}s.")

# --- API Endpoints ---
@app.route('/api/generate-knowledge-base', methods=['POST'])
@require_api_key
def start_knowledge_base_generation():
    if not request.is_json: return jsonify({"error": "Bad Request", "message": "Request body must be JSON"}), 400
    data = request.get_json(); url = data.get('url')
    if not url or not url.startswith(('http://', 'https://')): return jsonify({"error": "Bad Request", "message": "Valid 'url' is required"}), 400
    max_pages_input = int(data.get('max_pages', MAX_PAGES_FOR_KB_GENERATION))
    max_pages_to_process = min(max_pages_input, MAX_PAGES_FOR_KB_GENERATION)
    use_selenium = bool(data.get('use_selenium', False))
    if use_selenium and not SELENIUM_AVAILABLE: return jsonify({"error": "Bad Request", "message": "Selenium not available."}), 400
    if not openai_client: return jsonify({"error": "Service Unavailable", "message": "OpenAI service not configured."}), 503
    job_id = str(uuid.uuid4())
    logger.info(f"Received KB job {job_id} for URL: {url}, max_pages: {max_pages_to_process}, use_selenium: {use_selenium}, model: {OPENAI_MODEL}")
    job_details = { 
        "id": job_id, "url": url, "requested_max_pages": max_pages_input, "effective_max_pages_for_kb": max_pages_to_process,
        "use_selenium": use_selenium, "status": "pending", "progress": "Job accepted, pending start.", "created_at": time.time(),
        "crawler_used": None, "initial_found_pages_count": 0, "initial_found_page_details": [], 
        "detected_target_language": None, "pages_selected_for_kb_count": 0, "pages_selected_for_kb_details": [], 
        "knowledge_extraction_details": [], "intermediate_knowledge_chunks_count": 0,
        "final_knowledge_base": None, "error": None, "started_at": None, "finished_at": None, "duration_seconds": None,
        "total_prompt_tokens": 0, "total_completion_tokens": 0, "estimated_cost_usd": 0.0
    }
    with jobs_lock: jobs[job_id] = job_details
    thread = threading.Thread(target=run_knowledge_base_job, args=(job_id, url, max_pages_to_process, use_selenium), name=f"KBJob-{job_id[:6]}")
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
        if job_copy["status"] != "completed": 
            del job_copy["final_knowledge_base"]
    if "initial_found_page_details" in job_copy: # Remove bulky html_source from status
        job_copy["initial_found_page_details"] = [
            {k: v for k, v in detail.items() if k != 'html_source'} 
            for detail in job_copy.get("initial_found_page_details",[])
        ]
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
                "effective_max_pages": job_data.get("effective_max_pages_for_kb") })
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
    return jsonify(health_status), status_code

@app.errorhandler(404)
def not_found(error): return jsonify({"error": "Not Found", "message": "Endpoint does not exist."}), 404
@app.errorhandler(405)
def method_not_allowed(error): return jsonify({"error": "Method Not Allowed"}), 405
@app.errorhandler(500)
def internal_server_error(error):
    logger.error(f"Internal Server Error: {error}", exc_info=True)
    return jsonify({"error": "Internal Server Error", "message": "An unexpected server error."}), 500

# --- Main Execution ---
if __name__ == '__main__':
    if not EXPECTED_SERVICE_API_KEY or not openai_client:
        logger.error("FATAL: Service cannot start due to missing API key or OpenAI client. Check .env and logs.")
        exit(1)
    if TOKENIZER is None:
        logger.warning("Tokenizer could not be initialized. Token counting and cost estimation will NOT be accurate.")
    
    logger.info("Knowledge Base Generator API starting...")
    logger.info(f"Service API Key: Configured")
    logger.info(f"Using OpenAI Model: {OPENAI_MODEL}")
    logger.info(f"Pricing (Input/Output per 1M tokens): ${PRICE_PER_INPUT_TOKEN_MILLION:.2f} / ${PRICE_PER_OUTPUT_TOKEN_MILLION:.2f} (Output price is an assumption if not specified for your model)")
    logger.info(f"Max HTML characters per page for analysis: {MAX_HTML_CONTENT_LENGTH}")
    logger.info(f"Max pages for KB processing per job: {MAX_PAGES_FOR_KB_GENERATION}")
    logger.info(f"Default target language if detection fails: {DEFAULT_TARGET_LANGUAGE}")
    logger.info(f"Selenium Support Available: {SELENIUM_AVAILABLE}")
    if not SELENIUM_AVAILABLE: logger.warning("Running without Selenium support.")
    app.run(host='0.0.0.0', port=5000, debug=False)