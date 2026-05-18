import argparse
import arrow
import hashlib
import html
import io
import json
import logging
import re
import httpx
import time
import os
import subprocess
import uuid
import random
from urllib.parse import urlparse
from dotenv import load_dotenv
from atproto import Client, client_utils, models
from playwright.sync_api import sync_playwright
from moviepy import VideoFileClip
from bs4 import BeautifulSoup
from PIL import Image
import grapheme

# --- Configuration ---
LOG_PATH = "twitter2bsky.log"
STATE_PATH = "twitter2bsky_state.json"
SCRAPE_TWEET_LIMIT = 30
DEDUPE_BSKY_LIMIT = 30
TWEET_MAX_AGE_DAYS = 3
BSKY_TEXT_MAX_LENGTH = 300
DEFAULT_BSKY_LANGS = ["ca"]

VIDEO_MAX_DURATION_SECONDS = 179
MAX_VIDEO_UPLOAD_SIZE_MB = 45

BSKY_IMAGE_MAX_BYTES = 950 * 1024
BSKY_IMAGE_MAX_DIMENSION = 2000
BSKY_IMAGE_MIN_JPEG_QUALITY = 45

EXTERNAL_THUMB_MAX_BYTES = 950 * 1024
EXTERNAL_THUMB_MAX_DIMENSION = 1200
EXTERNAL_THUMB_MIN_JPEG_QUALITY = 40

BSKY_BLOB_UPLOAD_MAX_RETRIES = 5
BSKY_BLOB_UPLOAD_BASE_DELAY = 10
BSKY_BLOB_UPLOAD_MAX_DELAY = 300
BSKY_BLOB_TRANSIENT_ERROR_RETRIES = 3
BSKY_BLOB_TRANSIENT_ERROR_DELAY = 15

BSKY_SEND_POST_MAX_RETRIES = 3
BSKY_SEND_POST_BASE_DELAY = 5
BSKY_SEND_POST_MAX_DELAY = 60

BSKY_LOGIN_MAX_RETRIES = 4
BSKY_LOGIN_BASE_DELAY = 10
BSKY_LOGIN_MAX_DELAY = 600
BSKY_LOGIN_JITTER_MAX = 1.5

MEDIA_DOWNLOAD_TIMEOUT = 30
LINK_METADATA_TIMEOUT = 10
URL_RESOLVE_TIMEOUT = 12
PLAYWRIGHT_RESOLVE_TIMEOUT_MS = 60000
SUBPROCESS_TIMEOUT_SECONDS = 180
FFPROBE_TIMEOUT_SECONDS = 15
DEFAULT_BSKY_BASE_URL = "https://bsky.social"

OG_TITLE_WAIT_TIMEOUT_MS = 7000
PLAYWRIGHT_POST_GOTO_SLEEP_S = 2.0
PLAYWRIGHT_IDLE_POLL_SLEEP_S = 0.8
PLAYWRIGHT_IDLE_POLL_ROUNDS = 4
PLAYWRIGHT_RETRY_SLEEP_S = 2.0
VIDEO_PLAYER_WAIT_ROUNDS = 8
VIDEO_PLAYER_RETRY_ROUNDS = 5
URL_TAIL_MIN_PREFIX_CHARS = 35
URL_TAIL_MAX_LOOKBACK_CHARS = 120
URL_TAIL_MAX_CLAUSE_DISTANCE = 180
DYNAMIC_ALT_MAX_LENGTH = 150
TRUNCATE_MIN_PREFIX_CHARS = 20
SHORT_TWEET_OG_FETCH_THRESHOLD = 35
ORPHAN_DIGIT_MAX_DIGITS = 3
SESSION_FILE_PERMISSIONS = 0o600

# --- Logging Setup ---
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(),
    ],
    level=logging.INFO,
)


# --- Per-run caches ---
class _RunCache:
    def __init__(self):
        self.og_title: dict = {}
        self.url_resolution: dict = {}
        self.url_validity: dict = {}
        self.locale: str = "en-US"
        self.video_hash_owner: dict = {}
        self.video_url_owner: dict = {}

    def clear(self):
        self.og_title.clear()
        self.url_resolution.clear()
        self.url_validity.clear()
        self.video_hash_owner.clear()
        self.video_url_owner.clear()


_cache = _RunCache()


def reset_caches():
    _cache.clear()


# === VIDEO BINDING PATCH APPLIED ===
def sha256_bytes(data: bytes):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path, chunk_size=1024 * 1024):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def media_url_looks_audio_only(url):
    u = (url or "").lower()
    return "/aud/" in u or "/audio/" in u or "mp4a" in u


def grapheme_len(text):
    """Return the grapheme cluster count, matching Bluesky's character counting."""
    return grapheme.length(text)


# BCP-47 language tag → sensible locale for Playwright
_LANG_TO_LOCALE = {
    "ca": "ca-ES",
    "es": "es-ES",
    "en": "en-US",
    "fr": "fr-FR",
    "de": "de-DE",
    "pt": "pt-PT",
    "it": "it-IT",
    "nl": "nl-NL",
    "eu": "eu-ES",
    "gl": "gl-ES",
}


def bsky_langs_to_playwright_locale(bsky_langs):
    """
    Convert the first configured Bluesky language tag to a Playwright locale
    string (e.g. ['ca'] → 'ca-ES'). Falls back to 'en-US' if unknown.
    """
    if not bsky_langs:
        return "en-US"
    primary = bsky_langs[0].strip().lower()
    return _LANG_TO_LOCALE.get(primary, f"{primary}-{primary.upper()}")


# --- Custom Classes ---
class ScrapedMedia:
    def __init__(self, url, media_type="photo"):
        self.type = media_type
        self.media_url_https = url


class ScrapedTweet:
    def __init__(self, created_on, text, media_urls, tweet_url=None, card_url=None, is_retweet=False):
        self.created_on = created_on
        self.text = text
        self.tweet_url = tweet_url
        self.card_url = card_url
        self.is_retweet = is_retweet
        self.media = [ScrapedMedia(url, media_type) for url, media_type in media_urls]


# --- Helpers ---
def take_error_screenshot(page, error_msg):
    logging.info(f"📸 Taking screenshot... Shot: {error_msg}")
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    screenshot_name = f"screenshot_{timestamp}.png"
    page.screenshot(path=screenshot_name)
    logging.info(f"📸 Screenshot saved as: {screenshot_name}")


def is_valid_url(url):
    if url in _cache.url_validity:
        return _cache.url_validity[url]

    try:
        response = httpx.head(url, timeout=5, follow_redirects=True)
        result = response.status_code < 500
    except Exception:
        result = False

    _cache.url_validity[url] = result
    return result


def strip_trailing_url_punctuation(url):
    if not url:
        return url
    url = re.sub(r"#[A-Za-z]\w*$", "", url.strip())
    return re.sub(r"[\s…\.,;:!?)\]\"\']+$", "", url)


def split_url_hashtag_suffix(text):
    """
    Split a URL that has a hashtag fragment glued to it with no space, e.g.:
        'https://cit.transit.gencat.cat#SCT'
    becomes:
        'https://cit.transit.gencat.cat #SCT'

    Only splits when the fragment looks like a social hashtag: starts with #
    followed by a letter then word characters. The lookahead (?=\\s|$) ensures
    we only act at a word boundary so mid-sentence anchors followed by more
    URL path are left untouched.
    """
    if not text:
        return text

    fixed = re.sub(
        r"(https?://[^\s#<>\"']+)(#[A-Za-z]\w*)(?=\s|$)",
        r"\1 \2",
        text,
    )
    if fixed != text:
        logging.info("🔧 Split hashtag suffix from URL in text")
    return fixed


def split_concatenated_urls(text):
    if not text:
        return text

    fixed = re.sub(r"(https?://[^\s]+?)(https?://)", r"\1 \2", text)
    if fixed != text:
        logging.info("🔧 Split concatenated URLs in text")
    return fixed


def repair_broken_urls(text):
    if not text:
        return text

    original = text
    text = split_concatenated_urls(text)
    text = split_url_hashtag_suffix(text)
    text = re.sub(r"(https?://)\s*[\r\n]+\s*", r"\1", text, flags=re.IGNORECASE)

    prev_text = None
    while prev_text != text:
        prev_text = text
        text = re.sub(
            r"((?:https?://|www\.)[^\s<>\"]*?)[\r\n]+([A-Za-z0-9/\-._~%!$&'()*+,;=:@?#]+)",
            r"\1\2",
            text,
            flags=re.IGNORECASE,
        )

    text = re.sub(
        r"((?:https?://|www\.)[^\s<>\"]*?)\s+([A-Za-z0-9/\-._~%!$&'()*+,;=:@?#]+)",
        r"\1\2",
        text,
        flags=re.IGNORECASE,
    )

    text = split_concatenated_urls(text)
    text = split_url_hashtag_suffix(text)

    if text != original:
        logging.info("🔧 Repaired broken URL wrapping in scraped text")

    return text


def repair_broken_mentions(text):
    if not text:
        return text

    lines = text.splitlines()
    result = []
    i = 0
    changed = False

    def is_mention_only_line(s):
        return bool(re.fullmatch(r"@[A-Za-z0-9_]+", s.strip()))

    def is_blank_line(s):
        return not s.strip()

    while i < len(lines):
        current = lines[i]
        stripped = current.strip()

        if is_blank_line(current):
            result.append("")
            i += 1
            continue

        if is_mention_only_line(current):
            if result and result[-1].strip():
                result[-1] = result[-1].rstrip() + " " + stripped
                changed = True
            else:
                result.append(stripped)

            i += 1

            while i < len(lines):
                next_line = lines[i]
                next_stripped = next_line.strip()

                if is_blank_line(next_line):
                    break
                if is_mention_only_line(next_line):
                    break

                result[-1] = result[-1].rstrip() + " " + next_stripped
                changed = True
                i += 1

                if i < len(lines) and is_blank_line(lines[i]):
                    break

            continue

        if i + 1 < len(lines) and is_mention_only_line(lines[i + 1]):
            merged = stripped + " " + lines[i + 1].strip()
            changed = True
            i += 2

            while i < len(lines):
                next_line = lines[i]
                next_stripped = next_line.strip()

                if is_blank_line(next_line):
                    break
                if is_mention_only_line(next_line):
                    break

                merged = merged.rstrip() + " " + next_stripped
                changed = True
                i += 1

                if i < len(lines) and is_blank_line(lines[i]):
                    break

            result.append(merged)
            continue

        result.append(stripped)
        i += 1

    new_text = "\n".join(result)

    if changed:
        logging.info("🔧 Repaired broken mention wrapping in scraped text")

    return new_text


def strip_line_edge_whitespace(text):
    if not text:
        return text

    lines = text.splitlines()
    cleaned_lines = []
    changed = False

    for line in lines:
        cleaned = line.strip()
        if cleaned != line:
            changed = True
        cleaned_lines.append(cleaned)

    new_text = "\n".join(cleaned_lines)

    if changed:
        logging.info("🔧 Stripped leading/trailing whitespace from scraped text lines")

    return new_text


def remove_trailing_ellipsis_line(text):
    if not text:
        return text

    lines = text.splitlines()

    while lines and lines[-1].strip() in {"...", "…"}:
        lines.pop()

    return "\n".join(lines).strip()


def remove_orphaned_digit_lines_before_hashtags(text):
    if not text:
        return text

    lines = text.splitlines()
    if len(lines) < 2:
        return text

    result = []
    changed = False
    i = 0
    orphan_pattern = re.compile(rf"\d{{1,{ORPHAN_DIGIT_MAX_DIGITS}}}")

    while i < len(lines):
        stripped = lines[i].strip()

        if (
            stripped
            and orphan_pattern.fullmatch(stripped)
            and i + 1 < len(lines)
            and lines[i + 1].strip().startswith("#")
        ):
            logging.info(f"🔧 Removing orphaned digit line '{stripped}' before hashtag line")
            changed = True
            i += 1
            continue

        result.append(lines[i])
        i += 1

    if changed:
        return "\n".join(result)

    return text


def clean_post_text(text):
    raw_text = (text or "").strip()
    raw_text = repair_broken_urls(raw_text)
    raw_text = repair_broken_mentions(raw_text)
    raw_text = strip_line_edge_whitespace(raw_text)
    raw_text = remove_trailing_ellipsis_line(raw_text)
    raw_text = remove_orphaned_digit_lines_before_hashtags(raw_text)
    return raw_text.strip()


def clean_url(url):
    trimmed_url = url.strip()
    cleaned_url = re.sub(r"\s+", "", trimmed_url)
    cleaned_url = strip_trailing_url_punctuation(cleaned_url)

    if is_valid_url(cleaned_url):
        return cleaned_url
    return None


def canonicalize_url(url):
    if not url:
        return None
    return strip_trailing_url_punctuation(url.strip())


