# Grand Spider: Multi-Purpose AI-Powered Web Analyzer

## Overview

Grand Spider is a Flask-based Python application designed to perform multiple AI-powered analysis tasks on websites. It includes:

- **Comprehensive Knowledge Base Generation**: Automatically generates detailed, structured knowledge bases from websites by crawling URLs, using AI to categorize pages, extracting comprehensive information, and compiling it into coherent Markdown documents with product catalogs and detailed analysis.
- **Company Analysis**: Analyzes company websites to extract business information, contact details, and company profiles.
- **Prospect Qualification**: Qualifies potential prospects based on user profiles and personas.
- **HTML Analysis**: Analyzes HTML content to extract structured information.

The service is ideal for creating comprehensive company wikis, support documentation, product catalogs, company research, and prospect analysis. It features both simple HTTP-based crawling and more advanced JavaScript-aware crawling using Selenium. The system is optimized for Iranian websites with automatic Toman currency formatting.

## Features

-   **Multi-Purpose Analysis**: Supports comprehensive knowledge base generation, company analysis, prospect qualification, and HTML analysis.
-   **Automated Web Crawling**: Crawls websites to discover pages using either a simple requests-based crawler or a Selenium-based crawler for dynamic content.
-   **AI-Powered URL Categorization**: Leverages OpenAI's language models to intelligently categorize all discovered URLs into company info, product, service, technical, and other pages.
-   **Comprehensive Content Extraction**: Extracts detailed information from categorized pages, translating content into a target language if necessary.
-   **Product Catalog Generation**: Automatically generates comprehensive product catalogs with brands, categories, price ranges, and detailed product listings.
-   **AI-Compiled Knowledge Base**: Synthesizes extracted information, product catalogs, and URL analysis into a single, comprehensive Markdown document.
-   **Iranian Market Optimization**: Automatically formats all prices in Toman (IRT) currency, optimized for Iranian websites and markets.
-   **Company Intelligence**: Extracts business information, contact details, and company profiles from websites.
-   **Prospect Qualification**: Analyzes potential prospects based on user profiles and personas.
-   **HTML Structure Analysis**: Extracts structured information from HTML content.
-   **Language Auto-Detection**: Attempts to auto-detect the primary language of the source website to guide the translation and generation process.
-   **Job-Based Processing**: Handles all analysis tasks asynchronously as background jobs.
-   **Status Tracking**: Provides API endpoints to track the status and progress of all jobs.
-   **Cost Estimation**: Estimates the OpenAI API usage costs for each job based on token counts.
-   **Configurable**: Allows configuration via environment variables for API keys, model names, and crawling parameters.
-   **Health Check Endpoint**: Provides a health check endpoint to monitor the service status.
-   **Secure API**: Uses API key authentication for its endpoints.

## Tech Stack

-   **Python 3.x**
-   **Flask**: Web framework for API endpoints.
-   **OpenAI API**: For language understanding, content extraction, and compilation.
-   **Requests**: For simple HTTP-based web crawling.
-   **Selenium (Optional)**: For JavaScript-aware web crawling of dynamic websites.
    -   `webdriver-manager`: For managing ChromeDriver.
-   **BeautifulSoup4**: For HTML parsing.
-   **Tiktoken**: For counting tokens used by OpenAI models (essential for cost estimation).
-   **python-dotenv**: For managing environment variables.

## Prerequisites

-   Python 3.7+
-   An OpenAI API Key.
-   Access to a terminal or command prompt.
-   `pip` for installing Python packages.
-   (Optional) Google Chrome installed if using Selenium for crawling.

## Installation

1.  **Clone the repository (if applicable) or download the `grand_spider.py` file.**

2.  **Create a virtual environment (recommended):**
    ```bash
    python -m venv venv
    source venv/bin/activate  # On Windows: venv\Scripts\activate
    ```

