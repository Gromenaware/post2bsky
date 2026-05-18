import argparse
import arrow
import fastfeedparser
import logging
import re
import httpx
import time
import random
import charset_normalizer
import sys
import os
import io
import json
import hashlib
import html
from dataclasses import dataclass
from typing import Optional, List, Set, Dict, Any, Tuple
from urllib.parse import urlparse, urlunparse
from atproto import Client, client_utils, models
from bs4 import BeautifulSoup

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    Image = None
    PIL_AVAILABLE = False


# ============================================================
# Config
# ============================================================
DEFAULT_STATE_PATH = "rss2bsky_state.json"
DEFAULT_COOLDOWN_STATE_PATH = "rss2bsky_cooldowns.json"


@dataclass(frozen=True)
class LimitsConfig:
    dedupe_bsky_limit: int = 30
    bsky_text_max_length: int = 300

    external_thumb_max_bytes: int = 750 * 1024
    external_thumb_target_bytes: int = 500 * 1024
    external_thumb_max_dimension: int = 1000
    external_thumb_min_jpeg_quality: int = 35

    state_max_entries: int = 5000


@dataclass(frozen=True)
class RetryConfig:
    blob_upload_max_retries: int = 3
    blob_upload_base_delay: int = 8
    blob_upload_max_delay: int = 120
    blob_transient_error_retries: int = 2
    blob_transient_error_delay: int = 10
    post_retry_delay_seconds: int = 2

    # Login hardening
    login_max_attempts: int = 5
    login_base_delay_seconds: int = 2
    login_max_delay_seconds: int = 600
    login_jitter_seconds: float = 1.5


@dataclass(frozen=True)
class CooldownConfig:
    default_post_cooldown_seconds: int = 120
    default_thumb_cooldown_seconds: int = 60


@dataclass(frozen=True)
class NetworkConfig:
    http_timeout: int = 20


@dataclass(frozen=True)
class AppConfig:
    limits: LimitsConfig = LimitsConfig()
    retry: RetryConfig = RetryConfig()
    cooldown: CooldownConfig = CooldownConfig()
    network: NetworkConfig = NetworkConfig()


# ============================================================
# Local models
# ============================================================
@dataclass
class EntryCandidate:
    item: Any
    title_text: str
    normalized_title: str
    canonical_link: Optional[str]
    published_at: Optional[str]
    published_arrow: Any
    entry_fingerprint: str
    post_text_variants: List[str]


@dataclass
class RecentBskyPost:
    uri: Optional[str]
    text: str
    normalized_text: str
    canonical_non_x_urls: Set[str]
    created_at: Optional[str]


@dataclass
class RunResult:
    published_count: int
    stopped_reason: Optional[str] = None


# ============================================================
# Logging
# ============================================================
def setup_logging() -> None:
    logging.basicConfig(
        format="%(asctime)s %(message)s",
        level=logging.INFO,
        stream=sys.stdout
    )


# ============================================================
# State + cooldown
# ============================================================
def default_state() -> Dict[str, Any]:
    return {
        "version": 1,
        "posted_entries": {},
        "posted_by_bsky_uri": {},
        "updated_at": None,
    }


def load_state(state_path: str) -> Dict[str, Any]:
    if not os.path.exists(state_path):
        logging.info(f"🧠 No state file found at {state_path}. Starting with empty state.")
        return default_state()

    try:
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)

        if not isinstance(state, dict):
            logging.warning("⚠️ State file invalid. Reinitializing.")
            return default_state()

        state.setdefault("version", 1)
        state.setdefault("posted_entries", {})
        state.setdefault("posted_by_bsky_uri", {})
        state.setdefault("updated_at", None)
        return state

    except Exception as e:
        logging.warning(f"⚠️ Could not load state file {state_path}: {e}. Reinitializing.")
        return default_state()


def save_state(state: Dict[str, Any], state_path: str) -> None:
    try:
        state["updated_at"] = arrow.utcnow().isoformat()
        temp_path = f"{state_path}.tmp"

        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)

        os.replace(temp_path, state_path)
        logging.info(f"💾 State saved to {state_path}")

    except Exception as e:
        logging.error(f"❌ Failed to save state file {state_path}: {e}")


def prune_state(state: Dict[str, Any], max_entries: int = 5000) -> Dict[str, Any]:
    posted_entries = state.get("posted_entries", {})

    if len(posted_entries) <= max_entries:
        return state

    sortable = []
    for key, record in posted_entries.items():
        posted_at = record.get("posted_at") or ""
        sortable.append((key, posted_at))

    sortable.sort(key=lambda x: x[1], reverse=True)
    keep_keys = {key for key, _ in sortable[:max_entries]}

    state["posted_entries"] = {k: v for k, v in posted_entries.items() if k in keep_keys}
    state["posted_by_bsky_uri"] = {
        uri: key for uri, key in state.get("posted_by_bsky_uri", {}).items() if key in keep_keys
    }
    return state


def remember_posted_entry(state: Dict[str, Any], candidate: EntryCandidate, posted_text: str, bsky_uri: Optional[str] = None) -> None:
    canonical_link = candidate.canonical_link
    fallback_key = f"fp:{candidate.entry_fingerprint}"
    state_key = canonical_link or fallback_key

    record = {
        "canonical_link": canonical_link,
        "title_text": candidate.title_text,
        "normalized_title": candidate.normalized_title,
        "entry_fingerprint": candidate.entry_fingerprint,
        "post_text": posted_text,
        "published_at": candidate.published_at,
        "bsky_uri": bsky_uri,
        "posted_at": arrow.utcnow().isoformat(),
    }

    state["posted_entries"][state_key] = record

    if bsky_uri:
        state["posted_by_bsky_uri"][bsky_uri] = state_key