def normalize_urlish_token(token):
    if not token:
        return None

    token = strip_trailing_url_punctuation(token.strip())
    if not token:
        return None

    if token.startswith(("http://", "https://")):
        return token

    if token.startswith("www."):
        return f"https://{token}"

    return None


def canonicalize_tweet_url(url):
    if not url:
        return None

    url = url.strip()
    match = re.search(
        r"https?://(?:www\.)?(?:x\.com|twitter\.com)/([^/]+)/status/(\d+)",
        url,
        re.IGNORECASE,
    )
    if not match:
        return url.lower()

    handle = match.group(1).lower()
    tweet_id = match.group(2)
    return f"https://x.com/{handle}/status/{tweet_id}"


def extract_tweet_id(tweet_url):
    if not tweet_url:
        return None
    match = re.search(r"/status/(\d+)", tweet_url)
    if match:
        return match.group(1)
    return None


def make_unique_video_temp_base(tweet_url=None):
    tweet_id = extract_tweet_id(tweet_url) or "unknown"
    ts_ms = int(time.time() * 1000)
    rand = uuid.uuid4().hex[:8]
    base = f"temp_video_{tweet_id}_{ts_ms}_{rand}"
    logging.info(f"🎞️ Using unique temp video base: {base}")
    return base


def remove_file_quietly(path):
    if path and os.path.exists(path):
        try:
            os.remove(path)
            logging.info(f"🧹 Removed temp file: {path}")
        except Exception as e:
            logging.warning(f"⚠️ Could not remove temp file {path}: {e}")


def is_x_or_twitter_domain(url):
    try:
        normalized = normalize_urlish_token(url) or url
        hostname = (urlparse(normalized).hostname or "").lower()
        return hostname in {
            "x.com",
            "www.x.com",
            "twitter.com",
            "www.twitter.com",
            "mobile.twitter.com",
        }
    except Exception:
        return False


def is_tco_domain(url):
    try:
        normalized = normalize_urlish_token(url) or url
        hostname = (urlparse(normalized).hostname or "").lower()
        return hostname == "t.co"
    except Exception:
        return False


def is_external_non_x_url(url):
    if not url:
        return False
    return (not is_tco_domain(url)) and (not is_x_or_twitter_domain(url))


def extract_urls_from_text(text):
    if not text:
        return []

    repaired = repair_broken_urls(text)
    pattern = r'(?:(?:https?://)|(?:www\.))[^\s<>"\']+'
    return re.findall(pattern, repaired)


def extract_quoted_text_from_og_title(og_title):
    if not og_title:
        return None

    decoded = html.unescape(og_title).strip()
    match = re.search(r'on X:\s*"(?P<text>.*)"\s*/\s*X\s*$', decoded, flags=re.DOTALL)
    if match:
        extracted = match.group("text").strip()
        if extracted:
            return extracted

    first_quote = decoded.find('"')
    last_quote = decoded.rfind('"')
    if 0 <= first_quote < last_quote:
        extracted = decoded[first_quote + 1:last_quote].strip()
        if extracted:
            return extracted

    return None


def should_fetch_og_title(tweet):
    text = clean_post_text(tweet.text or "")
    urls = extract_urls_from_text(text)

    if not text:
        return True

    if any(is_tco_domain(normalize_urlish_token(u) or u) for u in urls):
        return True

    if "…" in text or text.endswith("..."):
        return True

    if len(text) < SHORT_TWEET_OG_FETCH_THRESHOLD:
        return True

    return False


def fetch_tweet_og_title_text(tweet_url, locale="en-US"):
    if not tweet_url:
        return None

    if tweet_url in _cache.og_title:
        logging.info(f"⚡ Using cached og:title text for {tweet_url}")
        return _cache.og_title[tweet_url]

    browser = None
    browser_context = None
    page = None

    try:
        logging.info(f"🧾 Fetching og:title from tweet page: {tweet_url}")

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            browser_context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/145.0.7632.6 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 900},
                locale=_cache.locale,
            )
            page = browser_context.new_page()
            page.goto(
                tweet_url,
                wait_until="domcontentloaded",
                timeout=PLAYWRIGHT_RESOLVE_TIMEOUT_MS,
            )

            try:
                page.wait_for_selector(
                    'meta[property="og:title"]',
                    timeout=OG_TITLE_WAIT_TIMEOUT_MS,
                )
            except Exception:
                pass

            og_title = (
                page.locator('meta[property="og:title"]')
                .first.get_attribute("content")
            )
            extracted = extract_quoted_text_from_og_title(og_title)

            if extracted:
                extracted = clean_post_text(extracted)
                _cache.og_title[tweet_url] = extracted
                logging.info(f"✅ Extracted tweet text from og:title for {tweet_url}")
                return extracted

            logging.info(f"ℹ️ No usable og:title text extracted for {tweet_url}")
            _cache.og_title[tweet_url] = None
            return None

    except Exception as e:
        logging.warning(f"⚠️ Could not extract og:title text from {tweet_url}: {repr(e)}")
        try:
            if page:
                take_error_screenshot(page, "tweet_og_title_failed")
        except Exception:
            pass
        _cache.og_title[tweet_url] = None
        return None
    finally:
        try:
            if page:
                page.close()
        except Exception:
            pass
        try:
            if browser_context:
                browser_context.close()
        except Exception:
            pass
        try:
            if browser:
                browser.close()
        except Exception:
            pass


def resolve_tco_with_httpx(url, http_client):
    try:
        response = http_client.get(url, timeout=URL_RESOLVE_TIMEOUT, follow_redirects=True)
        final_url = canonicalize_url(str(response.url))
        if final_url:
            logging.info(f"🔗 Resolved t.co with httpx: {url} -> {final_url}")
            return final_url
    except Exception as e:
        logging.warning(f"⚠️ httpx t.co resolution failed for {url}: {repr(e)}")

    return canonicalize_url(url)


def resolve_tco_with_playwright(url, locale="en-US"):
    browser = None
    browser_context = None
    page = None

    try:
        logging.info(f"🌐 Resolving t.co with Playwright: {url}")

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            browser_context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/145.0.7632.6 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 900},
                locale=locale,
            )
            page = browser_context.new_page()

            try:
                page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=PLAYWRIGHT_RESOLVE_TIMEOUT_MS,
                )
            except Exception as e:
                logging.warning(f"⚠️ Initial Playwright goto failed for {url}: {repr(e)}")

            time.sleep(PLAYWRIGHT_POST_GOTO_SLEEP_S)
            final_url = canonicalize_url(page.url)

            for _ in range(PLAYWRIGHT_IDLE_POLL_ROUNDS):
                if final_url and is_external_non_x_url(final_url):
                    break

                try:
                    page.wait_for_load_state("networkidle", timeout=2000)
                except Exception:
                    pass

                time.sleep(PLAYWRIGHT_IDLE_POLL_SLEEP_S)
                final_url = canonicalize_url(page.url)

            logging.info(f"🌐 Playwright final URL for {url}: {final_url}")
            return final_url

    except Exception as e:
        logging.warning(f"⚠️ Playwright t.co resolution failed for {url}: {repr(e)}")
        try:
            if page:
                take_error_screenshot(page, "tco_resolve_failed")
        except Exception:
            pass
    finally:
        try:
            if page:
                page.close()
        except Exception:
            pass
        try:
            if browser_context:
                browser_context.close()
        except Exception:
            pass
        try:
            if browser:
                browser.close()
        except Exception:
            pass

    return canonicalize_url(url)


def resolve_url_if_needed(url, http_client, allow_playwright_fallback=True):
    if not url:
        return None

    normalized = normalize_urlish_token(url) or url
    cleaned = canonicalize_url(normalized)
    if not cleaned:
        return None

    if cleaned in _cache.url_resolution:
        logging.info(
            f"⚡ Using cached URL resolution: {cleaned} -> {_cache.url_resolution[cleaned]}"
        )
        return _cache.url_resolution[cleaned]

    if not is_tco_domain(cleaned):
        _cache.url_resolution[cleaned] = cleaned
        return cleaned

    resolved_http = resolve_tco_with_httpx(cleaned, http_client)
    if is_external_non_x_url(resolved_http):
        _cache.url_resolution[cleaned] = resolved_http
        return resolved_http

    if not allow_playwright_fallback:
        _cache.url_resolution[cleaned] = resolved_http
        return resolved_http

    resolved_browser = resolve_tco_with_playwright(cleaned)
    if is_external_non_x_url(resolved_browser):
        logging.info(f"✅ Resolved t.co via Playwright to external URL: {resolved_browser}")
        _cache.url_resolution[cleaned] = resolved_browser
        return resolved_browser

    if resolved_http and not is_tco_domain(resolved_http):
        _cache.url_resolution[cleaned] = resolved_http
        return resolved_http

    _cache.url_resolution[cleaned] = cleaned
    return cleaned


def extract_non_x_urls_from_text(text):
    urls = extract_urls_from_text(text)
    result = []

    for url in urls:
        normalized = normalize_urlish_token(url)
        cleaned = strip_trailing_url_punctuation(normalized or url)
        if not cleaned:
            continue

        if is_tco_domain(cleaned):
            result.append(cleaned)
            continue

        if not is_x_or_twitter_domain(cleaned):
            result.append(cleaned)

    return result


def extract_ordered_non_x_urls(text):
    seen = set()
    ordered = []

    for url in extract_non_x_urls_from_text(text):
        canonical = canonicalize_url(url)
        if canonical and canonical not in seen:
            seen.add(canonical)
            ordered.append(canonical)

    return ordered


def extract_first_visible_non_x_url(text):
    for url in extract_non_x_urls_from_text(text or ""):
        canonical = canonicalize_url(url)
        if canonical:
            return canonical
    return None


def extract_first_resolved_external_url(text, http_client, allow_playwright_fallback=True):
    for url in extract_non_x_urls_from_text(text or ""):
        resolved = resolve_url_if_needed(
            url,
            http_client,
            allow_playwright_fallback=allow_playwright_fallback,
        )
        if not resolved:
            continue

        if is_external_non_x_url(resolved):
            logging.info(f"✅ Selected resolved external URL for card: {resolved}")
            return resolved

    return None


def resolve_card_url(card_url, http_client):
    if not card_url:
        return None

    cleaned = canonicalize_url(card_url.strip())
    if not cleaned:
        return None

    if is_external_non_x_url(cleaned):
        logging.info(f"🔗 Card URL is already external: {cleaned}")
        return cleaned

    if is_tco_domain(cleaned):
        resolved = resolve_url_if_needed(cleaned, http_client, allow_playwright_fallback=True)
        if resolved and is_external_non_x_url(resolved):
            logging.info(f"🔗 Resolved card t.co URL: {cleaned} -> {resolved}")
            return resolved

    if is_x_or_twitter_domain(cleaned):
        logging.info(f"ℹ️ Card URL resolves to X/Twitter domain, ignoring: {cleaned}")
        return None

    return cleaned


def sanitize_visible_urls_in_text(text, http_client, has_media=False):
    if not text:
        return text, None

    working = clean_post_text(text)
    url_pattern = r'(?:(?:https?://)|(?:www\.))[^\s<>"\']+'
    urls = re.findall(url_pattern, working)

    if not urls:
        return working, None

    replacements = {}
    first_external_resolved = None

    for raw_url in urls:
        normalized = normalize_urlish_token(raw_url)
        cleaned = canonicalize_url(normalized or raw_url)
        if not cleaned:
            continue

        if is_x_or_twitter_domain(cleaned):
            replacements[raw_url] = ""
            logging.info(f"🧹 Removing X/Twitter URL from visible text: {cleaned}")
            continue

        final_url = cleaned
        if is_tco_domain(cleaned):
            resolved_http_first = resolve_tco_with_httpx(cleaned, http_client)

            if is_external_non_x_url(resolved_http_first):
                final_url = resolved_http_first
                _cache.url_resolution[cleaned] = final_url
            else:
                if (
                    has_media
                    and resolved_http_first
                    and is_x_or_twitter_domain(resolved_http_first)
                ):
                    final_url = resolved_http_first
                    _cache.url_resolution[cleaned] = final_url
                    logging.info(
                        f"⚡ Skipping Playwright t.co fallback because tweet has media "
                        f"and httpx already resolved to X/Twitter URL: {final_url}"
                    )
                else:
                    final_url = resolve_url_if_needed(
                        cleaned, http_client, allow_playwright_fallback=True
                    )

            if is_x_or_twitter_domain(final_url):
                replacements[raw_url] = ""
                logging.info(
                    f"🧹 Removing resolved X/Twitter URL from visible text: {final_url}"
                )
                continue

        if normalized and normalized.startswith("https://www."):
            final_url = normalized
        elif normalized and normalized.startswith("http://www."):
            final_url = normalized

        if is_external_non_x_url(final_url) and not first_external_resolved:
            first_external_resolved = final_url

        replacements[raw_url] = final_url

    def replace_match(match):
        raw = match.group(0)
        return replacements.get(raw, raw)

    working = re.sub(url_pattern, replace_match, working)

    deduped_lines = []
    for line in working.splitlines():
        line_urls = re.findall(url_pattern, line)
        if len(line_urls) > 1:
            prefix = re.sub(url_pattern, "", line).strip()
            kept_urls = []
            seen_in_line: set = set()

            for url in line_urls:
                normalized = normalize_urlish_token(url) or url
                canonical = canonicalize_url(normalized)

                if not canonical:
                    continue
                if is_x_or_twitter_domain(canonical):
                    continue
                if canonical in seen_in_line:
                    continue

                seen_in_line.add(canonical)
                kept_urls.append(url)

            if prefix and kept_urls:
                rebuilt = prefix + " " + " ".join(kept_urls)
            elif prefix:
                rebuilt = prefix
            else:
                rebuilt = " ".join(kept_urls)

            deduped_lines.append(rebuilt.strip())
        else:
            cleaned_line = re.sub(r"\s{2,}", " ", line).strip()
            deduped_lines.append(cleaned_line)

    working = "\n".join(deduped_lines)
    working = re.sub(r"[ \t]+", " ", working)
    working = re.sub(r"\n{3,}", "\n\n", working).strip()

    return working, first_external_resolved


