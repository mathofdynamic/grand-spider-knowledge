"""Deterministic structured-data harvesting: JSON-LD (schema.org), Open Graph, meta tags.

Runs before any LLM call and costs nothing. Sites that publish Organization/FAQPage/
Product markup hand us authoritative company facts (name, logo, address, phones,
socials, opening hours, FAQ pairs) that the KB pipeline can treat as ground truth
instead of hoping the model extracts them from prose.
"""
import json
import logging
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# schema.org types that describe the site owner (Organization and its subtypes).
_ORG_TYPES = {
    'organization', 'localbusiness', 'store', 'onlinestore', 'corporation',
    'company', 'onlinebusiness', 'shop', 'restaurant', 'medicalorganization',
    'educationalorganization', 'ngo', 'professionalservice',
}

_SOCIAL_DOMAINS = (
    'instagram.com', 'facebook.com', 'twitter.com', 'x.com', 'linkedin.com',
    'youtube.com', 't.me', 'telegram.me', 'wa.me', 'whatsapp.com', 'aparat.com',
    'pinterest.com', 'tiktok.com', 'threads.net', 'github.com',
)


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _text(value):
    """Coerce a JSON-LD value (str | {'@value': ...} | list) to a plain string."""
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        value = value.get('@value') or value.get('name') or value.get('url')
    return value.strip() if isinstance(value, str) else None


def _image_url(value):
    """Coerce a JSON-LD image/logo value ('url' | ImageObject | list) to a URL string."""
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        value = value.get('url') or value.get('contentUrl')
    return value.strip() if isinstance(value, str) else None


def _format_address(addr):
    """schema.org PostalAddress -> single display string."""
    if isinstance(addr, str):
        return addr.strip() or None
    if isinstance(addr, list):
        addr = addr[0] if addr else None
    if not isinstance(addr, dict):
        return None
    parts = [_text(addr.get(k)) for k in
             ('streetAddress', 'addressLocality', 'addressRegion', 'postalCode', 'addressCountry')]
    joined = ', '.join(p for p in parts if p)
    return joined or None


def _iter_jsonld_nodes(payload):
    """Yield every node in a JSON-LD payload (handles @graph, lists, nesting one level)."""
    for item in _as_list(payload):
        if not isinstance(item, dict):
            continue
        yield item
        for sub in _as_list(item.get('@graph')):
            if isinstance(sub, dict):
                yield sub


def _node_types(node):
    return {t.lower().rsplit('/', 1)[-1] for t in _as_list(node.get('@type')) if isinstance(t, str)}


def _harvest_org(node, facts):
    org = facts['organization']
    for key, getter in (('name', _text), ('legal_name', _text), ('description', _text),
                        ('telephone', _text), ('email', _text), ('url', _text)):
        src_key = {'legal_name': 'legalName'}.get(key, key)
        val = getter(node.get(src_key))
        if val and not org.get(key):
            org[key] = val
    logo = _image_url(node.get('logo'))
    if logo and logo not in facts['logo_candidates']:
        facts['logo_candidates'].append(logo)
    address = _format_address(node.get('address'))
    if address and address not in org['addresses']:
        org['addresses'].append(address)
    for same_as in _as_list(node.get('sameAs')):
        if isinstance(same_as, str) and same_as not in facts['socials']:
            facts['socials'].append(same_as)
    for hours in _as_list(node.get('openingHours')) + _as_list(node.get('openingHoursSpecification')):
        text = hours if isinstance(hours, str) else None
        if isinstance(hours, dict):
            days = ', '.join(_text(d) or '' for d in _as_list(hours.get('dayOfWeek')))
            opens, closes = _text(hours.get('opens')), _text(hours.get('closes'))
            if opens and closes:
                text = f"{days}: {opens}-{closes}" if days else f"{opens}-{closes}"
        if text and text not in org['opening_hours']:
            org['opening_hours'].append(text)
    for cp in _as_list(node.get('contactPoint')):
        if not isinstance(cp, dict):
            continue
        phone, email = _text(cp.get('telephone')), _text(cp.get('email'))
        if phone and phone not in org['phones']:
            org['phones'].append(phone)
        if email and email not in org['emails']:
            org['emails'].append(email)
    if org.get('telephone') and org['telephone'] not in org['phones']:
        org['phones'].append(org['telephone'])
    if org.get('email') and org['email'] not in org['emails']:
        org['emails'].append(org['email'])


def _harvest_faq(node, facts):
    for q in _as_list(node.get('mainEntity')):
        if not isinstance(q, dict):
            continue
        question = _text(q.get('name'))
        answer_node = q.get('acceptedAnswer')
        if isinstance(answer_node, list):
            answer_node = answer_node[0] if answer_node else None
        answer = _text(answer_node.get('text')) if isinstance(answer_node, dict) else None
        if answer:
            # Answers may legally contain HTML; flatten to text.
            answer = BeautifulSoup(answer, 'html.parser').get_text(' ', strip=True)
        if question and answer and not any(f['question'] == question for f in facts['faq']):
            facts['faq'].append({'question': question, 'answer': answer})


def _harvest_product(node, facts):
    name = _text(node.get('name'))
    if name and name not in facts['product_names'] and len(facts['product_names']) < 50:
        facts['product_names'].append(name)