def candidate_matches_state(candidate: EntryCandidate, state: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    canonical_link = candidate.canonical_link
    entry_fingerprint = candidate.entry_fingerprint
    normalized_title = candidate.normalized_title
    posted_entries = state.get("posted_entries", {})

    if canonical_link and canonical_link in posted_entries:
        return True, "state:canonical_link"

    for _, record in posted_entries.items():
        if record.get("entry_fingerprint") == entry_fingerprint:
            return True, "state:entry_fingerprint"

    for _, record in posted_entries.items():
        if record.get("normalized_title") == normalized_title:
            if not canonical_link or record.get("canonical_link") == canonical_link:
                return True, "state:normalized_title"

    return False, None


def default_cooldown_state() -> Dict[str, Any]:
    return {
        "version": 1,
        "post_creation_cooldown_until": 0,
        "thumb_upload_cooldown_until": 0,
        "updated_at": None,
    }


def load_cooldown_state(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return default_cooldown_state()

    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)

        if not isinstance(state, dict):
            return default_cooldown_state()

        state.setdefault("version", 1)
        state.setdefault("post_creation_cooldown_until", 0)
        state.setdefault("thumb_upload_cooldown_until", 0)
        state.setdefault("updated_at", None)
        return state
    except Exception as e:
        logging.warning(f"⚠️ Could not load cooldown state {path}: {e}")
        return default_cooldown_state()


def save_cooldown_state(state: Dict[str, Any], path: str) -> None:
    try:
        state["updated_at"] = arrow.utcnow().isoformat()
        temp_path = f"{path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(temp_path, path)
    except Exception as e:
        logging.warning(f"⚠️ Could not save cooldown state {path}: {e}")


def get_global_post_cooldown_until(cooldown_path: str) -> int:
    state = load_cooldown_state(cooldown_path)
    return int(state.get("post_creation_cooldown_until", 0) or 0)


def get_global_thumb_cooldown_until(cooldown_path: str) -> int:
    state = load_cooldown_state(cooldown_path)
    return int(state.get("thumb_upload_cooldown_until", 0) or 0)


def is_global_post_cooldown_active(cooldown_path: str) -> bool:
    return int(time.time()) < get_global_post_cooldown_until(cooldown_path)


def is_global_thumb_cooldown_active(cooldown_path: str) -> bool:
    return int(time.time()) < get_global_thumb_cooldown_until(cooldown_path)


def set_global_post_cooldown_until(reset_ts: int, cooldown_path: str) -> int:
    state = load_cooldown_state(cooldown_path)
    current = int(state.get("post_creation_cooldown_until", 0) or 0)
    if reset_ts > current:
        state["post_creation_cooldown_until"] = int(reset_ts)
        save_cooldown_state(state, cooldown_path)
    return int(load_cooldown_state(cooldown_path).get("post_creation_cooldown_until", 0) or 0)


def set_global_thumb_cooldown_until(reset_ts: int, cooldown_path: str) -> int:
    state = load_cooldown_state(cooldown_path)
    current = int(state.get("thumb_upload_cooldown_until", 0) or 0)
    if reset_ts > current:
        state["thumb_upload_cooldown_until"] = int(reset_ts)
        save_cooldown_state(state, cooldown_path)
    return int(load_cooldown_state(cooldown_path).get("thumb_upload_cooldown_until", 0) or 0)


def format_cooldown_until(ts: int) -> str:
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))


def check_post_cooldown_or_log(cooldown_path: str) -> bool:
    if is_global_post_cooldown_active(cooldown_path):
        reset_str = format_cooldown_until(get_global_post_cooldown_until(cooldown_path))
        logging.warning(f"🟡 === BSKY POST SKIPPED: GLOBAL COOLDOWN === Active until {reset_str}")
        return True
    return False


def check_thumb_cooldown_or_log(cooldown_path: str) -> bool:
    if is_global_thumb_cooldown_active(cooldown_path):
        reset_str = format_cooldown_until(get_global_thumb_cooldown_until(cooldown_path))
        logging.info(f"🖼️ Skipping external thumbnail upload due to active cooldown until {reset_str}")
        return True
    return False


# ============================================================
# Text + URL utils
# ============================================================
def fix_encoding(text: str) -> str:
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def desescapar_unicode(text: str) -> str:
    try:
        return html.unescape(text)
    except Exception:
        return text


def is_html(text: str) -> bool:
    return bool(re.search(r'<.*?>', text or ""))


def strip_trailing_url_punctuation(url: str) -> str:
    if not url:
        return url
    return re.sub(r"[\s…\.,;:!?)\]\"']+$", "", url.strip())


def canonicalize_url(url: str):
    if not url:
        return None
    url = html.unescape(url.strip())
    url = strip_trailing_url_punctuation(url)
    try:
        parsed = urlparse(url)
        parsed = parsed._replace(fragment="")
        return urlunparse(parsed)
    except Exception:
        return url