def build_effective_tweet_text(tweet, http_client):
    scraped_text = clean_post_text(tweet.text or "")
    has_media = bool(tweet.media)
    og_title_text = None

    if should_fetch_og_title(tweet):
        og_title_text = fetch_tweet_og_title_text(tweet.tweet_url)

    candidate_text = scraped_text
    if og_title_text:
        scraped_urls = extract_urls_from_text(scraped_text)
        og_urls = extract_urls_from_text(og_title_text)

        if len(og_title_text) >= len(scraped_text) or (og_urls and not scraped_urls):
            candidate_text = og_title_text
            logging.info("🧾 Using og:title-derived tweet text as primary content")

    candidate_text, resolved_primary_external_url = sanitize_visible_urls_in_text(
        candidate_text, http_client, has_media=has_media,
    )
    candidate_text = clean_post_text(candidate_text)

    resolved_card_url = resolve_card_url(getattr(tweet, "card_url", None), http_client)

    if resolved_card_url and is_external_non_x_url(resolved_card_url):
        if not resolved_primary_external_url:
            resolved_primary_external_url = resolved_card_url
            logging.info(
                f"🔗 Using resolved card URL as primary external URL: {resolved_card_url}"
            )
        elif resolved_primary_external_url != resolved_card_url:
            logging.info(
                f"ℹ️ Card URL ({resolved_card_url}) differs from text URL "
                f"({resolved_primary_external_url}). Preferring card URL for external embed."
            )
            resolved_primary_external_url = resolved_card_url

    if not resolved_primary_external_url:
        resolved_primary_external_url = extract_first_resolved_external_url(
            candidate_text,
            http_client,
            allow_playwright_fallback=not has_media,
        )

    return candidate_text, resolved_primary_external_url


def remove_url_from_visible_text(text, url_to_remove):
    if not text or not url_to_remove:
        return text

    canonical_target = canonicalize_url(url_to_remove)
    lines = text.splitlines()
    cleaned_lines = []

    for line in lines:
        line_urls = extract_urls_from_text(line)
        new_line = line

        for url in line_urls:
            normalized = normalize_urlish_token(url) or url
            cleaned_candidate = canonicalize_url(strip_trailing_url_punctuation(normalized))
            if cleaned_candidate == canonical_target:
                pattern = re.escape(url)
                new_line = re.sub(pattern, "", new_line)

        new_line = re.sub(r"[ \t]+", " ", new_line).strip()
        cleaned_lines.append(new_line)

    result = "\n".join(cleaned_lines)
    result = re.sub(r"[ \t]+", " ", result)
    result = re.sub(r"\n{3,}", "\n\n", result).strip()

    return result


def looks_like_title_plus_url_post(text):
    if not text:
        return False

    repaired = repair_broken_urls(text)
    repaired = strip_line_edge_whitespace(repaired)
    lines = [line.strip() for line in repaired.splitlines() if line.strip()]
    if len(lines) < 2:
        return False

    last_line = lines[-1]
    urls_in_last_line = extract_ordered_non_x_urls(last_line)
    total_urls = extract_ordered_non_x_urls(repaired)

    return (
        len(urls_in_last_line) == 1
        and len(total_urls) == 1
        and last_line.startswith(("http://", "https://", "www."))
    )


def looks_like_url_and_tag_tail(text, primary_non_x_url=None):
    if not text or not primary_non_x_url:
        return False

    repaired = repair_broken_urls(text)
    idx = repaired.find(primary_non_x_url)
    if idx == -1:
        return False

    tail = repaired[idx:].strip()
    if not tail.startswith(("http://", "https://", "www.")):
        return False

    if re.search(r"(?:https?://|www\.)\S+.*#[^\s#]+", tail):
        return True

    return False


def find_tail_preservation_start(text, primary_non_x_url):
    if not text or not primary_non_x_url:
        return None

    url_pos = text.find(primary_non_x_url)
    if url_pos == -1:
        return None

    hashtag_match = re.search(r"\s#[^\s#]+", text[url_pos:])
    has_hashtag_after_url = hashtag_match is not None

    candidates = [url_pos]

    clause_patterns = [
        r"\.\s+", r":\s+", r";\s+", r"!\s+", r"\?\s+", r",\s+",
    ]

    before = text[:url_pos]
    for pattern in clause_patterns:
        for match in re.finditer(pattern, before):
            candidates.append(match.end())

    last_newline = before.rfind("\n")
    if last_newline != -1:
        candidates.append(last_newline + 1)

    if has_hashtag_after_url:
        generous_start = max(0, url_pos - URL_TAIL_MAX_LOOKBACK_CHARS)
        while generous_start > 0 and text[generous_start] not in {" ", "\n"}:
            generous_start -= 1
        candidates.append(generous_start)

    reasonable_candidates = [
        c for c in candidates
        if 0 <= c < url_pos and (url_pos - c) <= URL_TAIL_MAX_CLAUSE_DISTANCE
    ]

    if reasonable_candidates:
        start = min(reasonable_candidates, key=lambda c: (url_pos - c))
        if url_pos - start < URL_TAIL_MIN_PREFIX_CHARS:
            farther = [
                c for c in reasonable_candidates
                if url_pos - c >= URL_TAIL_MIN_PREFIX_CHARS
            ]
            if farther:
                start = min(farther, key=lambda c: (url_pos - c))
        return start

    return url_pos


def truncate_text_safely(text, max_length=BSKY_TEXT_MAX_LENGTH):
    if grapheme_len(text) <= max_length:
        return text

    clusters = list(grapheme.graphemes(text))
    truncated = "".join(clusters[:max_length])
    last_space = truncated.rfind(" ")
    if last_space > TRUNCATE_MIN_PREFIX_CHARS:
        return truncated[:last_space]
    return truncated


def truncate_text_preserving_tail(text, tail_start, max_length=BSKY_TEXT_MAX_LENGTH):
    if (
        not text
        or tail_start is None
        or tail_start < 0
        or tail_start >= len(text)
    ):
        return truncate_text_safely(text, max_length)

    if len(text) <= max_length:
        return text

    tail = text[tail_start:].strip()
    if not tail:
        return truncate_text_safely(text, max_length)

    reserve = len(tail) + 1
    if reserve >= max_length:
        shortened_tail = tail[-max_length:].strip()
        first_space = shortened_tail.find(" ")
        if 0 <= first_space <= 30:
            shortened_tail = shortened_tail[first_space + 1:].strip()
        return shortened_tail

    available_prefix = max_length - reserve
    prefix = text[:tail_start].rstrip()

    if len(prefix) > available_prefix:
        prefix = prefix[:available_prefix].rstrip()
        last_space = prefix.rfind(" ")
        if last_space > 20:
            prefix = prefix[:last_space].rstrip()

    final_text = f"{prefix} {tail}".strip()
    final_text = re.sub(r"[ \t]+", " ", final_text)
    final_text = re.sub(r"\n{3,}", "\n\n", final_text).strip()

    if len(final_text) <= max_length:
        return final_text

    return truncate_text_safely(text, max_length)


def choose_final_visible_text(
    full_clean_text, primary_non_x_url=None, prefer_full_text_without_url=True
):
    text = clean_post_text(full_clean_text or "")
    if not text:
        return text

    if len(text) <= BSKY_TEXT_MAX_LENGTH:
        logging.info("🟢 Original cleaned tweet text fits in Bluesky. Preserving exact text.")
        return text

    if primary_non_x_url:
        tail_start = find_tail_preservation_start(text, primary_non_x_url)

        if tail_start is not None:
            preserved = truncate_text_preserving_tail(text, tail_start, BSKY_TEXT_MAX_LENGTH)
            if preserved and len(preserved) <= BSKY_TEXT_MAX_LENGTH:
                logging.info(
                    "🔗 Preserving meaningful ending block with URL/hashtags in visible Bluesky text"
                )
                return preserved

        if prefer_full_text_without_url and not looks_like_url_and_tag_tail(
            text, primary_non_x_url
        ):
            text_without_url = remove_url_from_visible_text(text, primary_non_x_url).strip()
            if text_without_url and len(text_without_url) <= BSKY_TEXT_MAX_LENGTH:
                logging.info(
                    "🔗 Keeping full visible text by removing long external URL from body and using external card"
                )
                return text_without_url

    truncated = truncate_text_safely(text, BSKY_TEXT_MAX_LENGTH)
    logging.info("✂️ Falling back to safe truncation for visible Bluesky text")
    return truncated


def normalize_post_text(text):
    if not text:
        return ""

    text = clean_post_text(text)
    text = text.replace("\r", "\n")
    text = re.sub(r"\s+", " ", text).strip()
    return text.lower()


def build_media_fingerprint(tweet, resolved_video_hash=None):
    if not tweet or not tweet.media:
        return "no-media"

    parts = []

    for media in tweet.media:
        media_type = getattr(media, "type", "unknown")
        media_url = getattr(media, "media_url_https", "") or ""
        stable_value = media_url

        if media_type == "photo":
            stable_value = re.sub(r"[?&]name=\w+", "", stable_value)
            stable_value = re.sub(r"[?&]format=\w+", "", stable_value)
        elif media_type == "video":
            tweet_key = canonicalize_tweet_url(tweet.tweet_url or media_url or "")
            if resolved_video_hash:
                stable_value = f"{tweet_key}|vh:{resolved_video_hash}"
            else:
                stable_value = tweet_key

    parts.append(f"{media_type}:{stable_value}")

    parts.sort()
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_bsky_media_fingerprint(post_view):
    try:
        embed = getattr(post_view, "embed", None)
        if not embed:
            return "no-media"

        parts = []

        images = getattr(embed, "images", None)
        if images:
            for img in images:
                image_obj = getattr(img, "image", None)
                ref = (
                    getattr(image_obj, "ref", None)
                    or getattr(image_obj, "cid", None)
                    or str(image_obj)
                )
                parts.append(f"photo:{ref}")

        video = getattr(embed, "video", None)
        if video:
            ref = (
                getattr(video, "ref", None)
                or getattr(video, "cid", None)
                or str(video)
            )
            parts.append(f"video:{ref}")

        external = getattr(embed, "external", None)
        if external:
            uri = getattr(external, "uri", None) or str(external)
            parts.append(f"external:{uri}")

        if not parts:
            return "no-media"

        parts.sort()
        raw = "|".join(parts)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    except Exception as e:
        logging.debug(f"Could not build Bluesky media fingerprint: {e}")
        return "no-media"


def build_text_media_key(normalized_text, media_fingerprint):
    return hashlib.sha256(
        f"{normalized_text}||{media_fingerprint}".encode("utf-8")
    ).hexdigest()


# --- Login hardening helpers ---
def is_rate_limited_error(error_obj):
    text = repr(error_obj).lower()
    return (
        "429" in text
        or "ratelimitexceeded" in text
        or "too many requests" in text
        or "rate limit" in text
    )


def is_auth_error(error_obj):
    text = repr(error_obj).lower()
    return (
        "401" in text
        or "403" in text
        or "invalid identifier or password" in text
        or "authenticationrequired" in text
        or "invalidtoken" in text
    )


def is_network_error(error_obj):
    text = repr(error_obj)
    signals = [
        "ConnectError",
        "RemoteProtocolError",
        "ReadTimeout",
        "WriteTimeout",
        "TimeoutException",
        "503",
        "502",
        "504",
        "ConnectionResetError",
    ]
    return any(sig in text for sig in signals)


