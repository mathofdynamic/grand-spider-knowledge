"""Unit tests for the pure, network-free helpers.

Run with:  python -m pytest tests/ -q
No OpenAI calls, no live HTTP — everything here is deterministic.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import brand_kit
import structured_data
import grand_spider as gs


# --------------------------------------------------------------------- URL safety

class TestValidatePublicHttpUrl:
    def test_rejects_non_http_schemes(self):
        assert gs.validate_public_http_url("file:///etc/passwd")
        assert gs.validate_public_http_url("ftp://example.com/x")
        assert gs.validate_public_http_url("javascript:alert(1)")

    def test_rejects_missing_or_invalid(self):
        assert gs.validate_public_http_url(None)
        assert gs.validate_public_http_url("")
        assert gs.validate_public_http_url("not a url")
        assert gs.validate_public_http_url(123)

    def test_rejects_loopback_private_linklocal(self):
        # IP literals resolve without DNS.
        assert gs.validate_public_http_url("http://127.0.0.1/")
        assert gs.validate_public_http_url("http://10.0.0.5/")
        assert gs.validate_public_http_url("http://192.168.1.1/admin")
        assert gs.validate_public_http_url("http://169.254.169.254/latest/meta-data/")

    def test_accepts_public_ip(self):
        assert gs.validate_public_http_url("http://93.184.216.34/") is None


# --------------------------------------------------------------------- prefilter

class TestPrefilterCandidateUrls:
    BASE = "shop.example"

    def test_drops_assets_transactional_offsite(self):
        urls = [
            "https://shop.example/about/",
            "https://shop.example/style.css",
            "https://shop.example/cart/",
            "https://other.example/about/",
            "https://shop.example/tag/sale/",
            "https://shop.example/page?add-to-cart=5",
        ]
        kept, dropped = gs.prefilter_candidate_urls(urls, self.BASE)
        assert kept == ["https://shop.example/about/"]
        assert dropped["asset"] == 1
        assert dropped["transactional"] == 1
        assert dropped["offsite"] == 1
        assert dropped["archive"] == 1
        assert dropped["faceted"] == 1

    def test_deduplicates_and_strips_fragments(self):
        urls = ["https://shop.example/faq#a", "https://shop.example/faq#b",
                "https://shop.example/faq"]
        kept, dropped = gs.prefilter_candidate_urls(urls, self.BASE)
        assert kept == ["https://shop.example/faq"]
        assert dropped["duplicate"] == 2

    def test_product_pages_sampled_not_all_dropped(self):
        urls = [f"https://shop.example/product/item-{i}" for i in range(40)]
        kept, dropped = gs.prefilter_candidate_urls(urls, self.BASE, keep_product_sample=5)
        assert len(kept) == 5
        assert dropped["product"] == 35


class TestKnowledgeScore:
    def test_obvious_pages_score_high(self):
        assert gs.knowledge_path_score("https://x.ir/about-us/") >= 2.0
        assert gs.knowledge_path_score("https://x.ir/تماس-با-ما/") >= 2.0
        assert gs.knowledge_path_score("https://x.ir/faq/") >= 2.0

    def test_deep_random_pages_score_low(self):
        assert gs.knowledge_path_score("https://x.ir/blog/2024/05/some-post") < 2.0
        assert gs.knowledge_path_score("https://x.ir/") < 2.0


# --------------------------------------------------------------------- language

class TestDetectLanguageDeterministic:
    def test_persian(self):
        text = "فروشگاه اینترنتی ما بهترین محصولات را با گارانتی ارائه می‌کند. " * 5
        assert gs.detect_language_deterministic(text) == "fa"

    def test_arabic(self):
        text = "نحن نقدم أفضل المنتجات والخدمات لعملائنا الكرام في جميع أنحاء العالم " * 5
        assert gs.detect_language_deterministic(text) == "ar"

    def test_english_returns_none(self):
        assert gs.detect_language_deterministic("Just a plain English sentence. " * 20) is None

    def test_empty(self):
        assert gs.detect_language_deterministic("") is None


# --------------------------------------------------------------------- parsing & tokens

class TestParseJsonResponse:
    def test_plain_json(self):
        assert gs.parse_json_response('{"a": 1}') == {"a": 1}

    def test_json_fenced(self):
        assert gs.parse_json_response('```json\n{"a": 1}\n```') == {"a": 1}

    def test_bare_fence(self):
        assert gs.parse_json_response('```\n{"a": 1}\n```') == {"a": 1}

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            gs.parse_json_response("")


class TestBatchChunksByTokens:
    def test_single_small_batch(self):
        batches = gs._batch_chunks_by_tokens(["hi", "there"], token_budget=1000)
        assert len(batches) == 1

    def test_splits_on_budget(self):
        texts = ["word " * 400] * 4  # ~400 tokens each
        batches = gs._batch_chunks_by_tokens(texts, token_budget=500)
        assert len(batches) == 4
        assert sum(len(b) for b in batches) == 4


class TestHarvestContactCandidates:
    def test_finds_emails_and_phones(self):
        out = gs.harvest_contact_candidates(
            ["Contact us at info@shop.ir or call +98 21 8877 6655.", None, ""])
        assert "info@shop.ir" in out["emails"]
        assert any("8877" in p for p in out["phones"])


class TestModelPricing:
    def test_known_prefix_match(self):
        pricing = gs.get_model_pricing("gpt-5-nano-2025-01-01")
        assert pricing == gs.MODEL_PRICING["gpt-5-nano"]

    def test_unknown_falls_back_to_default(self):
        assert gs.get_model_pricing("weird-model") == gs.DEFAULT_MODEL_PRICING


# --------------------------------------------------------------------- sitemap

class TestSitemapParsing:
    def test_entries_with_lastmod_priority(self):
        xml = """<?xml version="1.0"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://x.ir/about/</loc><lastmod>2026-01-02</lastmod><priority>0.9</priority></url>
          <url><loc>https://x.ir/p/1</loc></url>
        </urlset>"""
        entries, nested = gs.get_sitemap_entries_from_xml(xml)
        assert nested == []
        assert {"url": "https://x.ir/about/", "lastmod": "2026-01-02", "priority": 0.9} in entries
        assert {"url": "https://x.ir/p/1", "lastmod": None, "priority": None} in entries

    def test_sitemap_index(self):
        xml = """<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <sitemap><loc>https://x.ir/sitemap-posts.xml</loc></sitemap>
        </sitemapindex>"""
        entries, nested = gs.get_sitemap_entries_from_xml(xml)
        assert entries == []
        assert nested == ["https://x.ir/sitemap-posts.xml"]

    def test_invalid_xml(self):
        assert gs.get_sitemap_entries_from_xml("<not xml") == ([], [])


# --------------------------------------------------------------------- structured data

ORG_HTML = """<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@graph":[{"@type":"Organization","name":"Acme",
"logo":{"@type":"ImageObject","url":"https://acme.ir/logo.png"},
"telephone":"+98-21-1234","address":{"@type":"PostalAddress","streetAddress":"1 Main",
"addressLocality":"Tehran"},"sameAs":["https://instagram.com/acme"]}]}
</script>
<script type="application/ld+json">
{"@type":"FAQPage","mainEntity":[{"@type":"Question","name":"چگونه سفارش دهم؟",
"acceptedAnswer":{"@type":"Answer","text":"<p>تماس بگیرید.</p>"}}]}
</script>
<meta property="og:site_name" content="Acme Shop">
<meta name="theme-color" content="#e91e63">
</head><body><footer><a href="https://t.me/acme_chat?start=1">tg</a></footer></body></html>"""


class TestStructuredData:
    def test_full_harvest(self):
        f = structured_data.extract_structured_data(ORG_HTML, "https://acme.ir/")
        assert f["organization"]["name"] == "Acme"
        assert "+98-21-1234" in f["organization"]["phones"]
        assert f["organization"]["addresses"] == ["1 Main, Tehran"]
        assert "https://acme.ir/logo.png" in f["logo_candidates"]
        assert f["faq"][0]["answer"] == "تماس بگیرید."
        assert f["theme_color"] == "#e91e63"
        assert "https://instagram.com/acme" in f["socials"]
        assert "https://t.me/acme_chat" in f["socials"]  # query string stripped

    def test_merge_dedupes(self):
        f = structured_data.extract_structured_data(ORG_HTML, "https://acme.ir/")
        merged = structured_data.merge_structured_facts([f, f, {}])
        assert merged["organization"]["name"] == "Acme"
        assert len(merged["faq"]) == 1
        assert merged["socials"].count("https://instagram.com/acme") == 1

    def test_garbage_html_never_raises(self):
        f = structured_data.extract_structured_data("<script type='application/ld+json'>{bad", "u")
        assert f["organization"] == {"addresses": [], "phones": [], "emails": [], "opening_hours": []}


# --------------------------------------------------------------------- brand kit

class TestBrandKit:
    def test_normalize_hex(self):
        assert brand_kit.normalize_hex("#F60") == "#ff6600"
        assert brand_kit.normalize_hex("rgb(255, 102, 0)") == "#ff6600"
        assert brand_kit.normalize_hex("rgba(0,0,0,0.5)") == "#000000"
        assert brand_kit.normalize_hex("bogus") is None

    def test_near_neutral(self):
        assert brand_kit.is_near_neutral("#ffffff")
        assert brand_kit.is_near_neutral("#0a0a0a")
        assert brand_kit.is_near_neutral("#888888")
        assert not brand_kit.is_near_neutral("#e91e63")

    def test_sniff_image_ext_rejects_html(self):
        assert brand_kit._sniff_image_ext(b"<!DOCTYPE html><html>") is None
        assert brand_kit._sniff_image_ext(b"") is None

    def test_sniff_image_ext_magic_bytes(self):
        assert brand_kit._sniff_image_ext(b"\x89PNG\r\n\x1a\n" + b"0" * 16) == ".png"
        assert brand_kit._sniff_image_ext(b"\xff\xd8\xff\xe0" + b"0" * 16) == ".jpg"
        assert brand_kit._sniff_image_ext(b"GIF89a" + b"0" * 16) == ".gif"
        assert brand_kit._sniff_image_ext(b"RIFF\x00\x00\x00\x00WEBP" + b"0" * 8) == ".webp"
        assert brand_kit._sniff_image_ext(b'<svg xmlns="http://www.w3.org/2000/svg"></svg>') == ".svg"

    def test_logo_candidate_priority(self):
        html = """<html><head>
        <link rel="apple-touch-icon" sizes="180x180" href="/apple.png">
        <link rel="icon" href="/favicon.ico"></head>
        <body><header><img class="site-logo" src="/logo.svg"></header></body></html>"""
        cands = brand_kit.collect_logo_candidates(
            html, "https://x.ir/", ["https://x.ir/schema-logo.png"])
        assert cands[0] == "https://x.ir/schema-logo.png"      # schema.org wins
        assert cands[1] == "https://x.ir/logo.svg"             # header logo beats icons
        assert "https://x.ir/apple.png" in cands
        assert "https://x.ir/favicon.ico" in cands

    def test_css_color_weighting_and_fonts(self):
        html = """<style>
        :root { --brand-primary: #e91e63; --text: #333333; }
        h1 { color: #e91e63; } .x { color: #123456; font-family: 'Vazirmatn', sans-serif; }
        </style>"""
        out = brand_kit.extract_css_colors(html, "https://x.ir/")
        assert out["weighted"]["#e91e63"] > out["weighted"]["#123456"]
        assert "Vazirmatn" in out["fonts"]

    def test_build_brand_kit_never_raises_on_empty(self):
        kit = brand_kit.build_brand_kit("", "https://x.ir/")
        assert kit["logo"] is None
        assert kit["palette"]["primary"] is None


# --------------------------------------------------------------------- extraction cache

class TestExtractionCache(object):
    def test_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gs, "EXTRACTION_CACHE_DIR", str(tmp_path))
        data = {"url": "https://x.ir/about", "extracted_chunk": "hello", "primary_category": "contact"}
        gs.store_cached_extraction("https://x.ir/about", "fa", "hash1", data, "gpt-5-nano")
        assert gs.load_cached_extraction("https://x.ir/about", "fa", "hash1") == data

    def test_miss_on_changed_content_or_lang(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gs, "EXTRACTION_CACHE_DIR", str(tmp_path))
        gs.store_cached_extraction("https://x.ir/a", "fa", "hash1", {"x": 1}, "m")
        assert gs.load_cached_extraction("https://x.ir/a", "fa", "hash2") is None
        assert gs.load_cached_extraction("https://x.ir/a", "en", "hash1") is None
        assert gs.load_cached_extraction("https://x.ir/other", "fa", "hash1") is None
