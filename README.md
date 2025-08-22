# Grand Spider: Multi-Purpose AI-Powered Web Analyzer

## Overview

Grand Spider is a Flask-based Python application designed to perform multiple AI-powered analysis tasks on websites. It includes:

- **Knowledge Base Generation**: Automatically generates structured knowledge bases from websites by crawling URLs, using AI to select relevant pages, extracting information, and compiling it into coherent Markdown documents.
- **Company Analysis**: Analyzes company websites to extract business information, contact details, and company profiles.
- **Prospect Qualification**: Qualifies potential prospects based on user profiles and personas.
- **HTML Analysis**: Analyzes HTML content to extract structured information.

The service is ideal for creating initial drafts of company wikis, support documentation, company research, and prospect analysis. It features both simple HTTP-based crawling and more advanced JavaScript-aware crawling using Selenium.

## Features

-   **Multi-Purpose Analysis**: Supports knowledge base generation, company analysis, prospect qualification, and HTML analysis.
-   **Automated Web Crawling**: Crawls websites to discover pages using either a simple requests-based crawler or a Selenium-based crawler for dynamic content.
-   **AI-Powered Page Selection**: Leverages OpenAI's language models to intelligently select the most relevant pages for knowledge base creation, focusing on informational content like "About Us", "Contact", "FAQ", policies, etc.
-   **AI-Driven Content Extraction**: Extracts meaningful information from the HTML of selected pages, translating content into a target language if necessary.
-   **AI-Compiled Knowledge Base**: Synthesizes extracted information from multiple pages into a single, well-structured Markdown document.
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
        "id": "job_id_example",
        "job_type": "knowledge_base_generation",
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
-   **OpenAI Helper Functions**:
    -   `detect_language_from_html_with_openai`: Detects the primary language of HTML content.
    -   `select_relevant_pages_for_kb_with_openai`: Prompts the AI to choose the most important pages from a list of crawled URLs based on their titles and relevance criteria.
    -   `extract_knowledge_from_page_with_openai`: Prompts the AI to extract and structure knowledge from the raw HTML of a single page, translating to the target language.
    -   `compile_final_knowledge_base_with_openai`: Prompts the AI to synthesize all extracted knowledge chunks into a final, coherent Markdown document.
-   **Background Job Runners**:
    -   `run_knowledge_base_job`: The core function executed in a separate thread for each knowledge base generation request. It orchestrates the crawling, language detection, page selection, extraction, and compilation steps. It meticulously updates the job status and logs token usage.
    -   `run_company_analysis_job`: Executes company analysis tasks in a separate thread.
    -   `run_prospect_qualification_job`: Executes prospect qualification tasks in a separate thread.
-   **API Endpoints**: Flask routes for starting various analysis jobs, checking status, listing jobs, and health checks.
-   **Error Handling**: Includes Flask error handlers for common HTTP errors (404, 405, 500) and specific error messages within the API responses.
-   **Logging**: Comprehensive logging throughout the application lifecycle, including detailed logs for each job's progress and any errors encountered.

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