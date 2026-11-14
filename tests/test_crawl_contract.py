"""Offline tests for crawl fingerprints, incremental jobs, and API response fields."""
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import grand_spider as gs


def test_crawl_failures_use_stable_error_codes():
    blocked = type("BlockedError", (Exception,), {
        "response": type("Response", (), {"status_code": 403})()
    })("forbidden")
    assert gs.crawl_failure_code(blocked) == "blocked"
    assert gs.crawl_failure_code(TimeoutError("request timed out")) == "timed_out"
    assert gs.crawl_failure_code(RuntimeError("no readable page content")) == "no_readable_content"
    assert gs._http_error_code(503) == "site_unreachable"
    assert gs._http_error_code(408) == "timed_out"
    assert gs._http_error_code(504) == "timed_out"


class TestSourceFingerprints:
    def test_page_fingerprint_covers_title_and_clean_text(self):
        original = gs.fingerprint_source_page("https://example.com/a", "About", "Same text")
        assert original == gs.fingerprint_source_page("https://example.com/a", "About", "Same text")
        assert original != gs.fingerprint_source_page("https://example.com/a", "About us", "Same text")
        assert original != gs.fingerprint_source_page("https://example.com/a", "About", "Changed")

    def test_job_fingerprint_ignores_result_order_but_includes_url(self):
        pages = [
            {"url": "https://example.com/a#section", "fingerprint": "a" * 64},
            {"url": "https://example.com/b", "fingerprint": "b" * 64},
        ]
        assert gs.fingerprint_source_pages(pages) == gs.fingerprint_source_pages(list(reversed(pages)))
        changed_url = [dict(pages[0], url="https://example.com/other"), pages[1]]
        assert gs.fingerprint_source_pages(pages) != gs.fingerprint_source_pages(changed_url)

    def test_deterministic_selector_never_calls_model_selection(self, monkeypatch):
        def fail_if_called(*args, **kwargs):
            raise AssertionError("fingerprint selection must not call the model")

        monkeypatch.setattr(gs, "identify_knowledge_rich_content_clusters", fail_if_called)
        urls = ["https://shop.example/blog/", "https://shop.example/contact/",
                "https://shop.example/about/", "https://shop.example/"]
        first, first_meta = gs.select_knowledge_pages_deterministically(
            "https://shop.example/", urls, 3,
            sitemap_meta={"https://shop.example/contact/": {"priority": 0.9, "lastmod": "2026-01-01"}},
        )
        second, second_meta = gs.select_knowledge_pages_deterministically(
            "https://shop.example/", list(reversed(urls)), 3,
            sitemap_meta={"https://shop.example/about/": {"priority": 1.0, "lastmod": "2026-12-01"}},
        )
        assert first == second
        assert [page["url"] for page in first] == [
            "https://shop.example/", "https://shop.example/about/", "https://shop.example/contact/"
        ]
        assert first_meta["llm_selection_skipped"] is True
        assert second_meta["selection_method"] == "deterministic_fingerprint_stable"

        products = [f"https://shop.example/product/item-{index:02d}" for index in range(25)]
        product_first, _ = gs.select_knowledge_pages_deterministically(
            "https://shop.example/", products, 8
        )
        product_second, _ = gs.select_knowledge_pages_deterministically(
            "https://shop.example/", list(reversed(products)), 8
        )
        assert product_first == product_second


def test_fetch_clean_pages_returns_source_text_and_fingerprint(monkeypatch):
    monkeypatch.setattr(gs, "SELENIUM_AVAILABLE", False)
    monkeypatch.setattr(gs, "get_robots_parser", lambda origin: None)
    monkeypatch.setattr(gs, "clean_text_from_html", lambda html, url: html.strip())
    monkeypatch.setattr(gs, "get_page_title_from_html", lambda html: "Article title")
    monkeypatch.setattr(gs, "extract_structured_data", lambda html, url: {"url": url})
    monkeypatch.setattr(gs, "fetch_url_html_content", lambda url: "Clean article body")

    pages, structured, stats, failures = gs.fetch_clean_pages_parallel([
        {"url": "https://example.com/article", "cluster": "educational_content"}
    ])

    assert pages == [{
        "url": "https://example.com/article",
        "title": "Article title",
        "fingerprint": gs.fingerprint_source_page(
            "https://example.com/article", "Article title", "Clean article body"
        ),
        "text": "Clean article body",
        "cluster": "educational_content",
    }]
    assert structured == [{"url": "https://example.com/article"}]
    assert stats["selenium_fallbacks"] == 0
    assert failures == []