def create_bsky_client(base_url, handle, password):
    normalized_base_url = (base_url or DEFAULT_BSKY_BASE_URL).strip().rstrip("/")
    logging.info(f"🔐 Connecting Bluesky client via base URL: {normalized_base_url}")

    client = Client(base_url=normalized_base_url)

    max_attempts = BSKY_LOGIN_MAX_RETRIES
    base_delay = BSKY_LOGIN_BASE_DELAY
    max_delay = BSKY_LOGIN_MAX_DELAY
    jitter_max = max(BSKY_LOGIN_JITTER_MAX, 0.0)

    for attempt in range(1, max_attempts + 1):
        try:
            logging.info(f"🔐 Bluesky login attempt {attempt}/{max_attempts} for {handle}")
            client.login(handle, password)
            logging.info("✅ Bluesky login successful.")
            return client

        except Exception as e:
            logging.exception("❌ Bluesky login exception")

            if is_auth_error(e):
                logging.error("❌ Bluesky auth failed (invalid handle/app password).")
                raise

            if is_rate_limited_error(e):
                if attempt < max_attempts:
                    wait = get_rate_limit_wait_seconds(e, default_delay=base_delay)
                    wait = wait + random.uniform(0, jitter_max)
                    logging.warning(
                        f"⏳ Bluesky login rate-limited (attempt {attempt}/{max_attempts}). "
                        f"Retrying in {wait:.1f}s."
                    )
                    time.sleep(wait)
                    continue

                logging.error("❌ Exhausted Bluesky login retries due to rate limiting.")
                raise

            if is_network_error(e) or is_transient_error(e):
                if attempt < max_attempts:
                    wait = min(base_delay * attempt, max_delay) + random.uniform(0, jitter_max)
                    logging.warning(
                        f"⏳ Transient Bluesky login failure (attempt {attempt}/{max_attempts}). "
                        f"Retrying in {wait:.1f}s."
                    )
                    time.sleep(wait)
                    continue

                logging.error("❌ Exhausted Bluesky login retries after transient/network errors.")
                raise

            if attempt < max_attempts:
                wait = min(base_delay * attempt, max_delay) + random.uniform(0, jitter_max)
                logging.warning(
                    f"⏳ Bluesky login retry for unexpected error "
                    f"(attempt {attempt}/{max_attempts}) in {wait:.1f}s."
                )
                time.sleep(wait)
                continue

            raise

    raise RuntimeError("Bluesky login failed after all retries.")


# --- State Management ---
def default_state():
    return {
        "version": 1,
        "posted_tweets": {},
        "posted_by_bsky_uri": {},
        "updated_at": None,
    }


def load_state(state_path=STATE_PATH):
    if not os.path.exists(state_path):
        logging.info(f"🧠 No state file found at {state_path}. Starting with empty memory.")
        return default_state()

    try:
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)

        if not isinstance(state, dict):
            logging.warning("⚠️ State file is invalid. Reinitializing.")
            return default_state()

        state.setdefault("version", 1)
        state.setdefault("posted_tweets", {})
        state.setdefault("posted_by_bsky_uri", {})
        state.setdefault("updated_at", None)

        return state

    except Exception as e:
        logging.warning(f"⚠️ Could not load state file {state_path}: {e}. Reinitializing.")
        return default_state()


def save_state(state, state_path=STATE_PATH):
    try:
        state["updated_at"] = arrow.utcnow().isoformat()
        temp_path = f"{state_path}.tmp"

        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)

        os.replace(temp_path, state_path)
        logging.info(f"💾 State saved to {state_path}")

    except Exception as e:
        logging.error(f"❌ Failed to save state file {state_path}: {e}")


def remember_posted_tweet(state, candidate, bsky_uri=None):
    canonical_tweet_url = candidate.get("canonical_tweet_url")
    fallback_key = f"textmedia:{candidate['text_media_key']}"
    state_key = canonical_tweet_url or fallback_key

    record = {
        "canonical_tweet_url": canonical_tweet_url,
        "normalized_text": candidate["normalized_text"],
        "raw_text": candidate["raw_text"],
        "full_clean_text": candidate.get("full_clean_text", candidate["raw_text"]),
        "media_fingerprint": candidate["media_fingerprint"],
        "text_media_key": candidate["text_media_key"],
        "canonical_non_x_urls": sorted(candidate["canonical_non_x_urls"]),
        "ordered_non_x_urls": candidate.get("ordered_non_x_urls", []),
        "resolved_primary_external_url": candidate.get("resolved_primary_external_url"),
        "bsky_uri": bsky_uri,
        "tweet_created_on": candidate["tweet"].created_on,
        "tweet_url": candidate["tweet"].tweet_url,
        "tweet_id": candidate.get("tweet_id"),
        "resolved_video_url": candidate.get("resolved_video_url"),
        "resolved_video_hash": candidate.get("resolved_video_hash"),
        "posted_at": arrow.utcnow().isoformat(),
    }

    state["posted_tweets"][state_key] = record

    if bsky_uri:
        state["posted_by_bsky_uri"][bsky_uri] = state_key


def candidate_matches_state(candidate, state):
    canonical_tweet_url = candidate["canonical_tweet_url"]
    text_media_key = candidate["text_media_key"]
    normalized_text = candidate["normalized_text"]

    posted_tweets = state.get("posted_tweets", {})

    if canonical_tweet_url and canonical_tweet_url in posted_tweets:
        return True, "state:tweet_url"

    for _, record in posted_tweets.items():
        if record.get("text_media_key") == text_media_key:
            return True, "state:text_media_fingerprint"

    for _, record in posted_tweets.items():
        if record.get("normalized_text") == normalized_text:
            return True, "state:normalized_text"

    return False, None


def prune_state(state, max_entries=5000):
    posted_tweets = state.get("posted_tweets", {})

    if len(posted_tweets) <= max_entries:
        return state

    sortable = []
    for key, record in posted_tweets.items():
        posted_at = record.get("posted_at") or ""
        sortable.append((key, posted_at))

    sortable.sort(key=lambda x: x[1], reverse=True)
    keep_keys = {key for key, _ in sortable[:max_entries]}

    new_posted_tweets = {
        key: record
        for key, record in posted_tweets.items()
        if key in keep_keys
    }
    new_posted_by_bsky_uri = {
        bsky_uri: key
        for bsky_uri, key in state.get("posted_by_bsky_uri", {}).items()
        if key in keep_keys
    }

    state["posted_tweets"] = new_posted_tweets
    state["posted_by_bsky_uri"] = new_posted_by_bsky_uri
    return state


# --- Bluesky Feed Helpers ---
def extract_urls_from_facets(record):
    urls = []

    try:
        facets = getattr(record, "facets", None) or []
        for facet in facets:
            features = getattr(facet, "features", None) or []
            for feature in features:
                uri = getattr(feature, "uri", None)
                if uri:
                    urls.append(uri)
    except Exception as e:
        logging.debug(f"Could not extract facet URLs: {e}")

    return urls


def get_recent_bsky_posts(client, handle, limit=30):
    recent_posts = []

    try:
        timeline = client.get_author_feed(handle, limit=limit)

        for item in timeline.feed:
            try:
                if item.reason is not None:
                    continue

                record = item.post.record
                if getattr(record, "reply", None) is not None:
                    continue

                text = getattr(record, "text", "") or ""
                normalized_text = normalize_post_text(text)

                urls = []
                urls.extend(extract_non_x_urls_from_text(text))
                urls.extend(extract_urls_from_facets(record))

                canonical_non_x_urls = set()
                for url in urls:
                    canonical = canonicalize_url(normalize_urlish_token(url) or url)
                    if canonical:
                        canonical_non_x_urls.add(canonical)

                media_fingerprint = build_bsky_media_fingerprint(item.post)
                text_media_key = build_text_media_key(normalized_text, media_fingerprint)

                recent_posts.append(
                    {
                        "uri": getattr(item.post, "uri", None),
                        "text": text,
                        "normalized_text": normalized_text,
                        "canonical_non_x_urls": canonical_non_x_urls,
                        "media_fingerprint": media_fingerprint,
                        "text_media_key": text_media_key,
                        "created_at": getattr(record, "created_at", None),
                    }
                )

            except Exception as e:
                logging.debug(f"Skipping one Bluesky feed item during dedupe fetch: {e}")

    except Exception as e:
        logging.warning(
            f"⚠️ Could not fetch recent Bluesky posts for duplicate detection "
            f"(live dedup disabled for this cycle): {e}"
        )

    return recent_posts


# --- Upload / Retry Helpers ---
def get_rate_limit_wait_seconds(error_obj, default_delay):
    """
    Parse common rate-limit headers and return a bounded wait time in seconds.
    Supports:
      - retry-after
      - x-ratelimit-after
      - ratelimit-reset (unix timestamp)
    """
    try:
        now_ts = int(time.time())
        headers = getattr(error_obj, "headers", None) or {}

        retry_after = headers.get("retry-after") or headers.get("Retry-After")
        if retry_after:
            return min(max(int(retry_after), 1), BSKY_LOGIN_MAX_DELAY)

        x_after = headers.get("x-ratelimit-after") or headers.get("X-RateLimit-After")
        if x_after:
            return min(max(int(x_after), 1), BSKY_LOGIN_MAX_DELAY)

        reset_value = headers.get("ratelimit-reset") or headers.get("RateLimit-Reset")
        if reset_value:
            wait_seconds = max(int(reset_value) - now_ts + 1, default_delay)
            return min(wait_seconds, BSKY_LOGIN_MAX_DELAY)
    except Exception:
        pass

    try:
        response = getattr(error_obj, "response", None)
        headers = getattr(response, "headers", None) or {}
        now_ts = int(time.time())

        retry_after = headers.get("retry-after") or headers.get("Retry-After")
        if retry_after:
            return min(max(int(retry_after), 1), BSKY_LOGIN_MAX_DELAY)

        x_after = headers.get("x-ratelimit-after") or headers.get("X-RateLimit-After")
        if x_after:
            return min(max(int(x_after), 1), BSKY_LOGIN_MAX_DELAY)

        reset_value = headers.get("ratelimit-reset") or headers.get("RateLimit-Reset")
        if reset_value:
            wait_seconds = max(int(reset_value) - now_ts + 1, default_delay)
            return min(wait_seconds, BSKY_LOGIN_MAX_DELAY)
    except Exception:
        pass

    # repr fallback parsing
    text = repr(error_obj)
    m = re.search(r"'retry-after': '(\d+)'", text, re.IGNORECASE)
    if m:
        return min(max(int(m.group(1)), 1), BSKY_LOGIN_MAX_DELAY)

    m = re.search(r"'x-ratelimit-after': '(\d+)'", text, re.IGNORECASE)
    if m:
        return min(max(int(m.group(1)), 1), BSKY_LOGIN_MAX_DELAY)

    m = re.search(r"'ratelimit-reset': '(\d+)'", text, re.IGNORECASE)
    if m:
        now_ts = int(time.time())
        wait_seconds = max(int(m.group(1)) - now_ts + 1, default_delay)
        return min(wait_seconds, BSKY_LOGIN_MAX_DELAY)

    return default_delay


def is_transient_error(error_obj):
    error_text = repr(error_obj)
    transient_signals = [
        "InvokeTimeoutError",
        "ReadTimeout",
        "WriteTimeout",
        "TimeoutException",
        "RemoteProtocolError",
        "ConnectError",
        "503",
        "502",
        "504",
    ]
    return any(signal in error_text for signal in transient_signals)


def upload_blob_with_retry(client, binary_data, media_label="media"):
    last_exception = None
    transient_attempts = 0

    for attempt in range(1, BSKY_BLOB_UPLOAD_MAX_RETRIES + 1):
        try:
            result = client.upload_blob(binary_data)
            return result.blob

        except Exception as e:
            last_exception = e
            error_text = str(e)
            is_rate_limited = (
                "429" in error_text or "RateLimitExceeded" in error_text
            )

            if is_rate_limited:
                backoff_delay = min(
                    BSKY_BLOB_UPLOAD_BASE_DELAY * (2 ** (attempt - 1)),
                    BSKY_BLOB_UPLOAD_MAX_DELAY,
                )
                wait_seconds = get_rate_limit_wait_seconds(e, backoff_delay)

                if attempt < BSKY_BLOB_UPLOAD_MAX_RETRIES:
                    logging.warning(
                        f"⏳ Bluesky blob upload rate-limited for {media_label}. "
                        f"Retry {attempt}/{BSKY_BLOB_UPLOAD_MAX_RETRIES} after {wait_seconds}s."
                    )
                    time.sleep(wait_seconds)
                    continue
                else:
                    logging.warning(
                        f"❌ Exhausted blob upload retries for {media_label} "
                        f"after rate limiting: {repr(e)}"
                    )
                    break

            if (
                is_transient_error(e)
                and transient_attempts < BSKY_BLOB_TRANSIENT_ERROR_RETRIES
            ):
                transient_attempts += 1
                wait_seconds = BSKY_BLOB_TRANSIENT_ERROR_DELAY * transient_attempts
                logging.warning(
                    f"⏳ Transient blob upload failure for {media_label}: {repr(e)}. "
                    f"Transient retry {transient_attempts}/"
                    f"{BSKY_BLOB_TRANSIENT_ERROR_RETRIES} after {wait_seconds}s."
                )
                time.sleep(wait_seconds)
                continue

            logging.warning(f"Could not upload {media_label}: {repr(e)}")

            if hasattr(e, "response") and e.response is not None:
                try:
                    logging.warning(f"Upload response status: {e.response.status_code}")
                    logging.warning(f"Upload response body: {e.response.text}")
                except Exception:
                    pass

            return None

    logging.warning(f"Could not upload {media_label}: {repr(last_exception)}")
    return None