3.  **Install dependencies:**
    Create a `requirements.txt` file with the following content:

    ```txt
    Flask
    openai
    python-dotenv
    requests
    BeautifulSoup4
    tiktoken
    # For Selenium (optional, uncomment if needed)
    # selenium
    # webdriver-manager
    ```
    Then run:
    ```bash
    pip install -r requirements.txt
    ```
    If you plan to use the Selenium-based crawler, make sure to uncomment `selenium` and `webdriver-manager` in `requirements.txt` before running `pip install`.

## Configuration

The application requires environment variables for its configuration. Create a `.env` file in the root directory of the project with the following content:

```env
# Your secret API key for this service (clients will use this to authenticate)
SERVICE_API_KEY="YOUR_STRONG_SECRET_API_KEY_HERE"

# Your OpenAI API Key
OPENAI_API_KEY="sk-YOUR_OPENAI_API_KEY_HERE"

# Optional: Override the default OpenAI model (default is gpt-4.1-nano-2025-04-14)
# OPENAI_MODEL_NAME="gpt-4-turbo"
```

Replace placeholders with your actual API keys.

**Key Environment Variables:**

*   `SERVICE_API_KEY`: A secret key you define. Clients sending requests to this service must include this key in the `api-key` header.
*   `OPENAI_API_KEY`: Your API key from OpenAI.

## How to Run

Once dependencies are installed and the `.env` file is configured:

1.  **Start the Flask application:**
    ```bash
    python grand_spider.py
    ```

2.  The API will typically be available at `http://localhost:5000`. The application logs will indicate the host and port.

    ```
    INFO:__main__:Multi-Purpose Analyzer API starting...
    INFO:__main__:Service API Key: Configured
    INFO:__main__:Using OpenAI Model: gpt-4.1-nano-2025-04-14
    ...
    INFO:werkzeug:Press CTRL+C to quit
    ```

## API Endpoints

All API endpoints require an `api-key` header for authentication, which should match the `SERVICE_API_KEY` defined in your `.env` file.

---

### 1. Generate Knowledge Base

-   **Endpoint**: `POST /api/generate-knowledge-base`
-   **Description**: Starts a new job to generate a comprehensive knowledge base for the given URL, including product catalogs and detailed analysis.
-   **Request Body (JSON)**:
    ```json
    {
        "url": "https://example.com",
        "max_pages": 20, // Optional, default: 20, max: 20
        "use_selenium": false // Optional, default: false. Set to true for JS-heavy sites.
    }
    ```
    -   `url` (string, required): The base URL of the website to crawl.
    -   `max_pages` (integer, optional): Maximum number of pages the crawler will attempt to find and AI will consider. The service caps this internally (currently at 20).
    -   `use_selenium` (boolean, optional): If `true`, uses Selenium for crawling (requires Selenium and ChromeDriver to be installed and working). Defaults to `false` (uses simple HTTP requests).
-   **Success Response (202 Accepted)**:
    ```json
    {
        "message": "Knowledge base generation job started.",
        "job_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
    }
    ```
-   **Error Responses**:
    -   `400 Bad Request`: Invalid JSON, missing URL, or Selenium requested but not available.
    -   `401 Unauthorized`: Missing or invalid `api-key` header.
    -   `503 Service Unavailable`: OpenAI service not configured.

---

### 2. Analyze Company

-   **Endpoint**: `POST /api/analyze-company`
-   **Description**: Starts a new job to analyze a company website and extract business information.
-   **Request Body (JSON)**:
    ```json
    {
        "url": "https://example.com"
    }
    ```
    -   `url` (string, required): The base URL of the company website to analyze.
-   **Success Response (202 Accepted)**:
    ```json
    {
        "message": "Company analysis job started.",
        "job_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
    }
    ```
-   **Error Responses**:
    -   `400 Bad Request`: Invalid JSON or missing URL.
    -   `401 Unauthorized`: Missing or invalid `api-key` header.

---

### 3. Qualify Prospects

