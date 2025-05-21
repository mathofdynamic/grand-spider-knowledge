# Grand Spider Knowledge: AI-Powered Knowledge Base Generator

## Overview

Grand Spider Knowledge is a Flask-based Python application designed to automatically generate structured knowledge bases from websites. It crawls specified URLs, uses AI (powered by OpenAI's GPT models) to select relevant pages, extracts information from these pages, and compiles it into a coherent Markdown document in a detected or specified target language.

The service is ideal for creating initial drafts of company wikis, support documentation, or summaries of website content. It features both simple HTTP-based crawling and more advanced JavaScript-aware crawling using Selenium.

## Features

-   **Automated Web Crawling**: Crawls websites to discover pages using either a simple requests-based crawler or a Selenium-based crawler for dynamic content.
-   **AI-Powered Page Selection**: Leverages OpenAI's language models to intelligently select the most relevant pages for knowledge base creation, focusing on informational content like "About Us", "Contact", "FAQ", policies, etc.
-   **AI-Driven Content Extraction**: Extracts meaningful information from the HTML of selected pages, translating content into a target language if necessary.
-   **AI-Compiled Knowledge Base**: Synthesizes extracted information from multiple pages into a single, well-structured Markdown document.
-   **Language Auto-Detection**: Attempts to auto-detect the primary language of the source website to guide the translation and generation process.
-   **Job-Based Processing**: Handles knowledge base generation asynchronously as background jobs.
-   **Status Tracking**: Provides API endpoints to track the status and progress of generation jobs.
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

1.  **Clone the repository (if applicable) or download the `grand_spider_knowledge.py` file.**

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
    python grand_spider_knowledge.py
    ```

2.  The API will typically be available at `http://localhost:5000`. The application logs will indicate the host and port.

    ```
    INFO:__main__:Knowledge Base Generator API starting...
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
-   **Description**: Starts a new job to generate a knowledge base for the given URL.
-   **Request Body (JSON)**:
    ```json
    {
        "url": "https://example.com",
        "max_pages": 10, // Optional, default: 15, max: 15
        "use_selenium": false // Optional, default: false. Set to true for JS-heavy sites.
    }
    ```
    -   `url` (string, required): The base URL of the website to crawl.
    -   `max_pages` (integer, optional): Maximum number of pages the crawler will attempt to find and AI will consider. The service caps this internally (currently at 15).
    -   `use_selenium` (boolean, optional): If `true`, uses Selenium for crawling (requires Selenium and ChromeDriver to be installed and working). Defaults to `false` (uses simple HTTP requests).
-   **Success Response (202 Accepted)**:
    ```json
    {
        "message": "KB generation job started (language will be auto-detected).",
        "job_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
        "status_url": "/api/knowledge-base-jobs/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
    }
    ```
-   **Error Responses**:
    -   `400 Bad Request`: Invalid JSON, missing URL, or Selenium requested but not available.
    -   `401 Unauthorized`: Missing or invalid `api-key` header.
    -   `503 Service Unavailable`: OpenAI service not configured.

---

### 2. Get Job Status

-   **Endpoint**: `GET /api/knowledge-base-jobs/<job_id>`
-   **Description**: Retrieves the status and details of a specific knowledge base generation job.
-   **URL Parameters**:
    -   `job_id` (string, required): The ID of the job.
-   **Success Response (200 OK)**:
    Returns a JSON object detailing the job's current status, progress, any errors, token usage, estimated costs, and (if completed) the generated knowledge base or a preview.
    Example (Completed Job):
    ```json
    {
        "id": "job_id_example",
        "url": "https://example.com",
        "requested_max_pages": 10,
        "effective_max_pages_for_kb": 10,
        "use_selenium": false,
        "status": "completed", // Other statuses: pending, crawling, selecting_pages, extracting_knowledge, compiling_kb, failed
        "progress": "Job finished.",
        "created_at": 1678886400.0,
        "crawler_used": "simple",
        "initial_found_pages_count": 5,
        "initial_found_page_details": [
            {"url": "https://example.com/", "title": "Homepage"},
            // ... other pages without html_source
        ],
        "detected_target_language": "English",
        "pages_selected_for_kb_count": 3,
        "pages_selected_for_kb_details": [
            {"url": "https://example.com/about", "title": "About Us", "reason": "Provides company background."},
            // ... other selected pages
        ],
        "knowledge_extraction_details": [
            // details of extraction for each selected page
        ],
        "intermediate_knowledge_chunks_count": 3,
        "final_knowledge_base": "# Knowledge Base for example.com\n\n## Introduction\n...", // Full KB if completed and not too long, else preview.
        "final_knowledge_base_preview": "# Knowledge Base for example.com...", // Preview if KB is large
        "error": null,
        "started_at": 1678886401.0,
        "finished_at": 1678886520.0,
        "duration_seconds": 119.0,
        "total_prompt_tokens": 15000,
        "total_completion_tokens": 8000,
        "estimated_cost_usd": 0.0156
    }
    ```
-   **Error Responses**:
    -   `401 Unauthorized`: Missing or invalid `api-key` header.
    -   `404 Not Found`: Job ID does not exist.

---

### 3. List All Jobs

-   **Endpoint**: `GET /api/knowledge-base-jobs`
-   **Description**: Retrieves a summary list of all submitted knowledge base generation jobs, sorted by creation time (newest first).
-   **Success Response (200 OK)**:
    ```json
    {
        "total_jobs": 2,
        "jobs": [
            {
                "job_id": "job_id_1",
                "url": "https://site1.com",
                "status": "completed",
                "progress": "Job finished.",
                "crawler_used": "selenium",
                "detected_target_language": "English",
                "total_prompt_tokens": 25000,
                "total_completion_tokens": 12000,
                "estimated_cost_usd": 0.0244,
                "created_at": 1678886800.0,
                "finished_at": 1678887000.0,
                "duration_seconds": 200.0,
                "error": null,
                "effective_max_pages": 15
            },
            {
                "job_id": "job_id_2",
                "url": "https://site2.org",
                "status": "failed",
                // ... other summary fields
            }
        ]
    }
    ```
-   **Error Responses**:
    -   `401 Unauthorized`: Missing or invalid `api-key` header.

---

### 4. Health Check

-   **Endpoint**: `GET /api/health`
-   **Description**: Provides a health check of the API, including status of dependencies like API keys and OpenAI client.
-   **Authentication**: Not required for this endpoint.
-   **Success Response (200 OK or 503 Service Unavailable if critical components are missing)**:
    ```json
    {
        "status": "ok", // "error" if critical components are missing
        "message": "Knowledge Base Generator API is running",
        "model_in_use": "gpt-4.1-nano-2025-04-14",
        "service_api_key_status": "configured", // "missing" if not set
        "openai_client_status": "initialized", // "not_initialized" if key missing or error
        "selenium_support": "available", // "not_available"
        "max_html_chars_per_page": 3500000,
        "default_target_language": "English",
        "tokenizer_available": true // false if tiktoken fails to load
    }
    ```

## Project Structure Insights

(Derived from `grand_spider_knowledge.py`)

-   **Configuration & Initialization**: Loads environment variables, sets up Flask app, logging, and initializes the OpenAI client.
-   **Constants**: Defines various limits (e.g., `MAX_HTML_CONTENT_LENGTH`, `MAX_PAGES_FOR_KB_GENERATION`), timeouts, OpenAI model names, and the default target language.
-   **Tokenizer**: Uses `tiktoken` to count tokens for OpenAI API calls, aiding in cost estimation and managing context limits.
-   **Job Management**: A thread-safe dictionary (`jobs`) stores the state and results of background knowledge base generation tasks.
-   **Authentication**: A decorator (`require_api_key`) protects sensitive endpoints.
-   **Crawling Logic**:
    -   `simple_crawl_website`: Uses `requests` and `BeautifulSoup` for basic HTML crawling.
    -   `selenium_crawl_website`: Uses Selenium for crawling sites that heavily rely on JavaScript to render content. Includes logic for page load timeouts, scrolling, and waiting for dynamic content.
-   **OpenAI Helper Functions**:
    -   `detect_language_from_html_with_openai`: Detects the primary language of HTML content.
    -   `select_relevant_pages_for_kb_with_openai`: Prompts the AI to choose the most important pages from a list of crawled URLs based on their titles and relevance criteria.
    -   `extract_knowledge_from_page_with_openai`: Prompts the AI to extract and structure knowledge from the raw HTML of a single page, translating to the target language.
    -   `compile_final_knowledge_base_with_openai`: Prompts the AI to synthesize all extracted knowledge chunks into a final, coherent Markdown document.
-   **Background Job Runner**:
    -   `run_knowledge_base_job`: The core function executed in a separate thread for each generation request. It orchestrates the crawling, language detection, page selection, extraction, and compilation steps. It meticulously updates the job status and logs token usage.
-   **API Endpoints**: Flask routes for starting jobs, checking status, listing jobs, and health checks.
-   **Error Handling**: Includes Flask error handlers for common HTTP errors (404, 405, 500) and specific error messages within the API responses.
-   **Logging**: Comprehensive logging throughout the application lifecycle, including detailed logs for each job's progress and any errors encountered.

## Important Considerations

-   **OpenAI API Costs**: Generating knowledge bases involves multiple calls to the OpenAI API, which incurs costs based on token usage. The application provides an estimated cost per job.
-   **Crawling Ethics**: Ensure you have permission to crawl websites and adhere to their `robots.txt` policies (this script does not currently implement `robots.txt` checking automatically). The `CRAWLER_USER_AGENT` is set to `GrandSpiderKnowledgeBuilder/1.5`.
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

This README provides a comprehensive guide to understanding, setting up, and using the Grand Spider Knowledge application. 