def send_post_with_retry(client, **kwargs):
    last_exception = None

    for attempt in range(1, BSKY_SEND_POST_MAX_RETRIES + 1):
        try:
            return client.send_post(**kwargs)

        except Exception as e:
            last_exception = e
            error_text = str(e)
            is_rate_limited = (
                "429" in error_text or "RateLimitExceeded" in error_text
            )

            if is_rate_limited:
                backoff_delay = min(
                    BSKY_SEND_POST_BASE_DELAY * (2 ** (attempt - 1)),
                    BSKY_SEND_POST_MAX_DELAY,
                )
                wait_seconds = get_rate_limit_wait_seconds(e, backoff_delay)

                if attempt < BSKY_SEND_POST_MAX_RETRIES:
                    logging.warning(
                        f"⏳ Bluesky send_post rate-limited. "
                        f"Retry {attempt}/{BSKY_SEND_POST_MAX_RETRIES} after {wait_seconds}s."
                    )
                    time.sleep(wait_seconds)
                    continue
                else:
                    logging.error(
                        f"❌ Exhausted send_post retries after rate limiting: {repr(e)}"
                    )
                    raise

            if is_transient_error(e) and attempt < BSKY_SEND_POST_MAX_RETRIES:
                wait_seconds = BSKY_SEND_POST_BASE_DELAY * attempt
                logging.warning(
                    f"⏳ Transient send_post failure: {repr(e)}. "
                    f"Retry {attempt}/{BSKY_SEND_POST_MAX_RETRIES} after {wait_seconds}s."
                )
                time.sleep(wait_seconds)
                continue

            raise

    raise last_exception


def compress_post_image_to_limit(image_bytes, max_bytes=BSKY_IMAGE_MAX_BYTES):
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            img = img.convert("RGB")
            width, height = img.size
            max_dim = max(width, height)

            if max_dim > BSKY_IMAGE_MAX_DIMENSION:
                scale = BSKY_IMAGE_MAX_DIMENSION / max_dim
                new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
                img = img.resize(new_size, Image.LANCZOS)
                logging.info(f"🖼️ Resized post image to {new_size[0]}x{new_size[1]}")

            for quality in [90, 82, 75, 68, 60, 52, BSKY_IMAGE_MIN_JPEG_QUALITY]:
                out = io.BytesIO()
                img.save(out, format="JPEG", quality=quality, optimize=True, progressive=True)
                data = out.getvalue()
                logging.info(
                    f"🖼️ Post image candidate size at JPEG quality {quality}: "
                    f"{len(data)} bytes ({len(data) / 1024:.2f} KB)"
                )
                if len(data) <= max_bytes:
                    return data

            for target_dim in [1800, 1600, 1400, 1200, 1000]:
                resized = img.copy()
                width, height = resized.size
                max_dim = max(width, height)

                if max_dim > target_dim:
                    scale = target_dim / max_dim
                    new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
                    resized = resized.resize(new_size, Image.LANCZOS)

                for quality in [68, 60, 52, BSKY_IMAGE_MIN_JPEG_QUALITY]:
                    out = io.BytesIO()
                    resized.save(out, format="JPEG", quality=quality, optimize=True, progressive=True)
                    data = out.getvalue()
                    logging.info(
                        f"🖼️ Post image resized to <= {target_dim}px at quality {quality}: "
                        f"{len(data)} bytes ({len(data) / 1024:.2f} KB)"
                    )
                    if len(data) <= max_bytes:
                        return data

    except Exception as e:
        logging.warning(f"Could not compress post image: {repr(e)}")

    return None


def get_blob_from_url(media_url, client, http_client):
    try:
        r = http_client.get(media_url, timeout=MEDIA_DOWNLOAD_TIMEOUT, follow_redirects=True)
        if r.status_code != 200:
            logging.warning(f"Could not fetch media {media_url}: HTTP {r.status_code}")
            return None

        content = r.content
        if not content:
            logging.warning(f"Could not fetch media {media_url}: empty response body")
            return None

        content_type = (r.headers.get("content-type") or "").lower()
        upload_bytes = content

        if content_type.startswith("image/"):
            original_size = len(content)
            logging.info(
                f"🖼️ Downloaded post image {media_url} "
                f"({original_size} bytes / {original_size / 1024:.2f} KB)"
            )

            if original_size > BSKY_IMAGE_MAX_BYTES:
                logging.info(
                    f"🖼️ Post image exceeds safe Bluesky limit "
                    f"({original_size} bytes > {BSKY_IMAGE_MAX_BYTES} bytes). Compressing..."
                )
                compressed = compress_post_image_to_limit(content, BSKY_IMAGE_MAX_BYTES)
                if compressed:
                    upload_bytes = compressed
                    logging.info(
                        f"✅ Post image compressed to {len(upload_bytes)} bytes "
                        f"({len(upload_bytes) / 1024:.2f} KB)"
                    )
                else:
                    logging.warning(f"⚠️ Could not compress post image to safe limit: {media_url}")
                    return None

        return upload_blob_with_retry(client, upload_bytes, media_label=media_url)

    except Exception as e:
        logging.warning(f"Could not fetch media {media_url}: {repr(e)}")
        return None


def get_blob_from_file(file_path, client):
    try:
        if not os.path.exists(file_path):
            logging.warning(f"Could not upload local file {file_path}: file does not exist")
            return None

        file_size = os.path.getsize(file_path)
        file_size_mb = file_size / (1024 * 1024)
        logging.info(f"📦 Uploading local file {file_path} ({file_size_mb:.2f} MB)")

        if file_path.lower().endswith(".mp4") and file_size_mb > MAX_VIDEO_UPLOAD_SIZE_MB:
            logging.warning(
                f"Could not upload local file {file_path}: "
                f"file too large ({file_size_mb:.2f} MB > {MAX_VIDEO_UPLOAD_SIZE_MB} MB)"
            )
            return None

        with open(file_path, "rb") as f:
            binary_data = f.read()

        return upload_blob_with_retry(client, binary_data, media_label=file_path)

    except Exception as e:
        logging.warning(f"Could not upload local file {file_path}: {repr(e)}")

        if hasattr(e, "response") and e.response is not None:
            try:
                logging.warning(f"Upload response status: {e.response.status_code}")
                logging.warning(f"Upload response body: {e.response.text}")
            except Exception:
                pass

        return None


def compress_external_thumb_to_limit(image_bytes, max_bytes=EXTERNAL_THUMB_MAX_BYTES):
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            img = img.convert("RGB")
            width, height = img.size
            max_dim = max(width, height)

            if max_dim > EXTERNAL_THUMB_MAX_DIMENSION:
                scale = EXTERNAL_THUMB_MAX_DIMENSION / max_dim
                new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
                img = img.resize(new_size, Image.LANCZOS)
                logging.info(f"🖼️ Resized external thumb to {new_size[0]}x{new_size[1]}")

            for quality in [85, 75, 65, 55, 45, EXTERNAL_THUMB_MIN_JPEG_QUALITY]:
                out = io.BytesIO()
                img.save(out, format="JPEG", quality=quality, optimize=True, progressive=True)
                data = out.getvalue()
                logging.info(
                    f"🖼️ External thumb candidate size at JPEG quality {quality}: "
                    f"{len(data) / 1024:.2f} KB"
                )
                if len(data) <= max_bytes:
                    return data

            for target_dim in [1000, 900, 800, 700, 600]:
                resized = img.copy()
                width, height = resized.size
                max_dim = max(width, height)

                if max_dim > target_dim:
                    scale = target_dim / max_dim
                    new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
                    resized = resized.resize(new_size, Image.LANCZOS)

                for quality in [60, 50, 45, EXTERNAL_THUMB_MIN_JPEG_QUALITY]:
                    out = io.BytesIO()
                    resized.save(out, format="JPEG", quality=quality, optimize=True, progressive=True)
                    data = out.getvalue()
                    logging.info(
                        f"🖼️ External thumb resized to <= {target_dim}px at quality {quality}: "
                        f"{len(data) / 1024:.2f} KB"
                    )
                    if len(data) <= max_bytes:
                        return data

    except Exception as e:
        logging.warning(f"Could not compress external thumbnail: {repr(e)}")

    return None


def get_external_thumb_blob_from_url(image_url, client, http_client):
    try:
        r = http_client.get(image_url, timeout=MEDIA_DOWNLOAD_TIMEOUT, follow_redirects=True)
        if r.status_code != 200:
            logging.warning(f"Could not fetch external thumb {image_url}: HTTP {r.status_code}")
            return None

        content = r.content
        if not content:
            logging.warning(f"Could not fetch external thumb {image_url}: empty body")
            return None

        original_size_kb = len(content) / 1024
        logging.info(f"🖼️ Downloaded external thumb {image_url} ({original_size_kb:.2f} KB)")

        upload_bytes = content
        if len(upload_bytes) > EXTERNAL_THUMB_MAX_BYTES:
            logging.info(
                f"🖼️ External thumb exceeds safe limit "
                f"({original_size_kb:.2f} KB > {EXTERNAL_THUMB_MAX_BYTES / 1024:.2f} KB). Compressing..."
            )
            compressed = compress_external_thumb_to_limit(upload_bytes, EXTERNAL_THUMB_MAX_BYTES)
            if compressed:
                upload_bytes = compressed
                logging.info(f"✅ External thumb compressed to {len(upload_bytes) / 1024:.2f} KB")
            else:
                logging.warning("⚠️ Could not compress external thumb to fit limit. Will omit thumbnail.")
                return None
        else:
            logging.info("✅ External thumb already within safe size limit.")

        blob = upload_blob_with_retry(client, upload_bytes, media_label=f"external-thumb:{image_url}")
        if blob:
            return blob

        logging.warning("⚠️ External thumb upload failed. Will omit thumbnail.")
        return None

    except Exception as e:
        logging.warning(f"Could not fetch/upload external thumb {image_url}: {repr(e)}")
        return None