def test_fetch_clean_pages_keeps_stable_errors_for_unreadable_pages(monkeypatch):
    monkeypatch.setattr(gs, "SELENIUM_AVAILABLE", False)
    monkeypatch.setattr(gs, "get_robots_parser", lambda origin: None)

    def blocked_fetch(url):
        raise gs.CrawlServiceError("HTTP 403 forbidden", "blocked")

    monkeypatch.setattr(gs, "fetch_url_html_content", blocked_fetch)
    pages, _, _, failures = gs.fetch_clean_pages_parallel([
        {"url": "https://example.com/private"}
    ])

    assert pages == []
    assert failures[0]["error_code"] == "blocked"


def test_fetch_clean_pages_respects_robots_disallow(monkeypatch):
    monkeypatch.setattr(gs, "get_robots_parser", lambda origin: object())
    monkeypatch.setattr(gs, "robots_allows", lambda parser, url: False)
    monkeypatch.setattr(gs, "fetch_url_html_content", lambda url: pytest.fail(
        "robots-disallowed URLs must not be fetched"
    ))

    pages, _, _, failures = gs.fetch_clean_pages_parallel([
        {"url": "https://example.com/private"}
    ])

    assert pages == []
    assert failures[0]["error_code"] == "blocked"


def test_simple_fallback_crawl_has_stable_link_order(monkeypatch):
    base_url = "https://example.com/"
    orders = [
        '<a href="/z">Z</a><a href="/a">A</a>',
        '<a href="/a">A</a><a href="/z">Z</a>',
    ]
    current_links = {"html": orders[0]}
    visited = []

    class Response:
        status_code = 200
        headers = {"Content-Type": "text/html"}

        def __init__(self, url):
            self.url = url
            self.text = ("<html><body>" + current_links["html"] + "</body></html>"
                         if url == base_url else "<html><body>Page</body></html>")

        def raise_for_status(self):
            pass

    def fake_get(url, **kwargs):
        visited.append(url)
        return Response(url)

    monkeypatch.setattr(gs, "get_robots_parser", lambda url: None)
    monkeypatch.setattr(gs, "robots_allows", lambda robots, url: True)
    monkeypatch.setattr(gs.SESSION, "get", fake_get)
    monkeypatch.setattr(gs.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(gs, "CRAWL_DELAY_SECONDS", 0)

    first = gs.simple_crawl_website(base_url, max_pages=3)
    first_order = [page["url"] for page in first]
    current_links["html"] = orders[1]
    visited.clear()
    second = gs.simple_crawl_website(base_url, max_pages=3)

    assert first_order == [base_url, "https://example.com/a", "https://example.com/z"]
    assert [page["url"] for page in second] == first_order


def test_discovery_cap_uses_stable_priority_order(monkeypatch):
    base_url = "https://example.com/"
    sitemap = {"urls": [
        "https://example.com/z", "https://example.com/about", "https://example.com/contact",
        "https://example.com/a", "https://example.com/terms",
    ], "meta": {
        "https://example.com/about": {"priority": 0.8, "lastmod": "2026-01-01"},
        "https://example.com/contact": {"priority": 0.9, "lastmod": "2026-01-01"},
    }}
    monkeypatch.setattr(gs, "find_sitemap_urls", lambda url: (sitemap["urls"], sitemap["meta"]))
    monkeypatch.setattr(gs, "MAX_DISCOVERY_URLS", 3)

    first, first_meta, _ = gs.discover_all_candidate_urls(base_url)
    sitemap["urls"] = list(reversed(sitemap["urls"]))
    second, second_meta, _ = gs.discover_all_candidate_urls(base_url)

    assert first_meta["capped"] is True
    assert second_meta["capped"] is True
    assert first == second
    assert first[0] == base_url


def test_specific_pages_are_exact_and_budgeted_stably(monkeypatch):
    monkeypatch.setattr(gs.SESSION, "head", lambda *args, **kwargs: pytest.fail(
        "explicit URLs are fetched directly so the job can report the actual GET failure"
    ))
    base_url = "https://example.com/"
    specified = ["https://example.com/article", "https://example.com/faq"]
    discovered = gs.discover_core_pages_only(base_url, specified)
    reversed_discovered = gs.discover_core_pages_only(base_url, list(reversed(specified)))
    first = gs.select_specific_pages_deterministically(base_url, discovered, 1)
    second = gs.select_specific_pages_deterministically(base_url, reversed_discovered, 1)

    assert [page["url"] for page in discovered] == specified
    assert first == second
    assert first[0]["url"] == "https://example.com/faq"


def test_core_page_discovery_respects_robots_before_head(monkeypatch):
    monkeypatch.setattr(gs, "get_robots_parser", lambda origin: object())
    monkeypatch.setattr(gs, "robots_allows", lambda parser, url: False)
    monkeypatch.setattr(gs.SESSION, "head", lambda *args, **kwargs: pytest.fail(
        "robots-disallowed core URLs must not be probed"
    ))

    pages = gs.discover_core_pages_only("https://example.com/")

    assert pages == [{
        "url": "https://example.com/",
        "title": "Main Page",
        "type": "main_page",
    }]


def _register_test_job():
    job_id = f"test-{uuid.uuid4()}"
    gs.register_job(job_id, {
        "id": job_id,
        "job_type": "knowledge_base_generation",
        "status": "pending",
        "created_at": gs.time.time(),
    })
    return job_id


def test_unchanged_job_skips_model_steps_and_omits_document(monkeypatch):
    job_id = _register_test_job()
    base_url = "https://example.com/"
    source_page = {
        "url": base_url,
        "title": "Example",
        "fingerprint": gs.fingerprint_source_page(base_url, "Example", "Stable source"),
        "text": "Stable source",
        "cluster": "company_information",
    }
    previous = gs.fingerprint_source_pages([source_page])
    monkeypatch.setattr(gs, "get_robots_parser", lambda origin: None)
    monkeypatch.setattr(gs, "fetch_url_html_content", lambda url: "<html>cached homepage</html>")
    monkeypatch.setattr(gs, "discover_all_candidate_urls", lambda *args: (
        [base_url], {"method": "sitemap"}, {}
    ))
    monkeypatch.setattr(gs, "select_knowledge_pages_deterministically", lambda *args, **kwargs: (
        [{"url": base_url, "cluster": "company_information"}], {"llm_selection_skipped": True}
    ))
    monkeypatch.setattr(gs, "fetch_clean_pages_parallel", lambda *args, **kwargs: (
        [source_page], [], {"selenium_fallbacks": 0}, []
    ))
    monkeypatch.setattr(gs, "capture_full_page_screenshot", lambda *args: pytest.fail("screenshot work should be skipped"))
    monkeypatch.setattr(gs, "detect_site_language", lambda *args, **kwargs: pytest.fail("language model should be skipped"))
    monkeypatch.setattr(gs, "llm_chat", lambda *args, **kwargs: pytest.fail("LLM call should be skipped"))
    monkeypatch.setattr(gs, "notify_job_callback", lambda *args: None)

    gs.run_knowledge_base_job(job_id, base_url, 1, False, previous_fingerprint=previous)
    job = gs.jobs[job_id]

    assert job["status"] == "completed"
    assert job["unchanged"] is True
    assert job["source_fingerprint"] == previous
    assert "final_knowledge_base" not in job
    assert job["single_page_text"] == "Stable source"
    assert job["pages"][0]["text"] == "Stable source"
    assert job["cost_estimation"]["prompt_tokens"] == 0


def test_partial_page_failure_never_short_circuits_as_unchanged(monkeypatch):
    job_id = _register_test_job()
    base_url = "https://example.com/"
    source_page = {
        "url": base_url,
        "title": "Example",
        "fingerprint": gs.fingerprint_source_page(base_url, "Example", "Stable source"),
        "text": "Stable source",
        "cluster": "company_information",
    }
    previous = gs.fingerprint_source_pages([source_page])
    language_detection_called = []
    monkeypatch.setattr(gs, "get_robots_parser", lambda origin: None)
    monkeypatch.setattr(gs, "fetch_url_html_content", lambda url: "<html>homepage</html>")
    monkeypatch.setattr(gs, "discover_all_candidate_urls", lambda *args: (
        [base_url, "https://example.com/faq"], {"method": "sitemap"}, {}
    ))
    monkeypatch.setattr(gs, "select_knowledge_pages_deterministically", lambda *args, **kwargs: (
        [{"url": base_url, "cluster": "company_information"},
         {"url": "https://example.com/faq", "cluster": "auto_keyword"}],
        {"llm_selection_skipped": True},
    ))
    monkeypatch.setattr(gs, "fetch_clean_pages_parallel", lambda *args, **kwargs: (
        [source_page], [], {"selenium_fallbacks": 0}, [{
            "url": "https://example.com/faq", "error": "read failed",
            "error_code": "site_unreachable",
        }]
    ))
    monkeypatch.setattr(gs, "detect_site_language", lambda *args, **kwargs: (
        language_detection_called.append(True) or "en"
    ))
    monkeypatch.setattr(gs, "capture_full_page_screenshot", lambda *args: (_ for _ in ()).throw(
        RuntimeError("stop after proving the job continued")
    ))
    monkeypatch.setattr(gs, "notify_job_callback", lambda *args: None)

    gs.run_knowledge_base_job(job_id, base_url, 2, False, previous_fingerprint=previous)

    assert language_detection_called
    assert gs.jobs[job_id]["status"] == "failed"
    assert gs.jobs[job_id].get("unchanged") is not True
    assert gs.jobs[job_id]["page_failures"][0]["error_code"] == "site_unreachable"


def test_job_checks_homepage_robots_before_fetching(monkeypatch):
    job_id = _register_test_job()
    base_url = "https://example.com/"
    monkeypatch.setattr(gs, "get_robots_parser", lambda origin: object())
    monkeypatch.setattr(gs, "robots_allows", lambda parser, url: False)
    monkeypatch.setattr(gs, "fetch_url_html_content", lambda url: pytest.fail(
        "homepage content must not be fetched when robots.txt disallows it"
    ))
    monkeypatch.setattr(gs, "discover_all_candidate_urls", lambda *args: (
        [base_url], {"method": "sitemap"}, {}
    ))
    monkeypatch.setattr(gs, "select_knowledge_pages_deterministically", lambda *args, **kwargs: (
        [{"url": base_url, "cluster": "company_information"}], {"llm_selection_skipped": True}
    ))
    monkeypatch.setattr(gs, "fetch_clean_pages_parallel", lambda *args, **kwargs: (
        [], [], {"selenium_fallbacks": 0}, [{
            "url": base_url, "error": "The URL is disallowed by robots.txt.",
            "error_code": "blocked",
        }]
    ))
    monkeypatch.setattr(gs, "notify_job_callback", lambda *args: None)

    gs.run_knowledge_base_job(job_id, base_url, 1, False)
    assert gs.jobs[job_id]["status"] == "failed"
    assert gs.jobs[job_id]["error_code"] == "blocked"


def test_job_without_readable_pages_fails_with_fixed_code(monkeypatch):
    job_id = _register_test_job()
    base_url = "https://example.com/"
    monkeypatch.setattr(gs, "get_robots_parser", lambda origin: None)
    monkeypatch.setattr(gs, "fetch_url_html_content", lambda url: None)
    monkeypatch.setattr(gs, "discover_all_candidate_urls", lambda *args: (
        [base_url], {"method": "sitemap"}, {}
    ))
    monkeypatch.setattr(gs, "select_knowledge_pages_deterministically", lambda *args, **kwargs: (
        [{"url": base_url, "cluster": "company_information"}], {"llm_selection_skipped": True}
    ))
    monkeypatch.setattr(gs, "fetch_clean_pages_parallel", lambda *args, **kwargs: (
        [], [], {"selenium_fallbacks": 0}, []
    ))
    monkeypatch.setattr(gs, "notify_job_callback", lambda *args: None)

    gs.run_knowledge_base_job(job_id, base_url, 1, False)
    assert gs.jobs[job_id]["status"] == "failed"
    assert gs.jobs[job_id]["error_code"] == "no_readable_content"


def test_changed_single_page_job_returns_page_text_and_document(monkeypatch):
    job_id = _register_test_job()
    base_url = "https://example.com/article"
    source_page = {
        "url": base_url,
        "title": "Article",
        "fingerprint": gs.fingerprint_source_page(base_url, "Article", "Clean article body"),
        "text": "Clean article body",
        "cluster": "company_information",
    }
    monkeypatch.setattr(gs, "get_robots_parser", lambda origin: None)
    monkeypatch.setattr(gs, "fetch_url_html_content", lambda url: "<html>homepage</html>")
    monkeypatch.setattr(gs, "discover_all_candidate_urls", lambda *args: (
        [base_url], {"method": "sitemap"}, {}
    ))
    monkeypatch.setattr(gs, "select_knowledge_pages_deterministically", lambda *args, **kwargs: (
        [{"url": base_url, "cluster": "company_information"}], {"llm_selection_skipped": True}
    ))
    monkeypatch.setattr(gs, "fetch_clean_pages_parallel", lambda *args, **kwargs: (
        [source_page], [], {"selenium_fallbacks": 0}, []
    ))
    monkeypatch.setattr(gs, "detect_site_language", lambda *args, **kwargs: "en")
    monkeypatch.setattr(gs, "capture_full_page_screenshot", lambda *args: None)
    monkeypatch.setattr(gs, "extract_structured_data", lambda *args: {})
    monkeypatch.setattr(gs, "build_brand_kit", lambda *args, **kwargs: {
        "palette": {"primary": "#123456", "background": "#ffffff"}
    })
    monkeypatch.setattr(gs, "extract_prepared_pages_parallel", lambda *args, **kwargs: (
        [{"url": base_url, "primary_category": "company_overview", "extracted_chunk": "Useful facts"}],
        {"cache_hits": 0},
    ))
    monkeypatch.setattr(gs, "synthesize_section", lambda *args, **kwargs: "Synthesized section")
    monkeypatch.setattr(gs, "assemble_final_kb", lambda *args, **kwargs: (
        "# Generated knowledge base", {"sections_present": ["company_overview"]}
    ))
    monkeypatch.setattr(gs, "check_kb_completeness", lambda *args: {"has_contact": False})
    monkeypatch.setattr(gs, "save_knowledge_base_report", lambda *args, **kwargs: None)
    monkeypatch.setattr(gs, "notify_job_callback", lambda *args: None)

    gs.run_knowledge_base_job(job_id, base_url, 1, False)
    job = gs.jobs[job_id]

    assert job["status"] == "completed"
    assert job["unchanged"] is False
    assert job["final_knowledge_base"] == "# Generated knowledge base"
    assert job["pages"][0]["text"] == "Clean article body"
    assert job["single_page_text"] == "Clean article body"
    assert job["page_fingerprints"][base_url] == source_page["fingerprint"]


def test_api_clamps_page_budget_and_returns_limit(monkeypatch):
    monkeypatch.setattr(gs, "EXPECTED_SERVICE_API_KEY", "test-key")
    monkeypatch.setattr(gs, "get_robots_parser", lambda origin: None)
    monkeypatch.setattr(gs.SESSION, "head", lambda *args, **kwargs: type(
        "HeadResponse", (), {"status_code": 200, "reason": "OK"}
    )())

    class FakeThread:
        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs

        def start(self):
            pass

    monkeypatch.setattr(gs.threading, "Thread", FakeThread)
    response = gs.app.test_client().post(
        "/api/generate-knowledge-base",
        headers={"api-key": "test-key"},
        json={"url": "https://93.184.216.34/", "max_pages": 120},
    )

    assert response.status_code == 202
    assert response.get_json()["page_budget"] == 80
    assert response.get_json()["max_pages_limit"] == 80


@pytest.mark.parametrize("specific_pages", ["", None, 17, {}, []])
def test_api_rejects_malformed_or_empty_specific_pages_before_crawling(monkeypatch, specific_pages):
    monkeypatch.setattr(gs, "EXPECTED_SERVICE_API_KEY", "test-key")
    monkeypatch.setattr(gs, "validate_public_http_url", lambda url: None)
    monkeypatch.setattr(gs.SESSION, "head", lambda *args, **kwargs: pytest.fail(
        "invalid specific_pages must be rejected before preflight or crawling"
    ))

    response = gs.app.test_client().post(
        "/api/generate-knowledge-base",
        headers={"api-key": "test-key"},
        json={"url": "https://example.com/", "specific_pages": specific_pages},
    )

    assert response.status_code == 400
    assert "specific_pages" in response.get_json()["error"]


def test_api_probes_get_when_head_is_not_supported(monkeypatch):
    monkeypatch.setattr(gs, "EXPECTED_SERVICE_API_KEY", "test-key")
    monkeypatch.setattr(gs, "get_robots_parser", lambda origin: None)
    monkeypatch.setattr(gs.SESSION, "head", lambda *args, **kwargs: type(
        "HeadResponse", (), {"status_code": 405, "reason": "Method Not Allowed"}
    )())
    get_calls = []

    class GetResponse:
        status_code = 200
        reason = "OK"

        def close(self):
            pass

    def fake_get(*args, **kwargs):
        get_calls.append((args, kwargs))
        return GetResponse()

    class FakeThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(gs.SESSION, "get", fake_get)
    monkeypatch.setattr(gs.threading, "Thread", FakeThread)
    response = gs.app.test_client().post(
        "/api/generate-knowledge-base",
        headers={"api-key": "test-key"},
        json={"url": "https://93.184.216.34/", "max_pages": 1},
    )

    assert response.status_code == 202
    assert get_calls[0][1]["stream"] is True


def test_completion_callback_posts_compact_event(monkeypatch):
    job_id = _register_test_job()
    gs.jobs[job_id].update({
        "callback_url": "https://example.com/callback",
        "status": "completed",
        "source_fingerprint": "a" * 64,
        "unchanged": True,
        "finished_at": 123.0,
    })
    calls = []
    monkeypatch.setattr(gs, "post_callback_event", lambda url, payload: calls.append((url, payload)))
    gs.notify_job_callback(job_id)

    callback_url, payload = calls[0]
    assert callback_url == "https://example.com/callback"
    assert payload == {
        "job_id": job_id,
        "status": "completed",
        "source_fingerprint": "a" * 64,
        "unchanged": True,
        "error_code": None,
        "finished_at": 123.0,
    }


def test_callback_event_uses_shared_session_and_pins_resolved_ip(monkeypatch):
    monkeypatch.setattr(gs, "resolve_public_http_url", lambda url: (["93.184.216.34"], None))
    calls = []

    class CallbackResponse:
        def raise_for_status(self):
            pass

    def fake_post(*args, **kwargs):
        assert gs._pinned_public_ip.get() == "93.184.216.34"
        calls.append((args, kwargs))
        return CallbackResponse()

    monkeypatch.setattr(gs.SESSION, "post", fake_post)
    gs.post_callback_event("https://callback.example/hook", {"job_id": "job-1"})

    args, kwargs = calls[0]
    assert args[0] == "https://callback.example/hook"
    assert kwargs["allow_redirects"] is False
    assert kwargs["proxies"] == {
        "http": None,
        "https": None,
        "all": None,
        "https://callback.example": None,
        "all://callback.example": None,
    }
    assert kwargs["json"] == {"job_id": "job-1"}
    assert gs._pinned_public_ip.get() is None


def test_callback_request_disables_environment_and_session_proxies(monkeypatch):
    callback_url = "https://bücher.example/hook"
    normalized_hostname = "xn--bcher-kva.example"
    monkeypatch.setattr(gs, "resolve_public_http_url", lambda url: (["93.184.216.34"], None))
    monkeypatch.setattr(gs.requests.sessions, "get_environ_proxies", lambda *args, **kwargs: {
        "all": "http://env-proxy.invalid:8888",
        f"https://{normalized_hostname}": "http://host-proxy.invalid:8888",
    })
    monkeypatch.setattr(gs.SESSION, "proxies", {
        "all": "http://session-proxy.invalid:8888",
        f"https://{normalized_hostname}": "http://session-host-proxy.invalid:8888",
    })

    class CallbackResponse:
        def raise_for_status(self):
            pass

    def fake_send(prepared_request, **kwargs):
        assert gs.requests.utils.select_proxy(prepared_request.url, kwargs["proxies"]) is None
        return CallbackResponse()

    monkeypatch.setattr(gs.SESSION, "send", fake_send)
    gs.post_callback_event(callback_url, {"job_id": "job-1"})


def test_callback_event_fails_over_to_next_validated_public_ip(monkeypatch):
    monkeypatch.setattr(gs, "resolve_public_http_url", lambda url: (
        ["93.184.216.34", "93.184.216.35"], None
    ))
    attempted_ips = []

    class CallbackResponse:
        def raise_for_status(self):
            pass

    def fake_post(*args, **kwargs):
        pinned_ip = gs._pinned_public_ip.get()
        attempted_ips.append(pinned_ip)
        if pinned_ip == "93.184.216.34":
            raise gs.requests.exceptions.ConnectionError("first address is unreachable")
        return CallbackResponse()

    monkeypatch.setattr(gs.SESSION, "post", fake_post)
    gs.post_callback_event("https://callback.example/hook", {"job_id": "job-1"})

    assert attempted_ips == ["93.184.216.34", "93.184.216.35"]
    assert gs._pinned_public_ip.get() is None


def test_callback_adapter_connects_to_pinned_ip_and_keeps_hostname_for_tls(monkeypatch):
    callback_url = "https://callback.example/hook"
    prepared = gs.SESSION.prepare_request(gs.requests.Request("POST", callback_url, json={}))
    adapter = gs.SESSION.get_adapter(callback_url)
    pin_token = gs._pinned_public_ip.set("93.184.216.34")
    try:
        pool = adapter.get_connection_with_tls_context(
            prepared, verify=True, proxies={"http": None, "https": None}, cert=None
        )
        connection = pool._new_conn()
    finally:
        gs._pinned_public_ip.reset(pin_token)

    socket_destinations = []
    monkeypatch.setattr(gs.urllib3_connection.connection, "create_connection", lambda address, *args, **kwargs: (
        socket_destinations.append(address) or object()
    ))
    connection._new_conn()

    assert isinstance(pool, gs._PinnedHTTPSConnectionPool)
    assert connection.host == "callback.example"
    assert socket_destinations == [("93.184.216.34", 443)]


def test_job_status_exposes_page_contract_without_callback_url(monkeypatch):
    job_id = _register_test_job()
    gs.jobs[job_id].update({
        "status": "completed",
        "callback_url": "https://example.com/private-callback",
        "unchanged": True,
        "source_fingerprint": "a" * 64,
        "page_fingerprints": {"https://example.com/": "b" * 64},
        "pages": [{
            "url": "https://example.com/", "title": "Example",
            "fingerprint": "b" * 64, "text": "Clean source",
        }],
    })
    monkeypatch.setattr(gs, "EXPECTED_SERVICE_API_KEY", "test-key")

    response = gs.app.test_client().get(
        f"/api/jobs/{job_id}", headers={"api-key": "test-key"}
    )
    body = response.get_json()

    assert response.status_code == 200
    assert body["pages"][0]["text"] == "Clean source"
    assert body["source_fingerprint"] == "a" * 64
    assert "callback_url" not in body
