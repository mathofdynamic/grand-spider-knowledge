"""Deterministic brand-asset extraction: logo, true color palette, fonts, social links.

Measures the real site instead of asking an LLM to guess: the logo comes from
schema.org / icon links / header <img> heuristics, colors from the site's own CSS
variables and the screenshot's pixels (Pillow quantization), fonts from CSS
font-family declarations and Google Fonts links. Free, fast, and reproducible.
"""
import base64
import io
import logging
import os
import re
from collections import Counter
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

logger = logging.getLogger(__name__)

MAX_LOGO_BYTES = 5 * 1024 * 1024
MAX_STYLESHEETS = 4
MAX_STYLESHEET_BYTES = 400 * 1024

_HEX_RE = re.compile(r'#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b')
_RGB_RE = re.compile(r'rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})')
_FONT_FAMILY_RE = re.compile(r'font-family\s*:\s*([^;}{]+)', re.I)
# CSS custom properties whose name suggests a brand color get extra weight.
_BRAND_VAR_RE = re.compile(
    r'--[\w-]*(?:primary|brand|accent|main|theme)[\w-]*\s*:\s*([^;}{]+)', re.I)

_GENERIC_FONTS = {'serif', 'sans-serif', 'monospace', 'cursive', 'fantasy', 'system-ui',
                  'inherit', 'initial', 'unset', 'ui-sans-serif', 'ui-serif', 'ui-monospace',
                  '-apple-system', 'blinkmacsystemfont'}

_IMAGE_CONTENT_TYPES = {
    'image/png': '.png', 'image/jpeg': '.jpg', 'image/jpg': '.jpg', 'image/gif': '.gif',
    'image/webp': '.webp', 'image/svg+xml': '.svg', 'image/x-icon': '.ico',
    'image/vnd.microsoft.icon': '.ico', 'image/avif': '.avif',
}


# --------------------------------------------------------------------------- colors

def normalize_hex(value: str) -> str | None:
    """'#ABC' / '#aabbcc' / 'rgb(1,2,3)' -> '#aabbcc' (lowercase), else None."""
    if not value:
        return None
    value = value.strip().lower()
    m = _RGB_RE.match(value)
    if m:
        r, g, b = (min(255, int(x)) for x in m.groups())
        return f'#{r:02x}{g:02x}{b:02x}'
    if _HEX_RE.match(value):
        h = value.lstrip('#')
        if len(h) == 3:
            h = ''.join(c * 2 for c in h)
        return f'#{h}'
    return None


def _rgb_of(hex_color: str):
    h = hex_color.lstrip('#')
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def is_near_neutral(hex_color: str) -> bool:
    """True for whites/blacks/greys — backgrounds, not brand colors."""
    try:
        r, g, b = _rgb_of(hex_color)
    except (ValueError, IndexError):
        return True
    mx, mn = max(r, g, b), min(r, g, b)
    if mx > 235 and mn > 215:   # near-white
        return True
    if mx < 40:                 # near-black
        return True
    saturation = 0 if mx == 0 else (mx - mn) / mx
    return saturation < 0.18    # grey-ish


def extract_css_colors(html: str, base_url: str, session=None, url_validator=None) -> dict:
    """Harvest colors from inline <style> blocks and same-domain stylesheets.

    Returns {'weighted': Counter(hex -> weight), 'fonts': [family, ...]}.
    Brand-suggestive CSS variables (--primary, --brand-...) weigh far more than a
    color merely appearing somewhere in a stylesheet.
    """
    weighted = Counter()
    fonts = []
    if not html:
        return {'weighted': weighted, 'fonts': fonts}
    session = session or requests
    try:
        soup = BeautifulSoup(html, 'html.parser')
    except Exception:
        return {'weighted': weighted, 'fonts': fonts}

    css_texts = [style.get_text() or '' for style in soup.find_all('style')]

    base_domain = urlparse(base_url).netloc
    fetched = 0
    for link in soup.find_all('link', rel=lambda v: v and 'stylesheet' in v):
        href = link.get('href')
        if not href or fetched >= MAX_STYLESHEETS:
            continue
        css_url = urljoin(base_url, href)
        if urlparse(css_url).netloc != base_domain:
            # Google Fonts links still tell us the typography.
            if 'fonts.googleapis.com' in css_url:
                for family in parse_qs(urlparse(css_url).query).get('family', []):
                    name = family.split(':')[0].replace('+', ' ').strip()
                    if name and name not in fonts:
                        fonts.append(name)
            continue
        if url_validator and url_validator(css_url):
            continue
        try:
            resp = session.get(css_url, timeout=15)
            if resp.status_code == 200:
                css_texts.append(resp.text[:MAX_STYLESHEET_BYTES])
                fetched += 1
        except requests.exceptions.RequestException:
            continue

    for css in css_texts:
        for m in _BRAND_VAR_RE.finditer(css):
            color = normalize_hex(m.group(1))
            if color and not is_near_neutral(color):
                weighted[color] += 50
        for raw in _HEX_RE.findall(css):
            color = normalize_hex(raw)
            if color:
                weighted[color] += 1
        for m in _FONT_FAMILY_RE.finditer(css):
            first = m.group(1).split(',')[0].strip().strip('\'"').strip()
            if first and first.lower() not in _GENERIC_FONTS and first not in fonts \
                    and not first.startswith('var('):
                fonts.append(first)

    return {'weighted': weighted, 'fonts': fonts[:12]}