def fetch_link_metadata(url, http_client):
    try:
        r = http_client.get(url, timeout=LINK_METADATA_TIMEOUT, follow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        title = soup.find("meta", property="og:title") or soup.find("title")
        desc = (
            soup.find("meta", property="og:description")
            or soup.find("meta", attrs={"name": "description"})
        )
        image = (
            soup.find("meta", property="og:image")
            or soup.find("meta", attrs={"name": "twitter:image"})
        )

        return {
            "title": (
                title["content"]
                if title and title.has_attr("content")
                else (title.text.strip() if title and title.text else "")
            ),
            "description": (
                desc["content"] if desc and desc.has_attr("content") else ""
            ),
            "image": (
                image["content"] if image and image.has_attr("content") else None
            ),
        }

    except Exception as e:
        logging.warning(f"Could not fetch link metadata for {url}: {repr(e)}")
        return {}


def build_external_link_embed(
    url, client, http_client, fallback_title="Link", prefetched_metadata=None,
):
    link_metadata = (
        prefetched_metadata
        if prefetched_metadata is not None
        else fetch_link_metadata(url, http_client)
    )

    thumb_blob = None
    if link_metadata.get("image"):
        thumb_blob = get_external_thumb_blob_from_url(
            link_metadata["image"], client, http_client
        )
        if thumb_blob:
            logging.info("✅ External link card thumbnail prepared successfully")
        else:
            logging.info("ℹ️ External link card will be posted without thumbnail")

    if (
        link_metadata.get("title")
        or link_metadata.get("description")
        or thumb_blob
    ):
        return models.AppBskyEmbedExternal.Main(
            external=models.AppBskyEmbedExternal.External(
                uri=url,
                title=link_metadata.get("title") or fallback_title,
                description=link_metadata.get("description") or "",
                thumb=thumb_blob,
            )
        )

    return None


def make_rich(content):
    text_builder = client_utils.TextBuilder()
    content = clean_post_text(content)
    lines = content.splitlines()

    for line_idx, line in enumerate(lines):
        if not line.strip():
            if line_idx < len(lines) - 1:
                text_builder.text("\n")
            continue

        words = line.split(" ")
        for i, word in enumerate(words):
            if not word:
                if i < len(words) - 1:
                    text_builder.text(" ")
                continue

            cleaned_word = strip_trailing_url_punctuation(word)
            normalized_candidate = normalize_urlish_token(cleaned_word)

            if normalized_candidate:
                if is_x_or_twitter_domain(normalized_candidate):
                    text_builder.text(word)
                else:
                    clean_url_value = clean_url(normalized_candidate)
                    if clean_url_value and is_valid_url(clean_url_value):
                        text_builder.link(cleaned_word, clean_url_value)
                        trailing = word[len(cleaned_word):]
                        if trailing:
                            text_builder.text(trailing)
                    else:
                        text_builder.text(word)

            elif cleaned_word.startswith("#") and len(cleaned_word) > 1:
                clean_tag = cleaned_word[1:].rstrip(".,;:!?)'\"")
                if clean_tag:
                    text_builder.tag(cleaned_word, clean_tag)
                    trailing = word[len(cleaned_word):]
                    if trailing:
                        text_builder.text(trailing)
                else:
                    text_builder.text(word)

            else:
                text_builder.text(word)

            if i < len(words) - 1:
                text_builder.text(" ")

        if line_idx < len(lines) - 1:
            text_builder.text("\n")

    return text_builder


def build_dynamic_alt(raw_text, link_title=None):
    dynamic_alt = clean_post_text(raw_text)
    dynamic_alt = dynamic_alt.replace("\n", " ").strip()
    dynamic_alt = re.sub(r"(?:(?:https?://)|(?:www\.))\S+", "", dynamic_alt).strip()

    if not dynamic_alt and link_title:
        dynamic_alt = link_title.strip()

    if len(dynamic_alt) > DYNAMIC_ALT_MAX_LENGTH:
        dynamic_alt = dynamic_alt[:DYNAMIC_ALT_MAX_LENGTH]
    elif not dynamic_alt:
        dynamic_alt = "Attached video or image from tweet"

    return dynamic_alt


def build_video_embed(video_blob, alt_text):
    try:
        return models.AppBskyEmbedVideo.Main(video=video_blob, alt=alt_text)
    except AttributeError:
        logging.error(
            "❌ Your atproto version does not support AppBskyEmbedVideo. Upgrade atproto."
        )
        return None


# --- Twitter Scraping ---
def scrape_tweets_via_playwright(username, password, email, target_handle, locale="en-US"):
    tweets = []
    state_file = "twitter_browser_state.json"

    if os.path.exists(state_file):
        try:
            os.chmod(state_file, SESSION_FILE_PERMISSIONS)
        except Exception as e:
            logging.warning(f"⚠️ Could not set permissions on {state_file}: {e}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        clean_ua = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/145.0.7632.6 Safari/537.36"
        )

        browser_context = None
        needs_login = True
        session_check_page = None

        if os.path.exists(state_file):
            logging.info("✅ Found existing browser state. Attempting to bypass login...")
            browser_context = browser.new_context(
                user_agent=clean_ua,
                viewport={"width": 1920, "height": 1080},
                storage_state=state_file,
                locale=locale,
            )
            session_check_page = browser_context.new_page()
            session_check_page.goto("https://x.com/home")
            time.sleep(3)

            if (
                session_check_page.locator('[data-testid="SideNav_NewTweet_Button"]').is_visible()
                or "/home" in session_check_page.url
            ):
                logging.info("✅ Session is valid!")
                needs_login = False
            else:
                logging.warning("⚠️ Saved session expired or invalid. Re-logging in...")
                session_check_page.close()
                session_check_page = None
                browser_context.close()
                browser_context = None
                os.remove(state_file)

        if session_check_page is not None:
            session_check_page.close()
            session_check_page = None

        if needs_login:
            logging.info("🚀 Launching fresh browser for automated Twitter login...")
            browser_context = browser.new_context(
                user_agent=clean_ua,
                viewport={"width": 1920, "height": 1080},
                locale=locale,
            )
            login_page = browser_context.new_page()

            try:
                login_page.goto("https://x.com")
                sign_in_button = login_page.get_by_text("Sign in", exact=True)
                sign_in_button.wait_for(state="visible", timeout=15000)
                sign_in_button.click(force=True)

                login_page.wait_for_selector(
                    'h1:has-text("Sign in to X")', state="visible", timeout=25000,
                )
                logging.info(f"👤 Entering username: {username}...")
                time.sleep(1)

                username_input = login_page.locator('input[autocomplete="username"]').first
                username_input.wait_for(state="visible", timeout=15000)
                username_input.click(force=True)
                username_input.press_sequentially(username, delay=100)

                login_page.locator('button:has-text("Next")').first.click(force=True)
                login_page.wait_for_selector(
                    'input[name="password"], '
                    'input[data-testid="ocfEnterTextTextInput"], '
                    'input[name="text"]',
                    timeout=15000,
                )
                time.sleep(1)

                if login_page.locator(
                    'input[data-testid="ocfEnterTextTextInput"]'
                ).is_visible() or login_page.locator('input[name="text"]').is_visible():
                    logging.warning("🛡️ Security challenge detected! Entering email/phone...")
                    login_page.fill(
                        'input[data-testid="ocfEnterTextTextInput"], input[name="text"]',
                        email,
                    )
                    sec_next = login_page.locator(
                        '[data-testid="ocfEnterTextNextButton"], span:has-text("Next")'
                    ).first
                    if sec_next.is_visible():
                        sec_next.click(force=True)
                    else:
                        login_page.keyboard.press("Enter")
                    login_page.wait_for_selector('input[name="password"]', timeout=15000)
                    time.sleep(1)

                logging.info("🔑 Entering password...")
                login_page.fill('input[name="password"]', password)
                login_page.locator('span:has-text("Log in")').first.click()

                login_page.wait_for_url("**/home", timeout=40000)
                time.sleep(3)

                browser_context.storage_state(path=state_file)
                try:
                    os.chmod(state_file, SESSION_FILE_PERMISSIONS)
                except Exception as chmod_err:
                    logging.warning(
                        f"⚠️ Could not set permissions on {state_file} after save: {chmod_err}"
                    )
                logging.info("✅ Login successful. Browser state saved.")

            except Exception as e:
                take_error_screenshot(login_page, "login_failed")
                logging.error(f"❌ Login failed: {e}")
                login_page.close()
                browser.close()
                return []

            login_page.close()

        logging.info(f"🌐 Navigating to https://x.com/{target_handle} to scrape tweets...")
        scrape_page = browser_context.new_page()
        scrape_page.goto(f"https://x.com/{target_handle}")

        try:
            scrape_page.wait_for_selector("article", timeout=40000)
            time.sleep(2)

            articles = scrape_page.locator("article").all()
            logging.info(
                f"📊 Found {len(articles)} tweets on screen. "
                f"Parsing up to {SCRAPE_TWEET_LIMIT}..."
            )

            for article in articles[:SCRAPE_TWEET_LIMIT]:
                try:
                    time_el = article.locator("time").first
                    if not time_el.is_visible():
                        continue

                    created_at = time_el.get_attribute("datetime")

                    tweet_url = None
                    time_link = article.locator("a:has(time)").first
                    if time_link.is_visible():
                        href = time_link.get_attribute("href")
                        if href:
                            tweet_url = (
                                f"https://x.com{href}" if href.startswith("/") else href
                            )

                    is_retweet = False
                    try:
                        social_context_el = article.locator(
                            '[data-testid="socialContext"]'
                        ).first
                        if social_context_el.is_visible():
                            context_text = social_context_el.inner_text().lower()
                            repost_keywords = [
                                "reposted", "retweeted", "ha repostejat",
                                "ha retuitat", "repostejat", "ha reposteado", "retuiteó",
                            ]
                            if any(kw in context_text for kw in repost_keywords):
                                is_retweet = True
                                logging.info(f"🔁 Detected retweet/repost: {tweet_url}")
                    except Exception:
                        pass

                    text_locator = article.locator('[data-testid="tweetText"]').first
                    text = text_locator.inner_text() if text_locator.is_visible() else ""

                    media_urls = []

                    photo_locators = article.locator('[data-testid="tweetPhoto"] img').all()
                    for img in photo_locators:
                        src = img.get_attribute("src")
                        if src:
                            src = re.sub(r"&name=\w+", "&name=large", src)
                            media_urls.append((src, "photo"))

                    video_locators = article.locator('[data-testid="videoPlayer"]').all()
                    if video_locators:
                        media_urls.append((tweet_url or "", "video"))

                    card_url = None
                    try:
                        card_locator = article.locator(
                            '[data-testid="card.wrapper"] a[href]'
                        ).first
                        if card_locator.is_visible():
                            card_href = card_locator.get_attribute("href")
                            if card_href:
                                card_url = card_href.strip()
                                logging.info(f"🃏 Scraped card URL from tweet: {card_url}")
                    except Exception:
                        pass

                    if not card_url:
                        try:
                            card_role_link = article.locator(
                                '[data-testid="card.wrapper"] [role="link"]'
                            ).first
                            if card_role_link.is_visible():
                                card_a = card_role_link.locator("a[href]").first
                                if card_a.is_visible():
                                    card_href = card_a.get_attribute("href")
                                    if card_href:
                                        card_url = card_href.strip()
                                        logging.info(
                                            f"🃏 Scraped card URL (fallback) from tweet: {card_url}"
                                        )
                        except Exception:
                            pass

                    tweets.append(
                        ScrapedTweet(
                            created_at,
                            text,
                            media_urls,
                            tweet_url=tweet_url,
                            card_url=card_url,
                            is_retweet=is_retweet,
                        )
                    )

                except Exception as e:
                    logging.warning(f"⚠️ Failed to parse a specific tweet: {e}")
                    continue

        except Exception as e:
            take_error_screenshot(scrape_page, "scrape_failed")
            logging.error(f"❌ Failed to scrape profile: {e}")

        browser.close()
        return tweets


# --- Video Extraction & Processing ---
def extract_video_url_from_tweet_page_isolated(browser, tweet_url, tweet_id=None, locale="en-US"):
    ctx = None
    page = None
    best_m3u8_url = None
    best_video_mp4_url = None
    seen_urls = set()

    def current_best():
        return best_m3u8_url or best_video_mp4_url

    def handle_response(response):
        nonlocal best_m3u8_url, best_video_mp4_url
        try:
            url = response.url
            if not url or url in seen_urls:
                return
            seen_urls.add(url)

            owner = _cache.video_url_owner.get(url)
            if owner and tweet_id and owner != tweet_id:
                logging.warning(
                    f"[tweet_id={tweet_id}] Rejecting URL owned by tweet_id={owner}: {url}"
                )
                return

            content_type = (response.headers.get("content-type") or "").lower()
            url_l = url.lower()

            if ".m4s" in url_l:
                return

            if (
                ".m3u8" in url_l
                or "application/vnd.apple.mpegurl" in content_type
                or "application/x-mpegurl" in content_type
            ):
                if best_m3u8_url is None:
                    best_m3u8_url = url
                return

            if ".mp4" in url_l or "video/mp4" in content_type or "audio/mp4" in content_type:
                if media_url_looks_audio_only(url):
                    return
                if best_video_mp4_url is None:
                    best_video_mp4_url = url
                return
        except Exception as e:
            logging.debug(f"[tweet_id={tweet_id}] response parse error: {e}")

    try:
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/145.0.7632.6 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            locale=locale,
        )
        page = ctx.new_page()
        page.on("response", handle_response)

        logging.info(
            f"[tweet_id={tweet_id}] 🎬 Opening tweet page to capture video URL: {tweet_url}"
        )
        page.goto(tweet_url, wait_until="domcontentloaded", timeout=40000)
        time.sleep(2)

        player = page.locator('[data-testid="videoPlayer"]').first
        if player.count() > 0:
            try:
                player.scroll_into_view_if_needed(timeout=5000)
            except Exception:
                pass
            try:
                player.click(force=True, timeout=5000)
            except Exception:
                pass

        for _ in range(VIDEO_PLAYER_WAIT_ROUNDS):
            if current_best():
                break
            time.sleep(1)

        if not current_best() and player.count() > 0:
            try:
                player.click(force=True, timeout=5000)
            except Exception:
                pass
            try:
                page.keyboard.press("Space")
            except Exception:
                pass
            for _ in range(VIDEO_PLAYER_RETRY_ROUNDS):
                if current_best():
                    break
                time.sleep(1)

        selected = current_best()
        if selected and tweet_id:
            _cache.video_url_owner[selected] = tweet_id

        logging.info(f"[tweet_id={tweet_id}] ✅ Selected media URL for download: {selected}")
        return selected

    except Exception as e:
        logging.warning(f"[tweet_id={tweet_id}] ⚠️ Could not extract video URL: {e}")
        return None
    finally:
        try:
            if page:
                page.remove_listener("response", handle_response)
                page.close()
        except Exception:
            pass
        try:
            if ctx:
                ctx.close()
        except Exception:
            pass