-   **Endpoint**: `POST /api/qualify-prospects`
-   **Description**: Starts a new job to qualify prospects based on user profiles and personas.
-   **Request Body (JSON)**:
    ```json
    {
        "user_profile": "Description of the user profile",
        "user_personas": ["Persona 1", "Persona 2"],
        "prospect_urls": ["https://prospect1.com", "https://prospect2.com"]
    }
    ```
    -   `user_profile` (string, required): Description of the user profile.
    -   `user_personas` (array, required): Array of user personas.
    -   `prospect_urls` (array, required): Array of prospect URLs to analyze.
-   **Success Response (202 Accepted)**:
    ```json
    {
        "message": "Prospect qualification job started.",
        "job_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
    }
    ```
-   **Error Responses**:
    -   `400 Bad Request`: Invalid JSON or missing required fields.
    -   `401 Unauthorized`: Missing or invalid `api-key` header.

---

### 4. Analyze HTML

-   **Endpoint**: `POST /api/analyze-html`
-   **Description**: Analyzes HTML content to extract structured information.
-   **Request Body (JSON)**:
    ```json
    {
        "html_content": "<html>...</html>"
    }
    ```
    -   `html_content` (string, required): The HTML content to analyze.
-   **Success Response (200 OK)**:
    ```json
    {
        "status": "success",
        "elements": { /* structured HTML elements */ }
    }
    ```
-   **Error Responses**:
    -   `400 Bad Request`: Invalid JSON or missing html_content.
    -   `401 Unauthorized`: Missing or invalid `api-key` header.

---

### 5. Analyze HTML File

-   **Endpoint**: `POST /api/analyze-html-file`
-   **Description**: Analyzes an uploaded HTML file to extract structured information.
-   **Request Body (multipart/form-data)**:
    -   `html_file` (file, required): The HTML file to analyze.
-   **Success Response (200 OK)**:
    ```json
    {
        "status": "success",
        "filename": "example.html",
        "elements": { /* structured HTML elements */ }
    }
    ```
-   **Error Responses**:
    -   `400 Bad Request`: No file uploaded.
    -   `401 Unauthorized`: Missing or invalid `api-key` header.

---

### 6. Get Job Status

-   **Endpoint**: `GET /api/jobs/<job_id>`
-   **Description**: Retrieves the status and details of a specific job (knowledge base generation, company analysis, prospect qualification, etc.).
-   **URL Parameters**:
    -   `job_id` (string, required): The ID of the job.