def palette_from_screenshot(screenshot_b64: str, max_colors: int = 6) -> dict:
    """Quantize the page screenshot into its dominant colors.

    Returns {'background': hex|None, 'colors': [{'hex', 'share'}, ...]} where 'colors'
    excludes near-neutral backgrounds and 'share' is the fraction of sampled pixels.
    """
    if not (screenshot_b64 and PIL_AVAILABLE):
        return {'background': None, 'colors': []}
    try:
        img = Image.open(io.BytesIO(base64.b64decode(screenshot_b64))).convert('RGB')
        img.thumbnail((240, 2400))
        quantized = img.quantize(colors=24)
        palette = quantized.getpalette()
        counts = sorted(quantized.getcolors(maxcolors=256) or [], reverse=True)
        total = sum(c for c, _ in counts) or 1
        background, colors = None, []
        for count, idx in counts:
            r, g, b = palette[idx * 3: idx * 3 + 3]
            hex_color = f'#{r:02x}{g:02x}{b:02x}'
            if is_near_neutral(hex_color):
                if background is None:
                    background = hex_color
                continue
            if len(colors) < max_colors:
                colors.append({'hex': hex_color, 'share': round(count / total, 4)})
        return {'background': background, 'colors': colors}
    except Exception as e:
        logger.warning(f"Screenshot palette extraction failed: {e}")
        return {'background': None, 'colors': []}


# --------------------------------------------------------------------------- logo

def collect_logo_candidates(html: str, base_url: str, structured_candidates=None) -> list:
    """Ordered (best-first) list of absolute logo-image URL candidates."""
    ordered = []

    def _add(url):
        if url:
            absolute = urljoin(base_url, url.strip())
            if absolute.lower().startswith(('http://', 'https://')) and absolute not in ordered:
                ordered.append(absolute)

    def _rel_contains(rel_value, needle):
        if not rel_value:
            return False
        text = ' '.join(rel_value) if isinstance(rel_value, list) else str(rel_value)
        return needle in text.lower()

    # 1) schema.org Organization.logo (arrives via structured_data harvest) is authoritative.
    for candidate in (structured_candidates or []):
        _add(candidate)
    structured_count = len(ordered)

    if html:
        try:
            soup = BeautifulSoup(html, 'html.parser')
        except Exception:
            return ordered
        # 2) apple-touch-icon, largest first.
        touch_icons = []
        for link in soup.find_all('link', rel=lambda v: _rel_contains(v, 'apple-touch-icon')):
            size = 0
            sizes_attr = (link.get('sizes') or '').split('x')[0]
            if sizes_attr.isdigit():
                size = int(sizes_attr)
            touch_icons.append((size, link.get('href') or ''))
        for _, href in sorted(touch_icons, reverse=True):
            _add(href)
        # 3) header/nav <img> whose src/alt/class/id mentions "logo".
        for img in soup.find_all('img'):
            blob = ' '.join([str(img.get('src') or ''), str(img.get('alt') or ''),
                             str(img.get('class') or ''), str(img.get('id') or '')]).lower()
            if 'logo' in blob:
                in_header = any(parent.name in ('header', 'nav') for parent in img.parents
                                if getattr(parent, 'name', None))
                target = img.get('src') or img.get('data-src')
                if in_header:
                    before = len(ordered)
                    _add(target)
                    # Header logos beat touch icons/favicons but not schema.org logos.
                    if len(ordered) > before:
                        ordered.insert(structured_count, ordered.pop())
                else:
                    _add(target)
        # 4) plain favicons as last resort.
        for link in soup.find_all('link', rel=lambda v: _rel_contains(v, 'icon')):
            _add(link.get('href'))

    return ordered[:10]