def _probe_video_duration(file_path):
    probe_cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        file_path,
    ]
    try:
        result = subprocess.run(
            probe_cmd,
            capture_output=True,
            text=True,
            timeout=FFPROBE_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"ffprobe exited with code {result.returncode}: {result.stderr.strip()}"
            )
        duration_str = result.stdout.strip()
        if not duration_str:
            raise RuntimeError("ffprobe returned empty duration output")
        return float(duration_str)
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"ffprobe timed out after {FFPROBE_TIMEOUT_SECONDS}s on {file_path}"
        )


def download_and_crop_video(video_url, output_path):
    temp_input = output_path.replace(".mp4", "_source.mp4")
    temp_trimmed = output_path.replace(".mp4", "_trimmed.mp4")
    temp_output = output_path.replace(".mp4", "_compressed.mp4")

    try:
        logging.info(f"⬇️ Downloading video source with ffmpeg: {video_url}")
        video_url_l = video_url.lower()

        if ".m3u8" in video_url_l:
            logging.info("📺 Using HLS ffmpeg mode")
            download_cmd = [
                "ffmpeg", "-y",
                "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
                "-allowed_extensions", "ALL",
                "-i", video_url,
                "-c", "copy",
                temp_input,
            ]
        else:
            logging.info("🎥 Using direct MP4 ffmpeg mode")
            download_cmd = [
                "ffmpeg", "-y",
                "-i", video_url,
                "-c", "copy",
                temp_input,
            ]

        download_result = subprocess.run(
            download_cmd,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )

        if download_result.returncode != 0:
            logging.error(f"❌ ffmpeg download failed:\n{download_result.stderr}")
            return None

        if not os.path.exists(temp_input) or os.path.getsize(temp_input) == 0:
            logging.error("❌ Downloaded video source file is missing or empty.")
            return None

        logging.info(f"✅ Video downloaded: {temp_input}")

        try:
            duration = _probe_video_duration(temp_input)
        except RuntimeError as probe_err:
            logging.error(f"❌ Could not probe video duration: {probe_err}")
            return None

        if duration <= 0:
            logging.error("❌ Downloaded video has invalid or unknown duration.")
            return None

        end_time = min(VIDEO_MAX_DURATION_SECONDS, duration)
        # Guard against floating-point precision errors where end_time == duration
        # causes MoviePy to reject the subclip as out of bounds
        end_time = min(end_time, duration - 0.05)
        end_time = max(end_time, 0.1)  # safety: never go negative or zero

        video_clip = VideoFileClip(temp_input)
        try:
            if hasattr(video_clip, "subclipped"):
                cropped_clip = video_clip.subclipped(0, end_time)
            else:
                cropped_clip = video_clip.subclip(0, end_time)

            try:
                cropped_clip.write_videofile(
                    temp_trimmed,
                    codec="libx264",
                    audio_codec="aac",
                    preset="veryfast",
                    bitrate="1800k",
                    audio_bitrate="128k",
                    logger=None,
                )
            finally:
                cropped_clip.close()
        finally:
            video_clip.close()

        if not os.path.exists(temp_trimmed) or os.path.getsize(temp_trimmed) == 0:
            logging.error("❌ Trimmed video output is missing or empty.")
            return None

        trimmed_size_mb = os.path.getsize(temp_trimmed) / (1024 * 1024)
        logging.info(f"📦 Trimmed video size before compression: {trimmed_size_mb:.2f} MB")

        compress_cmd = [
            "ffmpeg", "-y",
            "-i", temp_trimmed,
            "-vf", "scale='min(720,iw)':-2",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "30",
            "-maxrate", "1800k",
            "-bufsize", "3600k",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            temp_output,
        ]

        compress_result = subprocess.run(
            compress_cmd,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )

        if compress_result.returncode != 0:
            logging.error(f"❌ ffmpeg compression failed:\n{compress_result.stderr}")
            return None

        if not os.path.exists(temp_output) or os.path.getsize(temp_output) == 0:
            logging.error("❌ Compressed video output is missing or empty.")
            return None

        final_size_mb = os.path.getsize(temp_output) / (1024 * 1024)
        logging.info(
            f"✅ Video compressed successfully: {temp_output} ({final_size_mb:.2f} MB)"
        )

        os.replace(temp_output, output_path)
        logging.info(f"✅ Final video ready: {output_path}")
        return output_path

    except subprocess.TimeoutExpired:
        logging.error(f"❌ ffmpeg subprocess timed out after {SUBPROCESS_TIMEOUT_SECONDS}s")
        return None

    except Exception as e:
        logging.error(f"❌ Error processing video: {repr(e)}")
        return None

    finally:
        remove_file_quietly(temp_input)
        remove_file_quietly(temp_trimmed)
        remove_file_quietly(temp_output)


# --- Deduplication ---
def candidate_matches_existing_bsky(candidate, recent_bsky_posts):
    candidate_non_x_urls = candidate["canonical_non_x_urls"]
    candidate_text_media_key = candidate["text_media_key"]
    candidate_normalized_text = candidate["normalized_text"]

    for existing in recent_bsky_posts:
        existing_non_x_urls = existing["canonical_non_x_urls"]

        if (
            candidate_non_x_urls
            and candidate_non_x_urls == existing_non_x_urls
            and candidate_normalized_text == existing["normalized_text"]
        ):
            return True, "bsky:normalized_text_plus_non_x_urls"

        if candidate_text_media_key == existing["text_media_key"]:
            return True, "bsky:text_media_fingerprint"

        if candidate_normalized_text == existing["normalized_text"]:
            return True, "bsky:normalized_text"

    return False, None