def clean_whitespace(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\r", "\n")
    lines = [line.strip() for line in text.splitlines()]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_text(text: str) -> str:
    text = clean_whitespace(text)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


def process_title(title: str) -> str:
    if is_html(title):
        title_text = BeautifulSoup(title, "html.parser").get_text().strip()
    else:
        title_text = (title or "").strip()
    title_text = desescapar_unicode(title_text)
    title_text = fix_encoding(title_text)
    title_text = clean_whitespace(title_text)
    return title_text


def build_post_text_variants(title_text: str, link: str, max_length: int = 300):
    title_text = clean_whitespace(title_text)
    link = canonicalize_url(link) or link or ""

    variants = []
    seen = set()

    def add_variant(text: str):
        cleaned = clean_whitespace(text)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            variants.append(cleaned)

    # Variant 1: title + link (if fits whole)
    if title_text and link:
        full = f"{title_text}\n\n{link}"
        if len(full) <= max_length:
            add_variant(full)

    # Variant 2: full title only (no truncation)
    if title_text:
        if len(title_text) <= max_length:
            add_variant(title_text)
        else:
            truncated = title_text[:max_length].rstrip(" .")
            add_variant(truncated)

    # Variant 3: truncated title + link (when full title+link doesn't fit)
    if title_text and link:
        full = f"{title_text}\n\n{link}"
        if len(full) > max_length:
            reserve = len(link) + 2
            available = max_length - reserve
            if available > 20:
                truncated_title = title_text[:available].rstrip(" .")
                add_variant(f"{truncated_title}\n\n{link}")

    # Variant 4: link only (when no title)
    if link and not title_text:
        add_variant(link)

    return variants


def is_x_or_twitter_domain(url: str) -> bool:
    try:
        hostname = (urlparse(url).hostname or "").lower()
        return hostname in {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com", "t.co"}
    except Exception:
        return False


def extract_urls_from_text(text: str):
    if not text:
        return []
    return re.findall(r"https?://[^\s]+", text)


def extract_non_x_urls_from_text(text: str):
    urls = extract_urls_from_text(text)
    result = []
    for url in urls:
        cleaned = strip_trailing_url_punctuation(url)
        if cleaned and not is_x_or_twitter_domain(cleaned):
            result.append(cleaned)
    return result


def build_entry_fingerprint(normalized_title: str, canonical_link: str) -> str:
    raw = f"{normalized_title}||{canonical_link or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def make_rich(content: str):
    text_builder = client_utils.TextBuilder()
    content = clean_whitespace(content)
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

            if cleaned_word.startswith("http://") or cleaned_word.startswith("https://"):
                text_builder.link(cleaned_word, cleaned_word)
                trailing = word[len(cleaned_word):]
                if trailing:
                    text_builder.text(trailing)

            elif cleaned_word.startswith("#") and len(cleaned_word) > 1:
                tag_name = cleaned_word[1:].rstrip(".,;:!?)'\"")
                if tag_name:
                    text_builder.tag(cleaned_word, tag_name)
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


# ============================================================
# Error helpers
# ============================================================
def get_rate_limit_reset_timestamp(error_obj):
    # 1) direct headers
    try:
        headers = getattr(error_obj, "headers", None) or {}
        now_ts = int(time.time())

        retry_after = headers.get("retry-after") or headers.get("Retry-After")
        if retry_after:
            return now_ts + int(retry_after)

        x_after = headers.get("x-ratelimit-after") or headers.get("X-RateLimit-After")
        if x_after:
            return now_ts + int(x_after)

        reset_value = headers.get("ratelimit-reset") or headers.get("RateLimit-Reset")
        if reset_value:
            return int(reset_value)
    except Exception:
        pass

    # 2) headers nested in response
    try:
        response = getattr(error_obj, "response", None)
        headers = getattr(response, "headers", None) or {}
        now_ts = int(time.time())

        retry_after = headers.get("retry-after") or headers.get("Retry-After")
        if retry_after:
            return now_ts + int(retry_after)

        x_after = headers.get("x-ratelimit-after") or headers.get("X-RateLimit-After")
        if x_after:
            return now_ts + int(x_after)

        reset_value = headers.get("ratelimit-reset") or headers.get("RateLimit-Reset")
        if reset_value:
            return int(reset_value)
    except Exception:
        pass

    # 3) fallback parse
    text = repr(error_obj)

    m = re.search(r"'retry-after': '(\d+)'", text, re.IGNORECASE)
    if m:
        return int(time.time()) + int(m.group(1))

    m = re.search(r"'x-ratelimit-after': '(\d+)'", text, re.IGNORECASE)
    if m:
        return int(time.time()) + int(m.group(1))

    m = re.search(r"'ratelimit-reset': '(\d+)'", text, re.IGNORECASE)
    if m:
        return int(m.group(1))

    return None


def is_rate_limited_error(error_obj) -> bool:
    error_text = str(error_obj)
    repr_text = repr(error_obj)
    return (
        "429" in error_text or
        "429" in repr_text or
        "RateLimitExceeded" in error_text or
        "RateLimitExceeded" in repr_text or
        "Too Many Requests" in error_text or
        "Too Many Requests" in repr_text
    )


def is_transient_blob_error(error_obj) -> bool:
    error_text = repr(error_obj)
    transient_signals = [
        "InvokeTimeoutError", "ReadTimeout", "WriteTimeout", "TimeoutException",
        "RemoteProtocolError", "ConnectError", "503", "502", "504"
    ]
    return any(signal in error_text for signal in transient_signals)


def is_timeout_error(error_obj) -> bool:
    text = repr(error_obj)
    return any(signal in text for signal in ["InvokeTimeoutError", "ReadTimeout", "WriteTimeout", "TimeoutException"])


def is_probable_length_error(exc) -> bool:
    text = repr(exc)
    signals = [
        "TextTooLong", "text too long", "Invalid app.bsky.feed.post record",
        "string too long", "maxLength", "length", "grapheme too big"
    ]
    return any(signal.lower() in text.lower() for signal in signals)


def is_auth_error(error_obj) -> bool:
    text = repr(error_obj).lower()
    return (
        "401" in text
        or "403" in text
        or "invalid identifier or password" in text
        or "authenticationrequired" in text
        or "invalidtoken" in text
    )


def is_network_error(error_obj) -> bool:
    text = repr(error_obj)
    signals = [
        "ConnectError", "RemoteProtocolError", "ReadTimeout", "WriteTimeout",
        "TimeoutException", "503", "502", "504", "ConnectionResetError"
    ]
    return any(s in text for s in signals)


def activate_post_creation_cooldown_from_error(error_obj, cooldown_path: str, cfg: AppConfig) -> int:
    reset_ts = get_rate_limit_reset_timestamp(error_obj)
    if not reset_ts:
        reset_ts = int(time.time()) + cfg.cooldown.default_post_cooldown_seconds
    final_ts = set_global_post_cooldown_until(reset_ts, cooldown_path)
    logging.error(f"🛑 === BSKY POST STOPPED: RATE LIMITED === Posting disabled until {format_cooldown_until(final_ts)}")
    return final_ts


def activate_thumb_upload_cooldown_from_error(error_obj, cooldown_path: str, cfg: AppConfig) -> int:
    reset_ts = get_rate_limit_reset_timestamp(error_obj)
    if not reset_ts:
        reset_ts = int(time.time()) + cfg.cooldown.default_thumb_cooldown_seconds
    final_ts = set_global_thumb_cooldown_until(reset_ts, cooldown_path)
    logging.warning(f"🖼️ Thumbnail uploads disabled until {format_cooldown_until(final_ts)}.")
    return final_ts


def get_rate_limit_wait_seconds(error_obj, default_delay: int, cfg: AppConfig) -> int:
    reset_ts = get_rate_limit_reset_timestamp(error_obj)
    if reset_ts:
        now_ts = int(time.time())
        wait_seconds = max(reset_ts - now_ts + 1, default_delay)
        return min(wait_seconds, cfg.retry.blob_upload_max_delay)
    return default_delay


# ============================================================
# Bluesky helpers
# ============================================================
def extract_urls_from_facets(record) -> List[str]:
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


def get_recent_bsky_posts(client: Client, handle: str, limit: int) -> List[RecentBskyPost]:
    recent_posts: List[RecentBskyPost] = []
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
                normalized = normalize_text(text)

                urls = []
                urls.extend(extract_non_x_urls_from_text(text))
                urls.extend(extract_urls_from_facets(record))

                canonical_non_x_urls: Set[str] = set()
                for url in urls:
                    if not is_x_or_twitter_domain(url):
                        c = canonicalize_url(url)
                        if c:
                            canonical_non_x_urls.add(c)

                recent_posts.append(RecentBskyPost(
                    uri=getattr(item.post, "uri", None),
                    text=text,
                    normalized_text=normalized,
                    canonical_non_x_urls=canonical_non_x_urls,
                    created_at=getattr(record, "created_at", None),
                ))

            except Exception as e:
                logging.debug(f"Skipping one Bluesky feed item during dedupe fetch: {e}")
    except Exception as e:
        logging.warning(f"⚠️ Could not fetch recent Bluesky posts for duplicate detection: {e}")

    return recent_posts


def candidate_matches_existing_bsky(candidate: EntryCandidate, recent_bsky_posts: List[RecentBskyPost]) -> Tuple[bool, Optional[str]]:
    candidate_link = candidate.canonical_link
    candidate_title_normalized = candidate.normalized_title

    for existing in recent_bsky_posts:
        if candidate_link and candidate_link in existing.canonical_non_x_urls:
            return True, "bsky:canonical_link"

        if candidate_title_normalized and candidate_title_normalized in existing.normalized_text:
            if not candidate_link or candidate_link in existing.canonical_non_x_urls:
                return True, "bsky:title_plus_link"

    return False, None


def upload_blob_with_retry(
    client: Client,
    binary_data: bytes,
    cfg: AppConfig,
    media_label: str = "media",
    optional: bool = False,
    cooldown_on_rate_limit: bool = False,
    cooldown_path: Optional[str] = None,
):
    last_exception = None
    transient_attempts = 0

    for attempt in range(1, cfg.retry.blob_upload_max_retries + 1):
        try:
            result = client.upload_blob(binary_data)
            return result.blob

        except Exception as e:
            last_exception = e

            if is_rate_limited_error(e):
                if cooldown_on_rate_limit and cooldown_path:
                    activate_thumb_upload_cooldown_from_error(e, cooldown_path, cfg)

                if optional and cooldown_on_rate_limit:
                    logging.warning(
                        f"🟡 Optional blob upload rate-limited for {media_label}. "
                        f"Skipping remaining retries and omitting optional media."
                    )
                    return None

                backoff_delay = min(
                    cfg.retry.blob_upload_base_delay * (2 ** (attempt - 1)),
                    cfg.retry.blob_upload_max_delay
                )
                wait_seconds = get_rate_limit_wait_seconds(e, backoff_delay, cfg)

                if attempt < cfg.retry.blob_upload_max_retries:
                    logging.warning(
                        f"⏳ Blob upload rate-limited for {media_label}. "
                        f"Retry {attempt}/{cfg.retry.blob_upload_max_retries} after {wait_seconds}s."
                    )
                    time.sleep(wait_seconds)
                    continue

                logging.warning(f"⚠️ Exhausted blob upload retries for {media_label}: {repr(e)}")
                break

            if is_transient_blob_error(e) and transient_attempts < cfg.retry.blob_transient_error_retries:
                transient_attempts += 1
                wait_seconds = cfg.retry.blob_transient_error_delay * transient_attempts
                logging.warning(
                    f"⏳ Transient blob upload failure for {media_label}: {repr(e)}. "
                    f"Retry {transient_attempts}/{cfg.retry.blob_transient_error_retries} after {wait_seconds}s."
                )
                time.sleep(wait_seconds)
                continue

            logging.warning(f"⚠️ Could not upload {media_label}: {repr(e)}")
            return None

    logging.warning(f"⚠️ Could not upload {media_label}: {repr(last_exception)}")
    return None


def try_send_post_with_variants(client: Client, text_variants: List[str], embed, post_langs: List[str], cooldown_path: str, cfg: AppConfig):
    if is_global_post_cooldown_active(cooldown_path):
        reset_str = format_cooldown_until(get_global_post_cooldown_until(cooldown_path))
        raise RuntimeError(f"Posting skipped because global post cooldown is active until {reset_str}")

    last_exception = None

    for idx, variant in enumerate(text_variants, start=1):
        try:
            if is_global_post_cooldown_active(cooldown_path):
                reset_str = format_cooldown_until(get_global_post_cooldown_until(cooldown_path))
                raise RuntimeError(f"Posting skipped because global post cooldown is active until {reset_str}")

            logging.info(
                f"📝 Trying post text variant {idx}/{len(text_variants)} "
                f"(length={len(variant)} chars)"
            )
            rich_text = make_rich(variant)
            result = client.send_post(text=rich_text, embed=embed, langs=post_langs)
            return result, variant

        except Exception as e:
            last_exception = e
            logging.warning(f"⚠️ Post variant {idx} failed: {repr(e)}")

            if is_rate_limited_error(e):
                activate_post_creation_cooldown_from_error(e, cooldown_path, cfg)
                raise

            if is_timeout_error(e):
                raise

            if not is_probable_length_error(e):
                raise

    if last_exception:
        raise last_exception
    raise RuntimeError("No text variants available to post.")


# ============================================================
# Embeds / metadata / image compression
# ============================================================
def compress_external_thumb_to_limit(image_bytes: bytes, cfg: AppConfig):
    if not PIL_AVAILABLE:
        return None

    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            img = img.convert("RGB")

            width, height = img.size
            max_dim = max(width, height)

            if max_dim > cfg.limits.external_thumb_max_dimension:
                scale = cfg.limits.external_thumb_max_dimension / max_dim
                new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
                img = img.resize(new_size, Image.LANCZOS)
                logging.info(f"🖼️ Resized external thumb to {new_size[0]}x{new_size[1]}")

            best_so_far = None

            for quality in [78, 70, 62, 54, 46, 40, cfg.limits.external_thumb_min_jpeg_quality]:
                out = io.BytesIO()
                img.save(out, format="JPEG", quality=quality, optimize=True, progressive=True)
                data = out.getvalue()

                logging.info(f"🖼️ External thumb candidate size at JPEG quality {quality}: {len(data) / 1024:.2f} KB")

                if len(data) <= cfg.limits.external_thumb_target_bytes:
                    return data

                if len(data) <= cfg.limits.external_thumb_max_bytes:
                    best_so_far = data

            if best_so_far and len(best_so_far) <= cfg.limits.external_thumb_max_bytes:
                return best_so_far

            for target_dim in [900, 800, 700, 600, 500]:
                resized = img.copy()
                width, height = resized.size
                max_dim = max(width, height)

                if max_dim > target_dim:
                    scale = target_dim / max_dim
                    new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
                    resized = resized.resize(new_size, Image.LANCZOS)

                for quality in [54, 46, 40, cfg.limits.external_thumb_min_jpeg_quality]:
                    out = io.BytesIO()
                    resized.save(out, format="JPEG", quality=quality, optimize=True, progressive=True)
                    data = out.getvalue()

                    logging.info(
                        f"🖼️ External thumb resized to <= {target_dim}px at quality {quality}: "
                        f"{len(data) / 1024:.2f} KB"
                    )

                    if len(data) <= cfg.limits.external_thumb_target_bytes:
                        return data

                    if len(data) <= cfg.limits.external_thumb_max_bytes:
                        best_so_far = data

            if best_so_far and len(best_so_far) <= cfg.limits.external_thumb_max_bytes:
                return best_so_far

    except Exception as e:
        logging.warning(f"⚠️ Could not compress external thumbnail: {repr(e)}")

    return None


def fetch_link_metadata(url: str, http_client: httpx.Client, cfg: AppConfig):
    try:
        r = http_client.get(url, timeout=cfg.network.http_timeout, follow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        title = (soup.find("meta", property="og:title") or soup.find("title"))
        desc = (soup.find("meta", property="og:description") or soup.find("meta", attrs={"name": "description"}))
        image = (soup.find("meta", property="og:image") or soup.find("meta", attrs={"name": "twitter:image"}))

        return {
            "title": title["content"] if title and title.has_attr("content") else (title.text.strip() if title and title.text else ""),
            "description": desc["content"] if desc and desc.has_attr("content") else "",
            "image": image["content"] if image and image.has_attr("content") else None,
        }
    except Exception as e:
        logging.warning(f"⚠️ Could not fetch link metadata for {url}: {e}")
        return {}


def get_external_thumb_blob_from_url(image_url: str, client: Client, http_client: httpx.Client, cooldown_path: str, cfg: AppConfig):
    if check_thumb_cooldown_or_log(cooldown_path):
        return None

    try:
        r = http_client.get(image_url, timeout=cfg.network.http_timeout, follow_redirects=True)
        if r.status_code != 200:
            logging.warning(f"⚠️ Could not fetch external thumb {image_url}: HTTP {r.status_code}")
            return None

        content = r.content
        if not content:
            logging.warning(f"⚠️ Could not fetch external thumb {image_url}: empty body")
            return None

        logging.info(f"🖼️ Downloaded external thumb {image_url} ({len(content) / 1024:.2f} KB)")

        upload_bytes = compress_external_thumb_to_limit(content, cfg)
        if not upload_bytes:
            logging.warning("⚠️ Could not prepare compressed external thumbnail. Omitting thumbnail.")
            return None

        logging.info(f"🖼️ Final external thumb upload size: {len(upload_bytes) / 1024:.2f} KB")

        blob = upload_blob_with_retry(
            client=client,
            binary_data=upload_bytes,
            cfg=cfg,
            media_label=f"external-thumb:{image_url}",
            optional=True,
            cooldown_on_rate_limit=True,
            cooldown_path=cooldown_path
        )
        if blob:
            logging.info("✅ External thumbnail uploaded successfully")
            return blob

        logging.warning("⚠️ External thumbnail upload failed. Will omit thumbnail.")
        return None

    except Exception as e:
        logging.warning(f"⚠️ Could not fetch/upload external thumb {image_url}: {repr(e)}")
        return None


def build_external_link_embed(url: str, fallback_title: str, client: Client, http_client: httpx.Client, cooldown_path: str, cfg: AppConfig):
    link_metadata = fetch_link_metadata(url, http_client, cfg)

    thumb_blob = None
    if link_metadata.get("image"):
        thumb_blob = get_external_thumb_blob_from_url(link_metadata["image"], client, http_client, cooldown_path, cfg)
        if thumb_blob:
            logging.info("✅ External link card thumbnail prepared successfully")
        else:
            logging.info("ℹ️ External link card will be posted without thumbnail")
    else:
        logging.info("ℹ️ No og:image found for external link card")

    if link_metadata.get("title") or link_metadata.get("description") or thumb_blob:
        return models.AppBskyEmbedExternal.Main(
            external=models.AppBskyEmbedExternal.External(
                uri=url,
                title=link_metadata.get("title") or fallback_title or "Enllaç",
                description=link_metadata.get("description") or "",
                thumb=thumb_blob,
            )
        )
    return None


# ============================================================
# Feed helpers
# ============================================================
def parse_entry_time(item):
    candidates = [getattr(item, "published", None), getattr(item, "updated", None), getattr(item, "pubDate", None)]
    for candidate in candidates:
        if candidate:
            try:
                return arrow.get(candidate)
            except Exception:
                continue
    return None


def fetch_feed_content(feed_url: str, http_client: httpx.Client, cfg: AppConfig) -> str:
    response = http_client.get(feed_url, timeout=cfg.network.http_timeout, follow_redirects=True)
    response.raise_for_status()

    try:
        result = charset_normalizer.from_bytes(response.content).best()
        if not result or not hasattr(result, "text"):
            raise ValueError("Could not detect feed encoding.")
        return result.text
    except ValueError:
        logging.warning("⚠️ Could not detect feed encoding with charset_normalizer. Trying latin-1.")
        try:
            return response.content.decode("latin-1")
        except UnicodeDecodeError:
            logging.warning("⚠️ Could not decode with latin-1. Trying utf-8 with ignored errors.")
            return response.content.decode("utf-8", errors="ignore")


def build_candidates_from_feed(feed, max_length: int = 300) -> List[EntryCandidate]:
    candidates: List[EntryCandidate] = []

    for item in getattr(feed, "entries", []):
        try:
            title_text = process_title(getattr(item, "title", "") or "")
            link = canonicalize_url(getattr(item, "link", "") or "")
            published_at = parse_entry_time(item)

            if not title_text and not link:
                logging.info("⏭️ Skipping feed item with no usable title and no link.")
                continue

            normalized_title = normalize_text(title_text)
            entry_fingerprint = build_entry_fingerprint(normalized_title, link)

            candidates.append(EntryCandidate(
                item=item,
                title_text=title_text,
                normalized_title=normalized_title,
                canonical_link=link,
                published_at=published_at.isoformat() if published_at else None,
                published_arrow=published_at,
                entry_fingerprint=entry_fingerprint,
                post_text_variants=build_post_text_variants(title_text, link, max_length=max_length),
            ))

        except Exception as e:
            logging.warning(f"⚠️ Failed to prepare feed entry candidate: {e}")

    candidates.sort(key=lambda c: c.published_arrow or arrow.get(0))
    return candidates


# ============================================================
# Login
# ============================================================
def login_with_backoff(
    client: Client,
    bsky_username: str,
    bsky_password: str,
    service_url: str,
    cooldown_path: str,
    cfg: AppConfig
) -> bool:
    if check_post_cooldown_or_log(cooldown_path):
        return False

    max_attempts = cfg.retry.login_max_attempts
    base_delay = cfg.retry.login_base_delay_seconds
    max_delay = cfg.retry.login_max_delay_seconds
    jitter_max = max(cfg.retry.login_jitter_seconds, 0.0)

    for attempt in range(1, max_attempts + 1):
        try:
            if check_post_cooldown_or_log(cooldown_path):
                return False

            logging.info(
                f"🔐 Attempting login to server: {service_url} "
                f"with user: {bsky_username} (attempt {attempt}/{max_attempts})"
            )
            client.login(bsky_username, bsky_password)
            logging.info(f"✅ Login successful for user: {bsky_username}")
            return True

        except Exception as e:
            logging.exception("❌ Login exception")

            if is_rate_limited_error(e):
                if attempt < max_attempts:
                    wait_seconds = get_rate_limit_wait_seconds(e, base_delay, cfg)
                    wait_seconds = min(wait_seconds, max_delay) + random.uniform(0, jitter_max)
                    logging.warning(
                        f"⏳ Login rate-limited. Retrying in {wait_seconds:.1f}s "
                        f"(attempt {attempt}/{max_attempts})"
                    )
                    time.sleep(wait_seconds)
                    continue

                activate_post_creation_cooldown_from_error(e, cooldown_path, cfg)
                return False

            if is_auth_error(e):
                logging.error("❌ Authentication failed (bad handle/password/app-password).")
                return False

            if attempt < max_attempts and (is_network_error(e) or is_timeout_error(e)):
                delay = min(base_delay * attempt, max_delay) + random.uniform(0, jitter_max)
                logging.warning(f"⏳ Transient login failure. Retrying in {delay:.1f}s...")
                time.sleep(delay)
                continue

            if attempt < max_attempts:
                delay = min(base_delay * attempt, max_delay) + random.uniform(0, jitter_max)
                logging.warning(f"⏳ Login retry in {delay:.1f}s...")
                time.sleep(delay)
                continue

            return False

    return False


# ============================================================
# Orchestration
# ============================================================
def run_once(
    rss_feed: str,
    bsky_handle: str,
    bsky_username: str,
    bsky_password: str,
    service_url: str,
    post_langs: List[str],
    state_path: str,
    cooldown_path: str,
    cfg: AppConfig,
    max_posts: int = 5,
    max_age_days: int = 7,          # ← NEW: 0 = disabled
) -> RunResult:
    if not PIL_AVAILABLE:
        logging.warning("🟡 Pillow is not installed. External card thumbnail compression is disabled.")

    logging.info(f"🌍 Posting language(s): {post_langs}")

    # ── Age-filter cutoff ────────────────────────────────────────
    if max_age_days > 0:
        cutoff = arrow.utcnow().shift(days=-max_age_days)
        logging.info(
            f"📅 Age filter active: skipping entries published before "
            f"{cutoff.isoformat()} (max_age_days={max_age_days})"
        )
    else:
        cutoff = None
        logging.info("📅 Age filter disabled (max_age_days=0).")
    # ────────────────────────────────────────────────────────────

    if check_post_cooldown_or_log(cooldown_path):
        return RunResult(published_count=0, stopped_reason="global_post_cooldown_active")

    client = Client(base_url=service_url)

    logged_in = login_with_backoff(
        client=client,
        bsky_username=bsky_username,
        bsky_password=bsky_password,
        service_url=service_url,
        cooldown_path=cooldown_path,
        cfg=cfg
    )
    if not logged_in:
        if check_post_cooldown_or_log(cooldown_path):
            return RunResult(published_count=0, stopped_reason="global_post_cooldown_active")
        return RunResult(published_count=0, stopped_reason="login_failed")

    state = load_state(state_path)
    recent_bsky_posts = get_recent_bsky_posts(client, bsky_handle, limit=cfg.limits.dedupe_bsky_limit)

    logging.info(f"🧠 Loaded {len(recent_bsky_posts)} recent Bluesky posts for duplicate detection.")
    logging.info(f"🧠 Local state currently tracks {len(state.get('posted_entries', {}))} posted items.")

    with httpx.Client() as http_client:
        feed_content = fetch_feed_content(rss_feed, http_client, cfg)
        feed = fastfeedparser.parse(feed_content)
        candidates = build_candidates_from_feed(feed, max_length=cfg.limits.bsky_text_max_length)

        logging.info(f"📰 Prepared {len(candidates)} feed entry candidates for duplicate comparison.")

        entries_to_post: List[EntryCandidate] = []

        for candidate in candidates:

            # ── Age filter ───────────────────────────────────────────
            if cutoff is not None:
                pub = candidate.published_arrow
                if pub is None:
                    logging.info(
                        f"⏭️ Skipping entry with no publication date "
                        f"(age filter active, max_age_days={max_age_days}): "
                        f"{candidate.canonical_link or candidate.title_text}"
                    )
                    continue
                if pub < cutoff:
                    logging.info(
                        f"⏭️ Skipping old entry published {pub.isoformat()} "
                        f"(cutoff: {cutoff.isoformat()}): "
                        f"{candidate.canonical_link or candidate.title_text}"
                    )
                    continue
            # ────────────────────────────────────────────────────────

            # ── Deduplication: local state ───────────────────────────
            is_dup_state, reason_state = candidate_matches_state(candidate, state)
            if is_dup_state:
                logging.info(f"⏭️ Skipping candidate due to local state duplicate match on: {reason_state}")
                continue

            # ── Deduplication: recent Bluesky posts ──────────────────
            is_dup_bsky, reason_bsky = candidate_matches_existing_bsky(candidate, recent_bsky_posts)
            if is_dup_bsky:
                logging.info(f"⏭️ Skipping candidate due to recent Bluesky duplicate match on: {reason_bsky}")
                continue

            entries_to_post.append(candidate)

        logging.info(f"📬 {len(entries_to_post)} entries remain after duplicate filtering.")

        if len(entries_to_post) > max_posts:
            logging.info(
                f"🔢 max-posts cap is {max_posts}: will publish at most {max_posts} "
                f"of {len(entries_to_post)} entries this run."
            )

        if not entries_to_post:
            logging.info("ℹ️ Execution finished: no new entries to publish.")
            return RunResult(published_count=0)

        if check_post_cooldown_or_log(cooldown_path):
            return RunResult(published_count=0, stopped_reason="global_post_cooldown_active")

        published = 0

        for candidate in entries_to_post:

            if published >= max_posts:
                logging.info(f"🔢 === MAX POSTS REACHED === Stopping after {published} posts (limit: {max_posts}).")
                break

            if is_global_post_cooldown_active(cooldown_path):
                reset_str = format_cooldown_until(get_global_post_cooldown_until(cooldown_path))
                logging.error(f"🛑 === BSKY POST STOPPED: GLOBAL COOLDOWN === Skipping remaining entries until {reset_str}")
                break

            title_text = candidate.title_text
            canonical_link = candidate.canonical_link
            text_variants = candidate.post_text_variants

            logging.info(f"📰 Preparing to post RSS entry: {canonical_link or title_text}")
            logging.info(f"🚀 === BSKY POST START === {canonical_link or title_text}")

            embed = None
            if canonical_link:
                embed = build_external_link_embed(
                    canonical_link,
                    fallback_title=title_text or "Enllaç",
                    client=client,
                    http_client=http_client,
                    cooldown_path=cooldown_path,
                    cfg=cfg
                )

            try:
                post_result, posted_text = try_send_post_with_variants(
                    client=client,
                    text_variants=text_variants,
                    embed=embed,
                    post_langs=post_langs,
                    cooldown_path=cooldown_path,
                    cfg=cfg
                )

                bsky_uri = getattr(post_result, "uri", None)

                remember_posted_entry(state, candidate, posted_text=posted_text, bsky_uri=bsky_uri)
                state = prune_state(state, max_entries=cfg.limits.state_max_entries)
                save_state(state, state_path)

                recent_bsky_posts.insert(0, RecentBskyPost(
                    uri=bsky_uri,
                    text=posted_text,
                    normalized_text=normalize_text(posted_text),
                    canonical_non_x_urls={canonical_link} if canonical_link else set(),
                    created_at=arrow.utcnow().isoformat(),
                ))
                recent_bsky_posts = recent_bsky_posts[:cfg.limits.dedupe_bsky_limit]

                published += 1
                logging.info(f"✅ === BSKY POST SUCCESS === {canonical_link or title_text}")
                logging.info(f"🎉 Posted RSS entry to Bluesky: {canonical_link or title_text}")
                time.sleep(cfg.retry.post_retry_delay_seconds)

            except Exception as e:
                if is_rate_limited_error(e):
                    reset_str = format_cooldown_until(get_global_post_cooldown_until(cooldown_path))
                    logging.error(f"❌ === BSKY POST FAILED === {canonical_link or title_text}")
                    logging.error(f"🛑 === BSKY POST STOPPED: RATE LIMITED === Ending publish loop until {reset_str}")
                    break

                if "global post cooldown is active" in str(e).lower():
                    reset_str = format_cooldown_until(get_global_post_cooldown_until(cooldown_path))
                    logging.warning(f"🟡 === BSKY POST SKIPPED: GLOBAL COOLDOWN === {canonical_link or title_text}")
                    logging.warning(f"🛑 === BSKY POST STOPPED: GLOBAL COOLDOWN === Ending publish loop until {reset_str}")
                    break

                if is_timeout_error(e):
                    logging.error(f"⏰ === BSKY POST FAILED === {canonical_link or title_text} :: timeout")
                    break

                logging.exception(f"❌ === BSKY POST FAILED === {canonical_link or title_text}")

    if published > 0:
        logging.info(f"🎉 Execution finished: published {published} new entries to Bluesky.")
    else:
        logging.info("ℹ️ Execution finished: no new entries were published.")

    return RunResult(published_count=published)


# ============================================================
# CLI
# ============================================================
def main():
    setup_logging()

    parser = argparse.ArgumentParser(description="Post RSS to Bluesky with shared cooldown tracking.")
    parser.add_argument("rss_feed", help="RSS feed URL")
    parser.add_argument("bsky_handle", help="Bluesky handle")
    parser.add_argument("bsky_username", help="Bluesky username")
    parser.add_argument("bsky_app_password", help="Bluesky app password")
    parser.add_argument("--service", default="https://bsky.social", help="Bluesky server URL")
    parser.add_argument(
        "--lang",
        default="ca",
        help="Comma-separated language codes for Bluesky posts (default: ca). Example: ca,es",
    )
    parser.add_argument("--state-path", default=DEFAULT_STATE_PATH, help="Path to local JSON state file")
    parser.add_argument("--cooldown-path", default=DEFAULT_COOLDOWN_STATE_PATH, help="Path to shared cooldown JSON state file")
    parser.add_argument("--max-posts", type=int, default=5, help="Max new posts to publish per run (default: 5)")
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=7,
        help="Skip entries older than this many days (default: 7). Use 0 to disable the age filter.",
    )
    args = parser.parse_args()

    post_langs = [lang.strip() for lang in args.lang.split(",") if lang.strip()]
    if not post_langs:
        post_langs = ["ca"]

    logging.info(f"🌍 Using configured Bluesky language(s): {post_langs}")

    cfg = AppConfig()

    run_once(
        rss_feed=args.rss_feed,
        bsky_handle=args.bsky_handle,
        bsky_username=args.bsky_username,
        bsky_password=args.bsky_app_password,
        service_url=args.service,
        post_langs=post_langs,
        state_path=args.state_path,
        cooldown_path=args.cooldown_path,
        cfg=cfg,
        max_posts=args.max_posts,
        max_age_days=args.max_age_days,     # ← NEW
    )


if __name__ == "__main__":
    main()
