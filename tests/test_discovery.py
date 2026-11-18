"""Offline tests for page discovery failures, address forms, and HEAD-refusing sites."""
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import grand_spider as gs


class FakeResponse:
    def __init__(self, url, text="", status_code=200, content_type="text/html", reason="OK"):
        self.url = url
        self.text = text
        self.status_code = status_code
        self.reason = reason
        self.headers = {"Content-Type": content_type}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise gs.requests.exceptions.HTTPError(f"{self.status_code} error", response=self)

    def close(self):
        pass


@pytest.fixture
def offline_crawl(monkeypatch):
    """Route SESSION.get through a dict of url -> FakeResponse | Exception."""
    site = {}
    requested = []

    def fake_get(url, **kwargs):
        requested.append(url)
        result = site.get(url, FakeResponse(url, status_code=404))
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(gs, "get_robots_parser", lambda url: None)
    monkeypatch.setattr(gs, "find_sitemap_urls", lambda url: ([], {}))
    monkeypatch.setattr(gs.SESSION, "get", fake_get)
    monkeypatch.setattr(gs.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(gs, "validate_public_http_url", lambda url: None)
    site["_requested"] = requested
    return site


def _links(*hrefs):
    return "<html><body>" + "".join(f'<a href="{h}">x</a>' for h in hrefs) + "</body></html>"


def test_unreadable_start_page_marks_discovery_failed(offline_crawl):
    base = "https://example.com/"
    offline_crawl[base] = gs.requests.exceptions.ReadTimeout("read timed out")

    candidates, meta, _ = gs.discover_all_candidate_urls(base)

    assert meta["crawled_urls"] == 0
    assert meta["failed"] is True
    assert meta["error_code"] == "timed_out"
    # The start page gets a second try before discovery gives up.
    assert offline_crawl["_requested"].count(base) == 2


def test_start_page_recovers_on_second_try(offline_crawl):
    base = "https://example.com/"
    calls = {"n": 0}
    real_get = gs.SESSION.get

    def flaky(url, **kwargs):
        if url == base and calls["n"] == 0:
            calls["n"] += 1
            raise gs.requests.exceptions.ConnectionError("reset")
        return real_get(url, **kwargs)

    gs.SESSION.get = flaky
    offline_crawl[base] = FakeResponse(base, _links("/about"))
    offline_crawl["https://example.com/about"] = FakeResponse("https://example.com/about", "About")

    candidates, meta, _ = gs.discover_all_candidate_urls(base)

    assert not meta.get("failed")
    assert "https://example.com/about" in candidates


def test_real_one_page_site_is_not_a_discovery_failure(offline_crawl):
    base = "https://example.com/"
    offline_crawl[base] = FakeResponse(base, _links("/gone", "#top", "mailto:a@example.com"))

    candidates, meta, _ = gs.discover_all_candidate_urls(base)

    assert meta["crawled_urls"] == 1
    assert not meta.get("failed")  # /gone is a dead link (404), not a network failure


def test_links_that_all_time_out_mark_discovery_failed(offline_crawl):
    base = "https://example.com/"
    offline_crawl[base] = FakeResponse(base, _links("/a", "/b"))
    offline_crawl["https://example.com/a"] = gs.requests.exceptions.ConnectTimeout("timed out")
    offline_crawl["https://example.com/b"] = gs.requests.exceptions.ConnectTimeout("timed out")

    _, meta, _ = gs.discover_all_candidate_urls(base)

    assert meta["failed"] is True
    assert meta["error_code"] == "timed_out"


def test_relative_links_resolve_against_their_own_page(offline_crawl):
    base = "http://info.example.org"
    project = "http://info.example.org/hypertext/WWW/TheProject.html"
    offline_crawl[base] = FakeResponse(base, _links(project))
    offline_crawl[project] = FakeResponse(project, _links("Summary.html", "../Top.html"))
    offline_crawl["http://info.example.org/hypertext/WWW/Summary.html"] = FakeResponse(
        "http://info.example.org/hypertext/WWW/Summary.html", "Summary")
    offline_crawl["http://info.example.org/hypertext/Top.html"] = FakeResponse(
        "http://info.example.org/hypertext/Top.html", "Top")

    candidates, _, _ = gs.discover_all_candidate_urls(base)

    assert "http://info.example.org/hypertext/WWW/Summary.html" in candidates
    assert "http://info.example.org/hypertext/Top.html" in candidates
    assert "http://info.example.org/Summary.html" not in offline_crawl["_requested"]


def test_all_addresses_use_the_scheme_the_site_redirects_to(offline_crawl):
    base = "http://example.com/"
    landed = "https://www.example.com/"
    offline_crawl[base] = FakeResponse(landed, _links(
        "http://example.com/about", "https://www.example.com/contact", "/faq"))
    for path in ("about", "contact", "faq"):
        url = f"https://www.example.com/{path}"
        offline_crawl[url] = FakeResponse(url, "page")

    candidates, meta, _ = gs.discover_all_candidate_urls(base)

    assert meta["site_url"] == landed
    assert candidates[0] == landed
    assert all(url.startswith("https://www.example.com/") for url in candidates), candidates
    assert len(set(candidates)) == len(candidates)


def test_mixed_scheme_links_take_the_start_page_form(offline_crawl):
    base = "https://info.example.org"
    offline_crawl[base] = FakeResponse(base, _links("http://info.example.org/hypertext/A.html"))
    offline_crawl["https://info.example.org/hypertext/A.html"] = FakeResponse(
        "https://info.example.org/hypertext/A.html", "A")

    candidates, _, _ = gs.discover_all_candidate_urls(base)

    assert set(candidates) == {base, "https://info.example.org/hypertext/A.html"}


def test_sitemap_addresses_use_the_redirected_form(offline_crawl, monkeypatch):
    base = "http://example.com/"
    sitemap = [f"https://www.example.com/p{i}" for i in range(5)] + ["http://example.com/p0"]
    monkeypatch.setattr(gs, "find_sitemap_urls", lambda url: (sitemap, {}))
    monkeypatch.setattr(gs.SESSION, "head", lambda url, **kw: FakeResponse("https://www.example.com/"))

    candidates, meta, _ = gs.discover_all_candidate_urls(base)

    assert meta["method"] == "sitemap"
    assert meta["site_url"] == "https://www.example.com/"
    assert candidates == ["https://www.example.com/"] + sitemap[:5]


def test_one_timeout_among_dead_links_is_not_a_discovery_failure(offline_crawl):
    base = "https://example.com/"
    offline_crawl[base] = FakeResponse(base, _links("/gone", "/slow"))
    offline_crawl["https://example.com/slow"] = gs.requests.exceptions.ReadTimeout("timed out")

    _, meta, _ = gs.discover_all_candidate_urls(base)

    assert not meta.get("failed")


def test_redirected_origin_uses_its_own_robots_rules(offline_crawl, monkeypatch):
    base = "https://example.com/"
    landed = "https://www.example.com/"
    offline_crawl[base] = FakeResponse(landed, _links("/private", "/about"))
    offline_crawl["https://www.example.com/about"] = FakeResponse("https://www.example.com/about", "A")
    offline_crawl["https://www.example.com/private"] = FakeResponse("https://www.example.com/private", "P")
    origins = []

    class Rules:
        def __init__(self, origin):
            self.origin = origin

        def can_fetch(self, agent, url):
            return not (self.origin.startswith("https://www.") and url.endswith("/private"))

    def robots_for(origin):
        origins.append(origin)
        return Rules(origin)

    monkeypatch.setattr(gs, "get_robots_parser", robots_for)

    candidates, _, _ = gs.discover_all_candidate_urls(base)

    assert origins == [base, landed]
    assert "https://www.example.com/about" in candidates
    assert "https://www.example.com/private" not in candidates


def test_malformed_link_does_not_fail_discovery(offline_crawl):
    base = "https://example.com/"
    offline_crawl[base] = FakeResponse(base, _links("http://[broken", "/about"))
    offline_crawl["https://example.com/about"] = FakeResponse("https://example.com/about", "A")

    candidates, meta, _ = gs.discover_all_candidate_urls(base)

    assert not meta.get("failed")
    assert "https://example.com/about" in candidates


def test_scheme_only_redirect_reloads_robots_rules(offline_crawl, monkeypatch):
    base = "http://example.com/"
    offline_crawl[base] = FakeResponse("https://example.com/", "<html></html>")
    origins = []
    monkeypatch.setattr(gs, "get_robots_parser", lambda origin: origins.append(origin))

    gs.discover_all_candidate_urls(base)

    assert origins == [base, "https://example.com/"]


def test_redirect_to_another_site_keeps_submitted_address():
    assert gs.canonicalize_site_url("http://example.com/a#x", "https://other.org/") == "http://example.com/a"
    assert gs.canonicalize_site_url("http://example.com:8080/a", "https://example.com/") == \
        "http://example.com:8080/a"


def _register_job():
    job_id = str(uuid.uuid4())
    gs.register_job(job_id, {"id": job_id, "status": "pending"})
    return job_id


@pytest.mark.parametrize("budget,expect_failed", [(20, True), (1, False)])
def test_job_fails_when_discovery_failed_on_multi_page_budget(monkeypatch, budget, expect_failed):
    base = "https://example.com/"
    job_id = _register_job()
    monkeypatch.setattr(gs, "get_robots_parser", lambda origin: None)
    monkeypatch.setattr(gs, "fetch_url_html_content", lambda url: "<html>home</html>")
    monkeypatch.setattr(gs, "discover_all_candidate_urls", lambda *args: (
        [base], {"method": "crawl", "crawled_urls": 0, "failed": True,
                 "error": "read timed out", "error_code": "timed_out", "site_url": base}, {}))
    reached = {}

    def stop_after_selection(*args, **kwargs):
        reached["fetch"] = True
        return [], [], {}, []

    monkeypatch.setattr(gs, "fetch_clean_pages_parallel", stop_after_selection)
    monkeypatch.setattr(gs, "notify_job_callback", lambda *args: None)

    gs.run_knowledge_base_job(job_id, base, budget, False, previous_fingerprint="0" * 64)

    job = gs.jobs[job_id]
    assert job["status"] == "failed"
    if expect_failed:
        assert job["error_code"] == "timed_out"
        assert "discovery" in job["error"].lower()
        assert "fetch" not in reached
    else:
        assert "fetch" in reached  # a one-page budget never needed discovery


def test_probe_retries_with_get_when_head_errors(monkeypatch):
    def head(*args, **kwargs):
        raise gs.requests.exceptions.ConnectionError("connection reset by firewall")

    gets = []
    monkeypatch.setattr(gs.SESSION, "head", head)
    monkeypatch.setattr(gs.SESSION, "get", lambda url, **kw: gets.append(kw) or FakeResponse(url))

    response = gs.probe_url("https://example.com/")

    assert response.status_code == 200
    assert gets[0]["stream"] is True


def test_probe_skips_get_when_head_succeeds(monkeypatch):
    monkeypatch.setattr(gs.SESSION, "head", lambda url, **kw: FakeResponse(url))
    monkeypatch.setattr(gs.SESSION, "get", lambda *a, **kw: pytest.fail("GET not needed"))
    assert gs.probe_url("https://example.com/").status_code == 200


def test_scrape_page_accepts_site_that_refuses_head(monkeypatch):
    monkeypatch.setattr(gs.SESSION, "head", lambda url, **kw: FakeResponse(url, status_code=400))
    monkeypatch.setattr(gs.SESSION, "get", lambda url, **kw: FakeResponse(url))
    monkeypatch.setattr(gs, "fetch_url_html_content", lambda url: None)

    with pytest.raises(ValueError, match="Could not fetch HTML"):  # got past the probe
        gs.scrape_single_page("https://example.com/")