def _empty_facts():
    return {
        'organization': {'addresses': [], 'phones': [], 'emails': [], 'opening_hours': []},
        'faq': [],
        'product_names': [],
        'socials': [],
        'logo_candidates': [],
        'open_graph': {},
        'theme_color': None,
        'breadcrumbs': [],
    }


def extract_structured_data(html: str, page_url: str) -> dict:
    """Harvest JSON-LD + Open Graph + meta facts from one page. Never raises."""
    facts = _empty_facts()
    if not html:
        return facts
    try:
        soup = BeautifulSoup(html, 'html.parser')
    except Exception as e:
        logger.debug(f"structured-data parse failed for {page_url}: {e}")
        return facts

    # --- JSON-LD ---
    for script in soup.find_all('script', type=re.compile(r'application/ld\+json', re.I)):
        raw = script.string or script.get_text() or ''
        if not raw.strip():
            continue
        try:
            payload = json.loads(raw, strict=False)
        except (json.JSONDecodeError, ValueError):
            continue
        for node in _iter_jsonld_nodes(payload):
            types = _node_types(node)
            if types & _ORG_TYPES:
                _harvest_org(node, facts)
            if 'faqpage' in types:
                _harvest_faq(node, facts)
            if 'product' in types:
                _harvest_product(node, facts)
            if 'website' in types and not facts['organization'].get('name'):
                name = _text(node.get('name'))
                if name:
                    facts['organization']['name'] = name
            if 'breadcrumblist' in types:
                trail = []
                for el in _as_list(node.get('itemListElement')):
                    if isinstance(el, dict):
                        label = _text(el.get('name')) or _text(
                            el.get('item', {}).get('name') if isinstance(el.get('item'), dict) else None)
                        if label:
                            trail.append(label)
                if trail and trail not in facts['breadcrumbs']:
                    facts['breadcrumbs'].append(trail)

    # --- Open Graph / Twitter / theme-color ---
    for meta in soup.find_all('meta'):
        prop = (meta.get('property') or meta.get('name') or '').lower()
        content = (meta.get('content') or '').strip()
        if not prop or not content:
            continue
        if prop in ('og:site_name', 'og:title', 'og:description', 'og:image', 'og:url',
                    'og:type', 'twitter:site', 'twitter:title', 'twitter:description'):
            facts['open_graph'].setdefault(prop, content)
        elif prop == 'theme-color' and not facts['theme_color']:
            facts['theme_color'] = content

    og_image = facts['open_graph'].get('og:image')
    if og_image:
        og_abs = urljoin(page_url, og_image)
        if og_abs not in facts['logo_candidates']:
            facts['logo_candidates'].append(og_abs)

    # --- Social links from anchors (footer/header nav) ---
    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        if not href.lower().startswith(('http://', 'https://')):
            continue
        if any(d in href.lower() for d in _SOCIAL_DOMAINS):
            clean = href.split('?')[0].rstrip('/')
            if clean and clean not in facts['socials'] and len(facts['socials']) < 30:
                facts['socials'].append(clean)

    return facts


def merge_structured_facts(per_page_facts: list) -> dict:
    """Merge many per-page harvests into one site-level facts dict (first-writer wins
    for scalars, union for lists)."""
    merged = _empty_facts()
    for facts in per_page_facts:
        if not isinstance(facts, dict):
            continue
        org, m_org = facts.get('organization') or {}, merged['organization']
        for key, val in org.items():
            if isinstance(val, list):
                for v in val:
                    if v not in m_org.setdefault(key, []):
                        m_org[key].append(v)
            elif val and not m_org.get(key):
                m_org[key] = val
        for key in ('faq', 'product_names', 'socials', 'logo_candidates', 'breadcrumbs'):
            for item in facts.get(key) or []:
                if item not in merged[key]:
                    merged[key].append(item)
        for k, v in (facts.get('open_graph') or {}).items():
            merged['open_graph'].setdefault(k, v)
        if not merged['theme_color'] and facts.get('theme_color'):
            merged['theme_color'] = facts['theme_color']
    merged['faq'] = merged['faq'][:60]
    merged['product_names'] = merged['product_names'][:50]
    return merged


def structured_facts_summary_text(facts: dict) -> str:
    """Human/LLM-readable digest of harvested facts, for prompts and completeness checks."""
    if not facts:
        return ""
    lines = []
    org = facts.get('organization') or {}
    if org.get('name'):
        lines.append(f"Organization name: {org['name']}")
    if org.get('description'):
        lines.append(f"Description: {org['description'][:300]}")
    if org.get('phones'):
        lines.append("Phones: " + ", ".join(org['phones'][:10]))
    if org.get('emails'):
        lines.append("Emails: " + ", ".join(org['emails'][:10]))
    if org.get('addresses'):
        lines.append("Addresses: " + " | ".join(org['addresses'][:5]))
    if org.get('opening_hours'):
        lines.append("Opening hours: " + " | ".join(org['opening_hours'][:7]))
    if facts.get('socials'):
        lines.append("Social links: " + ", ".join(facts['socials'][:10]))
    if facts.get('faq'):
        lines.append(f"Structured FAQ entries found: {len(facts['faq'])}")
    return "\n".join(lines)