-   **Success Response (200 OK)**:
    Returns a JSON object detailing the job's current status, progress, any errors, token usage, estimated costs, and (if completed) the results or a preview.
    Example (Completed Knowledge Base Job):
    ```json
    {
        "id": "76cf5080-cd84-4c65-a333-466c60efe3d2",
        "job_type": "knowledge_base_generation",
        "url": "https://afraa.shop/",
        "requested_max_pages": 20,
        "effective_max_pages_for_kb": 20,
        "use_selenium": false,
        "status": "completed",
        "progress": "Compiling comprehensive knowledge base...",
        "created_at": 1755873559.543447,
        "crawler_used": "simple",
        "initial_found_pages_count": 11910,
        "comprehensive_analysis": {
            "company_info_pages": 0,
            "product_pages": 0,
            "service_pages": 0,
            "total_pages_analyzed": 11910,
            "total_products_identified": 18
        },
        "product_catalog": {
            "brands": ["آسوس", "ایسوس", "اینتل", "اچ‌پی", "برند اپل", "لنوو", "رایزر", "مایکروسافت", "دوجی", "سامسونگ"],
            "price_ranges": [
                "7,500,000 - 15,000,000 تومان",
                "15,000,000 - 30,000,000 تومان", 
                "30,000,000 - 50,000,000 تومان",
                "50,000,000 - 100,000,000 تومان",
                "بیشتر از 100,000,000 تومان"
            ],
            "product_categories": ["لپ‌تاپ", "تبلت", "کامپیوتر نوت‌بوک", "لوازم جانبی", "ساعت هوشمند", "هدفون و هدست"],
            "total_products_estimated": 250,
            "products": [
                {
                    "name": "TUF Gaming A15 FA507XV ZA 2023",
                    "category": "لپ‌تاپ گیمینگ",
                    "brand": "آسوس",
                    "estimated_price": "50,000,000 - 100,000,000 تومان",
                    "url_pattern": "https://afraa.shop/product/afp-13964-tuf-gaming-a15-fa507xv-za-2023/"
                },
                {
                    "name": "Galaxy S24 Ultra 12GB 256GB 2024",
                    "category": "گوشی هوشمند پرچمدار",
                    "brand": "سامسونگ", 
                    "estimated_price": "70,000,000 - 110,000,000 تومان",
                    "url_pattern": "https://afraa.shop/product/afp-84146-84147-84148-84149-84150-84151-84152-galaxy-s24-8gb-256gb-2024/"
                }
            ]
        },
        "final_knowledge_base": "# بانک اطلاعات جامع وب‌سایت افراء (afraa.shop)\n\n## 1. معرفی شرکت\n\nوب‌سایت **افراء** یک فروشگاه تخصصی در حوزه فناوری...",
        "final_knowledge_base_preview": "# بانک اطلاعات جامع وب‌سایت افراء (afraa.shop)\n\n---\n\n## 1. معرفی شرکت\n\nوب‌سایت **افراء** یک فروشگاه تخصصی در حوزه فناوری و لوازم کامپیوتری است که محصولات متنوعی در زمینه‌های مختلف فناوری اطلاعات، گجت‌های هوشمند، لوازم جانبی و تجهیزات گیمینگ ارائه می‌دهد...",
        "error": null,
        "started_at": 1755873559.543447,
        "finished_at": 1755874000.0,
        "duration_seconds": 441.0,
        "total_prompt_tokens": 33564,
        "total_completion_tokens": 9277,
        "estimated_cost_usd": "0.024558"
    }
    ```
-   **Error Responses**:
    -   `401 Unauthorized`: Missing or invalid `api-key` header.
    -   `404 Not Found`: Job ID does not exist.

---

### 7. List All Jobs

-   **Endpoint**: `GET /api/jobs`
-   **Description**: Retrieves a summary list of all submitted jobs (knowledge base generation, company analysis, prospect qualification, etc.), sorted by creation time (newest first).
-   **Success Response (200 OK)**:
    ```json
    {
        "jobs": [
            {
                "job_id": "job_id_1",
                "job_type": "knowledge_base_generation",
                "status": "completed",
                "created_at": 1678886800.0,
                "finished_at": 1678887000.0
            },
            {
                "job_id": "job_id_2",
                "job_type": "company_analysis",
                "status": "failed",
                "created_at": 1678886900.0,
                "finished_at": null
            }
        ]
    }
    ```
-   **Error Responses**:
    -   `401 Unauthorized`: Missing or invalid `api-key` header.

---

### 8. Health Check

-   **Endpoint**: `GET /api/health`
-   **Description**: Provides a health check of the API, including status of dependencies like API keys and OpenAI client.
-   **Authentication**: Not required for this endpoint.
-   **Success Response (200 OK)**:
    ```json
    {
        "status": "ok",
        "message": "API is running",
        "selenium_available": true
    }
    ```

## Project Structure Insights

(Derived from `grand_spider.py`)