# --- Main Sync Logic ---
def sync_feeds(args):
    logging.info("🔄 Starting sync cycle...")
    dry_run = getattr(args, "dry_run", False)
    bsky_langs = getattr(args, "bsky_langs", None) or DEFAULT_BSKY_LANGS
    bot_locale = bsky_langs_to_playwright_locale(bsky_langs)
    _cache.locale = bot_locale

    if dry_run:
        logging.info("🧪 DRY RUN MODE — no posts will be created on Bluesky.")

    try:
        state = load_state(STATE_PATH)
        state = prune_state(state, max_entries=5000)

        tweets = scrape_tweets_via_playwright(
            args.twitter_username,
            args.twitter_password,
            args.twitter_email,
            args.twitter_handle,
            locale=bot_locale,
        )

        if not tweets:
            logging.warning(
                "⚠️ No tweets found or failed to fetch. "
                "Skipping Bluesky sync for this cycle."
            )
            return

        bsky_client = None
        if not dry_run:
            bsky_client = create_bsky_client(
                args.bsky_base_url,
                args.bsky_handle,
                args.bsky_password,
            )

        recent_bsky_posts = []
        if not dry_run:
            recent_bsky_posts = get_recent_bsky_posts(
                bsky_client,
                args.bsky_handle,
                limit=DEDUPE_BSKY_LIMIT,
            )

        logging.info(
            f"🧠 Loaded {len(recent_bsky_posts)} recent Bluesky posts for duplicate detection."
        )
        logging.info(
            f"🧠 Local state currently tracks "
            f"{len(state.get('posted_tweets', {}))} posted items."
        )

        too_old_cutoff = arrow.utcnow().shift(days=-TWEET_MAX_AGE_DAYS)
        logging.info(f"🕒 Will ignore tweets older than: {too_old_cutoff}")

        candidate_tweets = []
        cheap_candidates = []

        for tweet in reversed(tweets):
            try:
                tweet_time = arrow.get(tweet.created_on)

                if tweet_time < too_old_cutoff:
                    logging.info(f"⏭️ Skipping old tweet from {tweet_time}")
                    continue

                if tweet.is_retweet:
                    logging.info(f"⏭️ Skipping retweet/repost: {tweet.tweet_url}")
                    continue

                canonical_tweet_url = canonicalize_tweet_url(tweet.tweet_url)
                if canonical_tweet_url and canonical_tweet_url in state.get("posted_tweets", {}):
                    logging.info(
                        f"⚡ Early skip due to known tweet URL in local state: "
                        f"{canonical_tweet_url}"
                    )
                    continue

                scraped_text = clean_post_text(tweet.text or "")
                if not scraped_text and not tweet.media:
                    logging.info(f"⏭️ Skipping empty/blank tweet from {tweet_time}")
                    continue

                cheap_candidates.append((tweet, tweet_time, canonical_tweet_url))

            except Exception as e:
                logging.warning(f"⚠️ Failed during cheap prefilter: {e}")

        logging.info(f"⚡ {len(cheap_candidates)} tweets remain after cheap prefilter.")

        with httpx.Client() as resolve_http_client:
            for tweet, tweet_time, canonical_tweet_url in cheap_candidates:
                try:
                    (
                        full_clean_text,
                        resolved_primary_external_url,
                    ) = build_effective_tweet_text(tweet, resolve_http_client)

                    normalized_text = normalize_post_text(full_clean_text)

                    if not normalized_text and not tweet.media:
                        logging.info(
                            f"⏭️ Skipping empty/blank tweet after enrichment from {tweet_time}"
                        )
                        continue

                    ordered_non_x_urls = extract_ordered_non_x_urls(full_clean_text)

                    canonical_non_x_urls = set()
                    if resolved_primary_external_url:
                        canonical_non_x_urls.add(canonicalize_url(resolved_primary_external_url))

                    for raw_url in ordered_non_x_urls:
                        if not is_tco_domain(raw_url) and not is_x_or_twitter_domain(raw_url):
                            canonical_non_x_urls.add(canonicalize_url(raw_url))

                    primary_non_x_url = None
                    if resolved_primary_external_url:
                        primary_non_x_url = resolved_primary_external_url
                    else:
                        primary_non_x_url = extract_first_visible_non_x_url(full_clean_text)
                        if not primary_non_x_url and ordered_non_x_urls:
                            primary_non_x_url = ordered_non_x_urls[0]

                    has_video = any(
                        getattr(m, "type", None) == "video" for m in (tweet.media or [])
                    )
                    has_photo = any(
                        getattr(m, "type", None) == "photo" for m in (tweet.media or [])
                    )

                    raw_text = choose_final_visible_text(
                        full_clean_text,
                        primary_non_x_url=primary_non_x_url,
                        prefer_full_text_without_url=False,
                    )

                    media_fingerprint = build_media_fingerprint(tweet)
                    text_media_key = build_text_media_key(normalized_text, media_fingerprint)

                    candidate = {
                        "tweet": tweet,
                        "tweet_time": tweet_time,
                        "raw_text": raw_text,
                        "full_clean_text": full_clean_text,
                        "normalized_text": normalized_text,
                        "media_fingerprint": media_fingerprint,
                        "text_media_key": text_media_key,
                        "canonical_tweet_url": canonical_tweet_url,
                        "canonical_non_x_urls": canonical_non_x_urls,
                        "ordered_non_x_urls": ordered_non_x_urls,
                        "primary_non_x_url": primary_non_x_url,
                        "resolved_primary_external_url": resolved_primary_external_url,
                        "looks_like_title_plus_url": looks_like_title_plus_url_post(
                            full_clean_text
                        ),
                        "has_video": has_video,
                        "has_photo": has_photo,
                        "tweet_id": extract_tweet_id(tweet.tweet_url),
                        "resolved_video_url": None,
                        "resolved_video_hash": None,
                    }

                    is_dup_state, reason_state = candidate_matches_state(candidate, state)
                    if is_dup_state:
                        logging.info(
                            f"⏭️ Skipping candidate due to local state duplicate "
                            f"match on: {reason_state}"
                        )
                        continue

                    is_dup_bsky, reason_bsky = candidate_matches_existing_bsky(
                        candidate, recent_bsky_posts
                    )
                    if is_dup_bsky:
                        logging.info(
                            f"⏭️ Skipping candidate due to recent Bluesky duplicate "
                            f"match on: {reason_bsky}"
                        )
                        continue

                    candidate_tweets.append(candidate)

                except Exception as e:
                    logging.warning(f"⚠️ Failed to prepare candidate tweet: {e}")

        logging.info(
            f"📬 {len(candidate_tweets)} tweets remain after duplicate filtering."
        )

        # Pre-resolve video URLs in isolated contexts
        if candidate_tweets:
            with sync_playwright() as p_pre:
                pre_browser = p_pre.chromium.launch(
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled"],
                )
                try:
                    for c in candidate_tweets:
                        if not c.get("has_video"):
                            continue
                        t = c["tweet"]
                        tid = c.get("tweet_id")
                        if not t.tweet_url or not tid:
                            continue
                        c["resolved_video_url"] = extract_video_url_from_tweet_page_isolated(
                            pre_browser,
                            t.tweet_url,
                            tweet_id=tid,
                            locale=bot_locale,
                        )
                finally:
                    pre_browser.close()

        if not candidate_tweets:
            logging.info("✅ No new tweets need posting after duplicate comparison.")
            return

        new_posts = 0
        browser_state_file = "twitter_browser_state.json"

        with sync_playwright() as p, httpx.Client() as media_http_client:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context_kwargs = {
                "user_agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/145.0.7632.6 Safari/537.36"
                ),
                "viewport": {"width": 1920, "height": 1080},
                "locale": bot_locale,
            }
            if os.path.exists(browser_state_file):
                context_kwargs["storage_state"] = browser_state_file

            browser_context = browser.new_context(**context_kwargs)

            for candidate in candidate_tweets:
                tweet = candidate["tweet"]
                tweet_time = candidate["tweet_time"]
                raw_text = candidate["raw_text"]
                full_clean_text = candidate["full_clean_text"]

                logging.info(
                    f"📝 {'[DRY RUN] Would post' if dry_run else 'Posting'} "
                    f"missing tweet from {tweet_time} to Bluesky..."
                )

                if dry_run:
                    logging.info(f"  📄 Text: {raw_text[:200]}")
                    logging.info(
                        f"  🔗 Primary external URL: "
                        f"{candidate.get('resolved_primary_external_url', 'None')}"
                    )
                    logging.info(f"  🃏 Card URL: {getattr(tweet, 'card_url', 'None')}")
                    logging.info(f"  🎬 Has video: {candidate.get('has_video', False)}")
                    logging.info(f"  🖼️ Has photo: {candidate.get('has_photo', False)}")
                    logging.info(f"  🔁 Is retweet: {getattr(tweet, 'is_retweet', False)}")

                    remember_posted_tweet(
                        state,
                        candidate,
                        bsky_uri=f"dry-run:{arrow.utcnow().isoformat()}",
                    )
                    save_state(state, STATE_PATH)
                    new_posts += 1
                    continue

                link_meta_for_alt: dict = {}
                if candidate.get("resolved_primary_external_url"):
                    try:
                        link_meta_for_alt = fetch_link_metadata(
                            candidate["resolved_primary_external_url"],
                            media_http_client,
                        )
                    except Exception:
                        pass

                rich_text = make_rich(raw_text)
                dynamic_alt = build_dynamic_alt(
                    full_clean_text,
                    link_title=link_meta_for_alt.get("title"),
                )

                image_embeds = []
                video_embed = None
                external_embed = None
                media_upload_failures = []

                has_video = candidate.get("has_video", False)

                # --- Video processing ---
                if has_video:
                    video_media = next(
                        (
                            m for m in (tweet.media or [])
                            if getattr(m, "type", None) == "video"
                        ),
                        None,
                    )

                    if video_media:
                        if not tweet.tweet_url:
                            logging.warning(
                                "⚠️ Tweet has video marker but no tweet URL. Skipping video."
                            )
                            media_upload_failures.append("video:no_tweet_url")
                        else:
                            temp_video_base = make_unique_video_temp_base(tweet.tweet_url)
                            temp_video_path = f"{temp_video_base}.mp4"

                            try:
                                tweet_id = candidate.get("tweet_id")
                                real_video_url = candidate.get("resolved_video_url")
                                if not real_video_url:
                                    logging.warning(
                                        f"⚠️ Could not resolve playable video URL "
                                        f"for {tweet.tweet_url}"
                                    )
                                    media_upload_failures.append(
                                        f"video:resolve_failed:{tweet.tweet_url}"
                                    )
                                else:
                                    cropped_video_path = download_and_crop_video(
                                        real_video_url, temp_video_path
                                    )
                                    if not cropped_video_path:
                                        logging.warning(
                                            f"⚠️ Video download/crop failed for {tweet.tweet_url}"
                                        )
                                        media_upload_failures.append(
                                            f"video:crop_failed:{tweet.tweet_url}"
                                        )
                                    else:
                                        video_hash = sha256_file(cropped_video_path)
                                        candidate["resolved_video_hash"] = video_hash
                                        owner = _cache.video_hash_owner.get(video_hash)
                                        if owner and owner != tweet_id:
                                            logging.warning(
                                                f"[tweet_id={tweet_id}] ⚠️ Video hash already "
                                                f"owned by tweet_id={owner}. Rejecting media."
                                            )
                                            media_upload_failures.append(
                                                f"video:hash_owned_by:{owner}"
                                            )
                                            video_blob = None
                                        else:
                                            _cache.video_hash_owner[video_hash] = tweet_id
                                            video_blob = get_blob_from_file(
                                                cropped_video_path, bsky_client
                                            )
                                        if not video_blob:
                                            logging.warning(
                                                f"⚠️ Video upload blob failed for {tweet.tweet_url}"
                                            )
                                            media_upload_failures.append(
                                                f"video:upload_failed:{tweet.tweet_url}"
                                            )
                                        else:
                                            video_embed = build_video_embed(
                                                video_blob, dynamic_alt
                                            )
                                            if not video_embed:
                                                media_upload_failures.append(
                                                    f"video:embed_failed:{tweet.tweet_url}"
                                                )
                            finally:
                                remove_file_quietly(temp_video_path)
                                remove_file_quietly(f"{temp_video_base}_source.mp4")
                                remove_file_quietly(f"{temp_video_base}_trimmed.mp4")
                                remove_file_quietly(f"{temp_video_base}_compressed.mp4")

                # ----------------------------------------------------------------
                # FIX: warn if video failed, but ALWAYS attempt photo uploads
                # independently — this is the core fix for photo-only tweets.
                # ----------------------------------------------------------------
                if has_video and not video_embed:
                    logging.warning(
                        "⚠️ Tweet contains video, but video could not be posted. "
                        "Skipping video — will still attempt photos if present."
                    )

                # Always collect photos regardless of video outcome
                if tweet.media:
                    for media in tweet.media:
                        if media.type == "photo":
                            blob = get_blob_from_url(
                                media.media_url_https,
                                bsky_client,
                                media_http_client,
                            )
                            if blob:
                                image_embeds.append(
                                    models.AppBskyEmbedImages.Image(
                                        alt=dynamic_alt,
                                        image=blob,
                                    )
                                )
                            else:
                                media_upload_failures.append(
                                    f"photo:{media.media_url_https}"
                                )

                # --- External link card logic ---
                if not video_embed and not image_embeds:
                    candidate_url = candidate.get("resolved_primary_external_url")

                    if candidate_url:
                        if candidate.get("looks_like_title_plus_url"):
                            logging.info(
                                f"🔗 Detected title+URL post style. "
                                f"Using resolved URL for external card: {candidate_url}"
                            )
                        else:
                            logging.info(
                                f"🔗 Using resolved first external URL for "
                                f"external card: {candidate_url}"
                            )

                        external_embed = build_external_link_embed(
                            candidate_url,
                            bsky_client,
                            media_http_client,
                            fallback_title="Link",
                            prefetched_metadata=link_meta_for_alt or None,
                        )

                        if external_embed:
                            logging.info(
                                f"✅ Built external link card for URL: {candidate_url}"
                            )
                        else:
                            logging.info(
                                f"ℹ️ Could not build external link card metadata "
                                f"for URL: {candidate_url}"
                            )

                try:
                    post_result = None
                    post_mode = "text"

                    if video_embed:
                        post_result = send_post_with_retry(
                            bsky_client,
                            text=rich_text,
                            embed=video_embed,
                            langs=bsky_langs,
                        )
                        post_mode = "video"
                    elif image_embeds:
                        embed = models.AppBskyEmbedImages.Main(images=image_embeds)
                        post_result = send_post_with_retry(
                            bsky_client,
                            text=rich_text,
                            embed=embed,
                            langs=bsky_langs,
                        )
                        post_mode = f"images:{len(image_embeds)}"
                    elif external_embed:
                        post_result = send_post_with_retry(
                            bsky_client,
                            text=rich_text,
                            embed=external_embed,
                            langs=bsky_langs,
                        )
                        post_mode = "external_link_card"
                    else:
                        post_result = send_post_with_retry(
                            bsky_client,
                            text=rich_text,
                            langs=bsky_langs,
                        )
                        post_mode = "text_only"

                    bsky_uri = getattr(post_result, "uri", None)

                    remember_posted_tweet(state, candidate, bsky_uri=bsky_uri)
                    state = prune_state(state, max_entries=5000)
                    save_state(state, STATE_PATH)

                    recent_bsky_posts.insert(
                        0,
                        {
                            "uri": bsky_uri,
                            "text": raw_text,
                            "normalized_text": candidate["normalized_text"],
                            "canonical_non_x_urls": candidate["canonical_non_x_urls"],
                            "media_fingerprint": candidate["media_fingerprint"],
                            "text_media_key": candidate["text_media_key"],
                            "created_at": arrow.utcnow().isoformat(),
                        },
                    )
                    recent_bsky_posts = recent_bsky_posts[:DEDUPE_BSKY_LIMIT]

                    new_posts += 1

                    if media_upload_failures:
                        logging.warning(
                            f"✅ Posted tweet to Bluesky with degraded media "
                            f"mode ({post_mode}). "
                            f"Failed media items: {media_upload_failures}"
                        )
                    else:
                        logging.info(
                            f"✅ Posted new tweet to Bluesky with mode "
                            f"{post_mode}: {raw_text}"
                        )

                    time.sleep(5)

                except Exception as e:
                    logging.error(f"❌ Failed to post tweet to Bluesky: {e}")

            browser.close()

        logging.info(f"✅ Sync complete. Posted {new_posts} new updates.")

    except Exception as e:
        logging.error(f"❌ Error during sync cycle: {e}")


def main():
    load_dotenv()

    parser = argparse.ArgumentParser(description="Twitter to Bluesky Sync")
    parser.add_argument("--twitter-username", help="Your Twitter login username")
    parser.add_argument("--twitter-password", help="Your Twitter login password")
    parser.add_argument("--twitter-email", help="Your Twitter email for security challenges")
    parser.add_argument("--twitter-handle", help="The Twitter account to scrape")
    parser.add_argument("--bsky-handle", help="Your Bluesky handle")
    parser.add_argument("--bsky-password", help="Your Bluesky app password")
    parser.add_argument(
        "--bsky-base-url",
        help="Bluesky/ATProto PDS base URL, e.g. https://eurosky.social",
    )
    parser.add_argument(
        "--bsky-langs",
        help="Comma-separated language codes for Bluesky posts (default: ca)",
        default=None,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Simulate sync without posting to Bluesky. Logs what would be posted.",
    )

    args = parser.parse_args()

    # Resolve credentials: CLI args take priority, then env vars.
    # Prefer .env / environment variables to avoid exposing secrets in process list.
    args.twitter_username = args.twitter_username or os.getenv("TWITTER_USERNAME")
    args.twitter_password = args.twitter_password or os.getenv("TWITTER_PASSWORD")
    args.twitter_email = args.twitter_email or os.getenv("TWITTER_EMAIL")
    args.bsky_handle = args.bsky_handle or os.getenv("BSKY_HANDLE")
    args.bsky_password = args.bsky_password or os.getenv("BSKY_APP_PASSWORD")
    args.twitter_handle = (
        args.twitter_handle
        or os.getenv("TWITTER_HANDLE")
        or args.twitter_username
    )
    args.bsky_base_url = (
        args.bsky_base_url if args.bsky_base_url else DEFAULT_BSKY_BASE_URL
    )

    # --- Language handling: CLI > env > default (Catalan) ---
    raw_langs = args.bsky_langs or os.getenv("BSKY_LANGS")
    if raw_langs:
        args.bsky_langs = [lang.strip() for lang in raw_langs.split(",") if lang.strip()]
        logging.info(f"🌍 Using configured Bluesky languages: {args.bsky_langs}")
    else:
        args.bsky_langs = DEFAULT_BSKY_LANGS
        logging.info(f"🌍 Using default Bluesky languages: {args.bsky_langs}")

    missing_args = []
    if not args.twitter_username:
        missing_args.append("--twitter-username / TWITTER_USERNAME")
    if not args.twitter_password:
        missing_args.append("--twitter-password / TWITTER_PASSWORD")
    if not args.bsky_handle:
        missing_args.append("--bsky-handle / BSKY_HANDLE")
    if not args.bsky_password:
        missing_args.append("--bsky-password / BSKY_APP_PASSWORD")

    if missing_args:
        logging.error(
            f"❌ Missing credentials! You forgot to provide: {', '.join(missing_args)}"
        )
        return

    logging.info(f"🤖 Bot started. Will check @{args.twitter_handle}")
    logging.info(f"🌍 Posting destination base URL: {args.bsky_base_url}")

    if args.dry_run:
        logging.info("🧪 DRY RUN MODE ENABLED — no posts will be created.")

    reset_caches()
    sync_feeds(args)
    logging.info("🤖 Bot finished.")


if __name__ == "__main__":
    main()