def _sniff_image_ext(content: bytes) -> str | None:
    """Identify an image by magic bytes; never trust the URL extension or the
    Content-Type header (SPA servers happily return HTML with 200 for missing icons)."""
    if not content:
        return None
    if content.startswith(b'\x89PNG\r\n\x1a\n'):
        return '.png'
    if content.startswith(b'\xff\xd8\xff'):
        return '.jpg'
    if content.startswith((b'GIF87a', b'GIF89a')):
        return '.gif'
    if content.startswith(b'RIFF') and content[8:12] == b'WEBP':
        return '.webp'
    if content.startswith(b'\x00\x00\x01\x00'):
        return '.ico'
    if len(content) > 12 and content[4:12] in (b'ftypavif', b'ftypavis'):
        return '.avif'
    head = content[:2048].lstrip()
    if head.startswith((b'<svg', b'<?xml')) and b'<svg' in content[:4096]:
        return '.svg'
    return None


def download_logo(candidates: list, domain_slug: str, reports_dir: str,
                  session=None, url_validator=None) -> dict | None:
    """Download the first candidate that is genuinely an image. Returns metadata or None."""
    session = session or requests
    os.makedirs(reports_dir, exist_ok=True)
    for url in candidates:
        if url_validator and url_validator(url):
            continue
        try:
            resp = session.get(url, timeout=15)
            if resp.status_code != 200 or not resp.content:
                continue
            if len(resp.content) > MAX_LOGO_BYTES:
                continue
            ext = _sniff_image_ext(resp.content)
            if not ext:
                continue
            width = height = None
            if ext != '.svg':
                if PIL_AVAILABLE:
                    try:
                        with Image.open(io.BytesIO(resp.content)) as img:
                            img.verify()
                        with Image.open(io.BytesIO(resp.content)) as img:
                            width, height = img.size
                    except Exception:
                        continue  # corrupt/undecodable — try the next candidate
            filename = f"brand_{domain_slug}_logo{ext}"
            filepath = os.path.join(reports_dir, filename)
            with open(filepath, 'wb') as f:
                f.write(resp.content)
            content_type = (resp.headers.get('Content-Type') or '').split(';')[0].strip().lower()
            logger.info(f"Logo saved: {filepath} (from {url})")
            return {'source_url': url, 'file': filepath, 'content_type': content_type,
                    'width': width, 'height': height, 'bytes': len(resp.content)}
        except requests.exceptions.RequestException:
            continue
    return None


# --------------------------------------------------------------------------- assembly

def build_brand_kit(html: str, base_url: str, screenshot_b64: str = None,
                    structured_facts: dict = None, session=None, url_validator=None,
                    reports_dir: str = "reports") -> dict:
    """Assemble the full deterministic brand kit for a site. Never raises."""
    structured_facts = structured_facts or {}
    domain_slug = urlparse(base_url).netloc.replace('www.', '').replace('.', '_') or 'site'

    css = extract_css_colors(html, base_url, session, url_validator)
    screen = palette_from_screenshot(screenshot_b64)

    theme_color = normalize_hex(structured_facts.get('theme_color') or '')

    # Primary: theme-color meta > weighted CSS brand color > dominant screenshot color.
    ranked = []
    if theme_color and not is_near_neutral(theme_color):
        ranked.append(theme_color)
    for color, _ in css['weighted'].most_common(20):
        if not is_near_neutral(color) and color not in ranked:
            ranked.append(color)
    for entry in screen['colors']:
        if entry['hex'] not in ranked:
            ranked.append(entry['hex'])

    logo = None
    try:
        candidates = collect_logo_candidates(html, base_url,
                                             structured_facts.get('logo_candidates'))
        logo = download_logo(candidates, domain_slug, reports_dir, session, url_validator)
    except Exception as e:
        logger.warning(f"Logo extraction failed for {base_url}: {e}")

    return {
        'logo': logo,
        'palette': {
            'primary': ranked[0] if ranked else None,
            'secondary': ranked[1] if len(ranked) > 1 else None,
            'accent': ranked[2] if len(ranked) > 2 else None,
            'background': screen['background'],
            'theme_color': theme_color,
            'screenshot_colors': screen['colors'],
            'css_colors_top': [c for c, _ in css['weighted'].most_common(10)],
        },
        'fonts': css['fonts'],
        'social_links': (structured_facts.get('socials') or [])[:15],
        'sources': {
            'theme_color_meta': bool(theme_color),
            'css_sheets_scanned': True,
            'screenshot_quantized': bool(screen['colors'] or screen['background']),
            'logo_found': bool(logo),
        },
    }