-   **Configuration & Initialization**: Loads environment variables, sets up Flask app, logging, and initializes the OpenAI client.
-   **Constants**: Defines various limits (e.g., `MAX_HTML_CONTENT_LENGTH`, `MAX_PAGES_FOR_KB_GENERATION`), timeouts, OpenAI model names, and the default target language.
-   **Tokenizer**: Uses `tiktoken` to count tokens for OpenAI API calls, aiding in cost estimation and managing context limits.
-   **Job Management**: A thread-safe dictionary (`jobs`) stores the state and results of background tasks (knowledge base generation, company analysis, prospect qualification, etc.).
-   **Authentication**: A decorator (`require_api_key`) protects sensitive endpoints.
-   **Crawling Logic**:
    -   `simple_crawl_website`: Uses `requests` and `BeautifulSoup` for basic HTML crawling.
    -   `selenium_crawl_website`: Uses Selenium for crawling sites that heavily rely on JavaScript to render content. Includes logic for page load timeouts, scrolling, and waiting for dynamic content.
-   **Comprehensive Analysis Functions**:
    -   `analyze_all_urls_comprehensively`: Categorizes all discovered URLs into company info, product, service, technical, and other pages using AI analysis.
    -   `generate_comprehensive_product_catalog`: Creates detailed product catalogs with brands, categories, price ranges, and individual product listings.
    -   `compile_comprehensive_knowledge_base`: Synthesizes all gathered information into a comprehensive Markdown knowledge base.
-   **OpenAI Helper Functions**:
    -   `detect_language_from_html_with_openai`: Detects the primary language of HTML content.
    -   `extract_knowledge_from_page_with_openai`: Prompts the AI to extract and structure knowledge from the raw HTML of a single page, translating to the target language with Toman currency formatting.
    -   `compile_comprehensive_knowledge_base`: Prompts the AI to synthesize all extracted information, product catalogs, and URL analysis into a comprehensive Markdown document.
-   **Background Job Runners**:
    -   `run_knowledge_base_job`: The core function executed in a separate thread for each knowledge base generation request. It orchestrates the crawling, language detection, URL categorization, content extraction, product catalog generation, and compilation steps. It meticulously updates the job status and logs token usage.
    -   `run_company_analysis_job`: Executes company analysis tasks in a separate thread.
    -   `run_prospect_qualification_job`: Executes prospect qualification tasks in a separate thread.
-   **API Endpoints**: Flask routes for starting various analysis jobs, checking status, listing jobs, and health checks.
-   **Error Handling**: Includes Flask error handlers for common HTTP errors (404, 405, 500) and specific error messages within the API responses.
-   **Logging**: Comprehensive logging throughout the application lifecycle, including detailed logs for each job's progress and any errors encountered.
-   **Iranian Market Optimization**: All pricing functions automatically format prices in Toman (IRT) currency, with conversion from USD when necessary.

## Important Considerations

-   **OpenAI API Costs**: Generating knowledge bases involves multiple calls to the OpenAI API, which incurs costs based on token usage. The application provides an estimated cost per job.
-   **Crawling Ethics**: Ensure you have permission to crawl websites and adhere to their `robots.txt` policies (this script does not currently implement `robots.txt` checking automatically). The `CRAWLER_USER_AGENT` is set to `GrandSpiderMultiPurposeAnalyzer/2.0`.
-   **Content Quality**: The quality of the generated knowledge base depends heavily on the website's structure, the clarity of its content, and the capabilities of the underlying AI model.
-   **Rate Limits**: Be mindful of OpenAI API rate limits and potential timeouts for long-running crawl or extraction processes.
-   **HTML Complexity**: Extremely complex or poorly structured HTML might pose challenges for the extraction process. The `MAX_HTML_CONTENT_LENGTH` constant limits the amount of HTML processed per page.

## Potential Future Improvements

-   Implement `robots.txt` respect.
-   Allow user to specify target language instead of only auto-detect.
-   More sophisticated handling of very large websites (e.g., deeper crawling strategies, incremental KB building).
-   Support for other output formats besides Markdown.
-   Improved error recovery and retry mechanisms for individual page processing steps.
-   Web UI for easier job submission and monitoring.

---

This README provides a comprehensive guide to understanding, setting up, and using the Grand Spider Multi-Purpose Analyzer application. 