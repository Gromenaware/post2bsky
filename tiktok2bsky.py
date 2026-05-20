#!/usr/bin/env python3
"""
tiktok2bsky.py
──────────────
Scrapes recent videos from a public TikTok profile and cross-posts
them to a Bluesky account.

Usage:
    python tiktok2bsky.py \
        --tiktok-handle    jijantesfc \
        --bsky-handle      jijantesfc.bsky.social \
        --bsky-app-password xxxx-xxxx-xxxx-xxxx \
        --bsky-base-url    https://bsky.social \
        --bsky-langs       es \
        --cookies-path     tiktok_cookies.json
"""

import argparse
import json
import logging
import os
import random
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import arrow
import httpx
from atproto import Client
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright


# ─────────────────────────────────────────────────────────────────────────────
#  playwright-stealth: detect installed version
#  v2.x (2.0.x) has a completely unstable API — we skip stealth for it and
#  rely on browser launch args instead. v1.x stealth_sync works fine.
# ─────────────────────────────────────────────────────────────────────────────
_STEALTH_SYNC = None   # will hold the stealth_sync callable if v1.x is present

try:
    from playwright_stealth import stealth_sync as _stealth_sync_import
    _STEALTH_SYNC = _stealth_sync_import
except ImportError:
    # v2.x is installed but its API is too unstable to use reliably —
    # browser launch args provide equivalent protection for our use case
    pass


# ─────────────────────────────────────────────────────────────────────────────
#  Logging
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("tiktok2bsky.log", encoding="utf-8"),
    ],
    level=logging.INFO,
)

# ─────────────────────────────────────────────────────────────────────────────
#  Constants & defaults
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_BSKY_BASE_URL   = "https://bsky.social"
DEFAULT_BSKY_LANGS      = ["es"]
TIKTOK_COOKIES_PATH     = "tiktok_cookies.json"

STATE_FILE              = "tiktok2bsky_state.json"
STATE_MAX_ENTRIES       = 5000

SCRAPE_VIDEO_LIMIT      = 30
VIDEO_MAX_AGE_DAYS      = 3

VIDEO_MAX_DURATION_S    = 179       # Bluesky hard limit is 180s

# ── Bluesky login retry config (ported from twitter2bsky.py) ─────────────────
BSKY_LOGIN_MAX_RETRIES          = 6
BSKY_LOGIN_BASE_DELAY           = 15.0
BSKY_LOGIN_MAX_DELAY            = 600.0
BSKY_LOGIN_JITTER_MAX           = 5.0
BSKY_LOGIN_RATE_LIMIT_DELAY     = 90.0   # minimum wait on 429
BSKY_LOGIN_RATE_LIMIT_MAX_DELAY = 600.0  # maximum wait on 429

# ── Bluesky upload retry config ───────────────────────────────────────────────
BSKY_UPLOAD_MAX_RETRIES = 5
BSKY_UPLOAD_BASE_DELAY  = 10.0
BSKY_UPLOAD_MAX_DELAY   = 120.0
BSKY_UPLOAD_JITTER_MAX  = 5.0

# ── Playwright scraping config ────────────────────────────────────────────────
PLAYWRIGHT_TIMEOUT_MS   = 30_000
PLAYWRIGHT_SLOW_MO      = 50
PLAYWRIGHT_MAX_RELOADS  = 3

# ── TikTok selectors ──────────────────────────────────────────────────────────
TIKTOK_VIDEO_GRID_SEL    = '[data-e2e="user-post-item-list"]'
TIKTOK_VIDEO_ITEM_SEL    = '[data-e2e="user-post-item"]'
TIKTOK_BANNER_SELS       = [
    '[id*="banner"]',
    '[class*="banner"]',
    '[data-e2e="recommend-modal-close"]',
    'button:has-text("Rechazar")',
    'button:has-text("Reject")',
    'button:has-text("Accept")',
    'button:has-text("Aceptar")',
    '[aria-label="Close"]',
    '[aria-label="Cerrar"]',
]
TIKTOK_COOKIE_MODAL_SELS = [
    'button:has-text("Decline all")',
    'button:has-text("Rechazar todo")',
    'button:has-text("Reject all")',
    'button:has-text("Accept all")',
    'button:has-text("Aceptar todo")',
    '[class*="cookie"] button',
    '[id*="cookie"] button',
]


# ─────────────────────────────────────────────────────────────────────────────
#  Dynamic video size limit based on PDS
# ─────────────────────────────────────────────────────────────────────────────
def get_video_size_limit(bsky_base_url: str) -> int:
    """
    bsky.social supports ~50 MB blobs. Third-party PDS instances
    typically cap at 10–20 MB. Use a conservative 10 MB for
    anything that isn't the official PDS.
    """
    if "bsky.social" in (bsky_base_url or ""):
        return 20 * 1024 * 1024   # 20 MB — official PDS
    return 10 * 1024 * 1024       # 10 MB — safe for third-party PDS


# ─────────────────────────────────────────────────────────────────────────────
#  State management
# ─────────────────────────────────────────────────────────────────────────────
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
                logging.info(
                    f"📂 Loaded state: {len(state.get('posted', {}))} entries."
                )
                return state
        except Exception as e:
            logging.warning(f"⚠️ Could not load state file: {e}. Starting fresh.")
    return {"posted": {}}


def save_state(state: dict):
    posted = state.get("posted", {})
    if len(posted) > STATE_MAX_ENTRIES:
        sorted_keys = sorted(
            posted.keys(),
            key=lambda k: posted[k].get("posted_at", ""),
        )
        for old_key in sorted_keys[: len(posted) - STATE_MAX_ENTRIES]:
            del posted[old_key]
        state["posted"] = posted
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logging.error(f"❌ Could not save state: {e}")


def is_already_posted(video_id: str, state: dict) -> bool:
    return video_id in state.get("posted", {})


def mark_as_posted(video_id: str, state: dict, meta: dict = None):
    state.setdefault("posted", {})[video_id] = {
        "posted_at": arrow.utcnow().isoformat(),
        **(meta or {}),
    }
    save_state(state)


# ─────────────────────────────────────────────────────────────────────────────
#  Cookie helpers
# ─────────────────────────────────────────────────────────────────────────────
def load_cookies_from_file(path: str) -> list:
    if not os.path.exists(path):
        logging.warning(f"⚠️ Cookie file not found: {path}")
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            cookies = json.load(f)
        logging.info(f"🍪 Loaded {len(cookies)} cookies from {path}")
        return cookies
    except Exception as e:
        logging.warning(f"⚠️ Could not load cookies from {path}: {e}")
        return []


def inject_cookies_into_context(context, cookies: list):
    if not cookies:
        return
    playwright_cookies = []
    for c in cookies:
        entry = {
            "name":     c.get("name", ""),
            "value":    c.get("value", ""),
            "domain":   c.get("domain", ".tiktok.com"),
            "path":     c.get("path", "/"),
            "secure":   c.get("secure", False),
            "httpOnly": c.get("httpOnly", False),
            "sameSite": c.get("sameSite", "None"),
        }
        exp = c.get("expirationDate") or c.get("expires")
        if exp and float(exp) > 0:
            entry["expires"] = float(exp)
        playwright_cookies.append(entry)
    try:
        context.add_cookies(playwright_cookies)
        logging.info(
            f"🍪 Injected {len(playwright_cookies)} cookies into browser context."
        )
    except Exception as e:
        logging.warning(f"⚠️ Could not inject cookies: {e}")


def convert_json_cookies_to_netscape(json_path: str) -> str | None:
    """
    Convert a JSON cookie file (browser extension format) to a Netscape
    cookie file that yt-dlp can consume. Returns temp file path or None.
    Caller must delete the file when done.
    """
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            cookies = json.load(f)

        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        )
        tmp.write("# Netscape HTTP Cookie File\n")
        tmp.write("# Generated by tiktok2bsky.py\n\n")

        for c in cookies:
            domain      = c.get("domain", ".tiktok.com")
            include_sub = "TRUE" if domain.startswith(".") else "FALSE"
            path        = c.get("path", "/")
            secure      = "TRUE" if c.get("secure", False) else "FALSE"
            expiry      = int(c.get("expirationDate") or c.get("expires") or 0)
            name        = c.get("name", "")
            value       = c.get("value", "")
            tmp.write(
                f"{domain}\t{include_sub}\t{path}\t"
                f"{secure}\t{expiry}\t{name}\t{value}\n"
            )

        tmp.close()
        logging.info(
            f"🍪 Converted {len(cookies)} cookies to Netscape format: {tmp.name}"
        )
        return tmp.name

    except Exception as e:
        logging.warning(
            f"⚠️ Could not convert cookies to Netscape format: "
            f"{type(e).__name__}: {e}"
        )
        return None


# ─────────────────────────────────────────────────────────────────────────────
#  Bluesky error classification (ported from twitter2bsky.py)
# ─────────────────────────────────────────────────────────────────────────────
def _bsky_error_text(error_obj) -> str:
    return repr(error_obj).lower()


def is_rate_limited_error(error_obj) -> bool:
    text = _bsky_error_text(error_obj)
    return (
        "429"                  in text
        or "ratelimitexceeded" in text
        or "too many requests" in text
        or "rate limit"        in text
        or "ratelimit"         in text
    )


def is_auth_error(error_obj) -> bool:
    text = _bsky_error_text(error_obj)
    return (
        "401"                               in text
        or "403"                            in text
        or "invalid identifier"             in text
        or "invalid password"               in text
        or "authenticationrequired"         in text
        or "invalidtoken"                   in text
        or "expiredtoken"                   in text
        or "accounttakedown"                in text
        or "invalid identifier or password" in text
    )


def is_network_error(error_obj) -> bool:
    text = repr(error_obj)
    return any(s in text for s in [
        "ConnectError", "RemoteProtocolError", "ReadTimeout",
        "WriteTimeout", "TimeoutException", "ConnectionResetError",
        "503", "502", "504",
    ])


def is_transient_error(error_obj) -> bool:
    text = repr(error_obj)
    return any(s in text for s in [
        "InvokeTimeoutError", "ReadTimeout", "WriteTimeout",
        "TimeoutException", "RemoteProtocolError", "ConnectError",
        "503", "502", "504",
    ])


def get_rate_limit_wait_seconds(error_obj, default_delay: float) -> float:
    """
    Extract the server-requested wait time from rate-limit error headers.
    Ported from twitter2bsky.py.
    """
    now_ts = int(time.time())

    try:
        headers = getattr(error_obj, "headers", None) or {}
        for key in ("retry-after", "Retry-After"):
            val = headers.get(key)
            if val:
                return min(max(int(val), 1), BSKY_LOGIN_RATE_LIMIT_MAX_DELAY)
        for key in ("x-ratelimit-after", "X-RateLimit-After"):
            val = headers.get(key)
            if val:
                return min(max(int(val), 1), BSKY_LOGIN_RATE_LIMIT_MAX_DELAY)
        for key in ("ratelimit-reset", "RateLimit-Reset"):
            val = headers.get(key)
            if val:
                wait = max(int(val) - now_ts + 2, default_delay)
                return min(wait, BSKY_LOGIN_RATE_LIMIT_MAX_DELAY)
    except Exception:
        pass

    text = repr(error_obj)
    for pattern, is_ts in [
        (r"['\"]retry-after['\"]\s*:\s*['\"](\d+)['\"]",       False),
        (r"['\"]x-ratelimit-after['\"]\s*:\s*['\"](\d+)['\"]", False),
        (r"['\"]ratelimit-reset['\"]\s*:\s*['\"](\d+)['\"]",   True),
        (r"retry.?after[=:\s]+(\d+)",                           False),
    ]:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            val = int(m.group(1))
            if is_ts:
                wait = max(val - now_ts + 2, default_delay)
                return min(wait, BSKY_LOGIN_RATE_LIMIT_MAX_DELAY)
            return min(max(val, 1), BSKY_LOGIN_RATE_LIMIT_MAX_DELAY)

    return default_delay


# ─────────────────────────────────────────────────────────────────────────────
#  Bluesky client — robust login (ported from twitter2bsky.py)
# ─────────────────────────────────────────────────────────────────────────────
def connect_bluesky(handle: str, app_password: str, base_url: str) -> Client:
    """
    Authenticate with Bluesky with full retry logic:
      • 429 / rate-limit  → honour Retry-After header; wait up to 600s
      • auth errors       → fail immediately (retrying won't help)
      • network/transient → exponential backoff with jitter
      • other errors      → exponential backoff with jitter
    """
    logging.info(f"🔐 Connecting Bluesky client → {base_url}")
    client     = Client(base_url=base_url)
    attempt    = 0
    last_error = None

    while attempt < BSKY_LOGIN_MAX_RETRIES:
        attempt += 1
        logging.info(
            f"🔐 Bluesky login attempt {attempt}/{BSKY_LOGIN_MAX_RETRIES} for {handle}"
        )

        try:
            client.login(handle, app_password)
            client.me = client.get_profile(handle)
            logging.info(f"✅ Bluesky login successful as {handle}")
            return client

        except Exception as e:
            last_error = e
            err_detail = f"{type(e).__name__}: {e}"

            # Auth errors — no point retrying
            if is_auth_error(e):
                logging.error(
                    f"❌ Bluesky login auth error (will not retry): {err_detail}"
                )
                raise

            # Rate-limited (429)
            if is_rate_limited_error(e):
                raw_wait = get_rate_limit_wait_seconds(e, BSKY_LOGIN_RATE_LIMIT_DELAY)
                jitter   = random.uniform(0.0, BSKY_LOGIN_JITTER_MAX)
                wait     = min(raw_wait + jitter, BSKY_LOGIN_RATE_LIMIT_MAX_DELAY)
                logging.warning(
                    f"⏳ Bluesky login rate-limited (attempt {attempt}/"
                    f"{BSKY_LOGIN_MAX_RETRIES}). "
                    f"Waiting {wait:.1f}s (server requested {raw_wait:.0f}s)."
                )
                if attempt < BSKY_LOGIN_MAX_RETRIES:
                    time.sleep(wait)
                continue

            # Network / transient errors
            if is_network_error(e) or is_transient_error(e):
                delay  = min(
                    BSKY_LOGIN_BASE_DELAY * (2 ** (attempt - 1)),
                    BSKY_LOGIN_MAX_DELAY,
                )
                jitter = random.uniform(0.0, BSKY_LOGIN_JITTER_MAX)
                wait   = delay + jitter
                logging.warning(
                    f"⚠️ Bluesky login network/transient error "
                    f"(attempt {attempt}/{BSKY_LOGIN_MAX_RETRIES}): "
                    f"{err_detail}. Retrying in {wait:.1f}s."
                )
                if attempt < BSKY_LOGIN_MAX_RETRIES:
                    time.sleep(wait)
                continue

            # Unknown errors
            delay  = min(
                BSKY_LOGIN_BASE_DELAY * (2 ** (attempt - 1)),
                BSKY_LOGIN_MAX_DELAY,
            )
            jitter = random.uniform(0.0, BSKY_LOGIN_JITTER_MAX)
            wait   = delay + jitter
            logging.warning(
                f"⚠️ Bluesky login failed "
                f"(attempt {attempt}/{BSKY_LOGIN_MAX_RETRIES}): "
                f"{err_detail}. Retrying in {wait:.1f}s."
            )
            if attempt < BSKY_LOGIN_MAX_RETRIES:
                time.sleep(wait)

    logging.error(
        f"❌ Bluesky login failed after {BSKY_LOGIN_MAX_RETRIES} attempts. "
        f"Last error: {type(last_error).__name__}: {last_error}"
    )
    raise RuntimeError(
        f"Bluesky login failed after {BSKY_LOGIN_MAX_RETRIES} attempts: {last_error}"
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Video helpers
# ─────────────────────────────────────────────────────────────────────────────
def get_video_duration(path: str) -> float:
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True, text=True, timeout=15,
        )
        return float(result.stdout.strip())
    except Exception as e:
        logging.warning(f"⚠️ ffprobe failed for {path}: {e}")
        return 0.0


def compress_video(
    input_path: str,
    output_path: str,
    max_duration: int = VIDEO_MAX_DURATION_S,
    max_size_bytes: int = None,
) -> bool:
    if max_size_bytes is None:
        max_size_bytes = 20 * 1024 * 1024

    try:
        duration = get_video_duration(input_path)
        if duration <= 0:
            logging.error(
                f"❌ compress_video: invalid duration={duration} for {input_path}"
            )
            return False

        trim_to     = min(duration, max_duration)
        target_bits = max_size_bytes * 8 * 0.85
        total_kbps  = int(target_bits / trim_to / 1000)
        audio_kbps  = 96
        video_kbps  = max(200, total_kbps - audio_kbps)

        logging.info(
            f"🎬 Compressing: duration={duration:.1f}s → trim={trim_to:.1f}s, "
            f"video_bitrate={video_kbps}k "
            f"(target ≤ {max_size_bytes // 1024 // 1024}MB)"
        )

        cmd = [
            "ffmpeg", "-y",
            "-i", input_path,
            "-t", str(trim_to),
            "-vf", (
                "scale='min(1280,iw)':'min(720,ih)'"
                ":force_original_aspect_ratio=decrease,"
                "pad=ceil(iw/2)*2:ceil(ih/2)*2"
            ),
            "-c:v", "libx264",
            "-b:v", f"{video_kbps}k",
            "-maxrate", f"{video_kbps}k",
            "-bufsize", f"{video_kbps * 2}k",
            "-c:a", "aac",
            "-b:a", f"{audio_kbps}k",
            "-movflags", "+faststart",
            "-pix_fmt", "yuv420p",
            output_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

        if result.returncode != 0:
            logging.error(f"❌ ffmpeg failed:\n{result.stderr}")
            return False

        final_size = os.path.getsize(output_path)
        if final_size > max_size_bytes:
            logging.error(
                f"❌ Compressed file still too large: "
                f"{final_size / 1024 / 1024:.1f} MB > "
                f"{max_size_bytes / 1024 / 1024:.0f} MB. Skipping."
            )
            return False

        logging.info(
            f"✅ Compressed video: {final_size / 1024 / 1024:.1f} MB → {output_path}"
        )
        return True

    except Exception as e:
        logging.error(f"❌ compress_video error: {type(e).__name__}: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
#  yt-dlp helpers
# ─────────────────────────────────────────────────────────────────────────────
def get_best_impersonation_target():
    """
    Ask yt-dlp directly which impersonation targets are actually available
    in the current environment. Returns the best ImpersonateTarget object,
    or None if none are available.

    This is the only reliable method — curl_cffi's BrowserType enum values
    change between versions and do not map 1:1 to yt-dlp's target names.
    """
    try:
        import yt_dlp
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            targets = getattr(ydl, "_impersonate_targets", None)
            if not targets:
                logging.warning(
                    "⚠️ yt-dlp: no impersonation targets available in this environment."
                )
                return None

            available_strs = []
            for t in targets.keys():
                client  = getattr(t, "client", None) or str(t)
                version = getattr(t, "version", None)
                label   = f"{client}-{version}" if version else str(client)
                available_strs.append((label.lower(), t))

            logging.info(
                f"🎭 yt-dlp available impersonation targets: "
                f"{[s for s, _ in available_strs]}"
            )

            # Prefer highest-versioned chrome, then anything else
            chrome_targets = sorted(
                [(s, t) for s, t in available_strs if "chrome" in s],
                key=lambda x: x[0],
                reverse=True,
            )
            if chrome_targets:
                best_label, best_target = chrome_targets[0]
                logging.info(f"🎭 Selected impersonation target: {best_label}")
                return best_target

            best_label, best_target = available_strs[0]
            logging.info(f"🎭 Selected impersonation target (fallback): {best_label}")
            return best_target

    except Exception as e:
        logging.warning(
            f"⚠️ Could not determine yt-dlp impersonation targets: "
            f"{type(e).__name__}: {e}"
        )
    return None


def fetch_video_metadata_ytdlp(
    url: str,
    netscape_cookies_path: str = None,
) -> dict:
    """
    Fetch metadata (title, description, timestamp, uploader) for a single
    TikTok video URL using yt-dlp without downloading the video file.

    TikTok captions (the text the creator wrote) live in the 'description'
    field of yt-dlp's info dict. 'title' is a shorter auto-generated label.

    Returns a dict with keys: description, title, timestamp, uploader.
    All values default to empty string / None on failure.
    """
    import yt_dlp

    impersonate = get_best_impersonation_target()

    ydl_opts = {
        "quiet":         True,
        "no_warnings":   True,
        "skip_download": True,
    }
    if netscape_cookies_path and os.path.exists(netscape_cookies_path):
        ydl_opts["cookiefile"] = netscape_cookies_path
    if impersonate is not None:
        ydl_opts["impersonate"] = impersonate

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if not info:
            return {}

        raw_desc  = (info.get("description") or "").strip()
        raw_title = (info.get("title") or "").strip()

        # Prefer description (full caption with hashtags) over title
        description = raw_desc or raw_title

        logging.info(
            f"📝 Fetched metadata for {url}: "
            f"description={description[:80]!r}"
            f"{'...' if len(description) > 80 else ''}"
        )

        return {
            "description": description,
            "title":       raw_title,
            "timestamp":   info.get("timestamp"),
            "uploader":    info.get("uploader") or info.get("channel") or "",
        }

    except Exception as e:
        logging.warning(
            f"⚠️ Could not fetch metadata for {url}: {type(e).__name__}: {e}"
        )
        return {}


def download_video_ytdlp(
    url: str,
    output_path: str,
    netscape_cookies_path: str = None,
) -> bool:
    """
    Download a TikTok video using yt-dlp with browser impersonation.
    Accepts a Netscape-format cookie file path (not JSON).
    """
    impersonate = get_best_impersonation_target()

    ydl_opts = {
        "outtmpl":             output_path,
        "format":              "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "quiet":               False,
        "no_warnings":         False,
        "merge_output_format": "mp4",
    }
    if netscape_cookies_path and os.path.exists(netscape_cookies_path):
        ydl_opts["cookiefile"] = netscape_cookies_path
    if impersonate is not None:
        ydl_opts["impersonate"] = impersonate

    try:
        import yt_dlp
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        if os.path.exists(output_path) and os.path.getsize(output_path) > 50 * 1024:
            size_mb = os.path.getsize(output_path) / 1024 / 1024
            logging.info(f"✅ yt-dlp download OK: {size_mb:.1f} MB")
            return True

        logging.warning(
            f"⚠️ yt-dlp output too small or missing: {output_path} "
            f"({os.path.getsize(output_path) if os.path.exists(output_path) else 0} bytes)"
        )
        return False

    except Exception as e:
        logging.error(f"❌ yt-dlp download failed for {url}: {type(e).__name__}: {e}")
        return False


def download_video(
    url: str,
    output_path: str,
    netscape_cookies_path: str = None,
) -> bool:
    logging.info(f"⬇️  Downloading: {url}")
    return download_video_ytdlp(
        url, output_path, netscape_cookies_path=netscape_cookies_path
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Bluesky upload
# ─────────────────────────────────────────────────────────────────────────────
def upload_video_to_bluesky(
    client: Client,
    video_path: str,
    video_id: str,
) -> object | None:
    size_mb = os.path.getsize(video_path) / 1024 / 1024
    logging.info(f"⬆️  Uploading to Bluesky ({size_mb:.1f} MB)...")

    with open(video_path, "rb") as f:
        video_data = f.read()

    delay = BSKY_UPLOAD_BASE_DELAY

    for attempt in range(1, BSKY_UPLOAD_MAX_RETRIES + 1):
        try:
            blob = client.upload_blob(video_data)
            logging.info(f"✅ Blob uploaded successfully for {video_id}")
            return blob.blob

        except Exception as e:
            err_detail = f"{type(e).__name__}: {e}"
            if attempt >= BSKY_UPLOAD_MAX_RETRIES:
                logging.error(
                    f"❌ Blob upload failed after {BSKY_UPLOAD_MAX_RETRIES} attempts: "
                    f"{err_detail}"
                )
                return None
            logging.warning(
                f"⚠️ Blob upload attempt {attempt}/{BSKY_UPLOAD_MAX_RETRIES} "
                f"failed: {err_detail}. Retrying in {delay:.1f}s..."
            )
            time.sleep(delay + random.uniform(0, BSKY_UPLOAD_JITTER_MAX))
            delay = min(delay * 2, BSKY_UPLOAD_MAX_DELAY)

    return None


# ─────────────────────────────────────────────────────────────────────────────
#  Bluesky post
# ─────────────────────────────────────────────────────────────────────────────
def post_video_to_bluesky(
    client: Client,
    blob,
    caption: str,
    langs: list[str],
    video_id: str,
) -> bool:
    from atproto import models
    try:
        video_embed = models.AppBskyEmbedVideo.Main(video=blob)
        client.send_post(text=caption, embed=video_embed, langs=langs)
        logging.info(f"✅ Posted video {video_id} to Bluesky.")
        return True
    except Exception as e:
        logging.error(
            f"❌ Failed to post video {video_id} to Bluesky: "
            f"{type(e).__name__}: {e}"
        )
        return False


# ─────────────────────────────────────────────────────────────────────────────
#  Caption builder
# ─────────────────────────────────────────────────────────────────────────────
def build_caption(video_info: dict, tiktok_handle: str, max_len: int = 300) -> str:
    """
    Build a Bluesky post caption from video metadata.

    The TikTok URL is intentionally omitted — the video is already
    embedded in the post, so the URL is redundant.

    If the description exceeds 300 chars it is trimmed at the last
    whitespace boundary before the limit.
    """
    desc = (video_info.get("description") or "").strip()

    if not desc:
        return ""

    if len(desc) > max_len:
        trimmed = desc[:max_len - 1]
        cut     = trimmed.rfind(" ")
        # Only use word boundary if it doesn't cut off too much
        if cut > max_len // 2:
            trimmed = trimmed[:cut]
        desc = trimmed + "…"

    return desc
# ─────────────────────────────────────────────────────────────────────────────
#  TikTok scraping — Playwright
# ─────────────────────────────────────────────────────────────────────────────
def dismiss_overlays(page) -> None:
    all_sels = TIKTOK_COOKIE_MODAL_SELS + TIKTOK_BANNER_SELS
    for sel in all_sels:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=1500):
                el.click(timeout=1500)
                logging.info(f"🚫 Dismissed overlay: {sel}")
                time.sleep(0.5)
        except Exception:
            pass


def _run_playwright_scrape_loop(page, profile_url: str, limit: int) -> list[dict]:
    """Inner scraping loop — shared by stealth and no-stealth paths."""
    videos = []

    for attempt in range(1, PLAYWRIGHT_MAX_RELOADS + 1):
        try:
            logging.info(
                f"🌐 Loading profile (attempt {attempt}/{PLAYWRIGHT_MAX_RELOADS})..."
            )
            page.goto(
                profile_url,
                wait_until="domcontentloaded",
                timeout=PLAYWRIGHT_TIMEOUT_MS,
            )
            time.sleep(3)
            dismiss_overlays(page)

            try:
                page.wait_for_selector(
                    TIKTOK_VIDEO_GRID_SEL, timeout=PLAYWRIGHT_TIMEOUT_MS
                )
            except Exception:
                pass

            grid = page.locator(TIKTOK_VIDEO_GRID_SEL).first
            if not grid.is_visible(timeout=5000):
                logging.warning(f"⚠️ Video grid not found on attempt {attempt}.")
                ts = int(time.time())
                try:
                    page.screenshot(path=f"screenshot_no_grid_{attempt}_{ts}.png")
                    logging.info(
                        f"📸 Screenshot saved: screenshot_no_grid_{attempt}_{ts}.png"
                    )
                except Exception:
                    pass
                time.sleep(3)
                continue

            items = page.locator(TIKTOK_VIDEO_ITEM_SEL).all()
            for item in items[:limit]:
                try:
                    link = item.locator("a").first.get_attribute("href")
                    if link and "/video/" in link:
                        vid_match = re.search(r"/video/(\d+)", link)
                        if vid_match:
                            video_id = vid_match.group(1)
                            full_url = (
                                link if link.startswith("http")
                                else f"https://www.tiktok.com{link}"
                            )
                            videos.append({
                                "video_id":    video_id,
                                "url":         full_url,
                                "timestamp":   None,
                                "description": "",
                            })
                except Exception:
                    pass

            if videos:
                logging.info(f"✅ Playwright scraped {len(videos)} videos.")
                break

        except Exception as e:
            logging.warning(
                f"⚠️ Playwright attempt {attempt} error: {type(e).__name__}: {e}"
            )
            ts = int(time.time())
            try:
                page.screenshot(path=f"screenshot_error_{attempt}_{ts}.png")
            except Exception:
                pass
            time.sleep(3)

    return videos


def scrape_tiktok_profile_playwright(
    handle: str,
    cookies: list,
    limit: int = SCRAPE_VIDEO_LIMIT,
) -> list[dict]:
    """
    Scrape the most recent video URLs from a TikTok profile page using Playwright.

    Stealth strategy:
      v1.x → stealth_sync(page) after new_page() — works reliably
      v2.x → skipped entirely; v2.0.x API is unstable across patch versions.
              Browser launch args provide equivalent bot-detection evasion.
    """
    profile_url = f"https://www.tiktok.com/@{handle}"
    logging.info(f"🕷️ Scraping TikTok profile: {profile_url}")

    videos = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            slow_mo=PLAYWRIGHT_SLOW_MO,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-web-security",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="es-ES",
            extra_http_headers={
                "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
            },
        )

        inject_cookies_into_context(context, cookies)
        page = context.new_page()

        # Apply stealth v1.x if available; skip v2.x entirely (unstable API)
        if _STEALTH_SYNC is not None:
            try:
                _STEALTH_SYNC(page)
                logging.info("🥷 playwright-stealth v1.x applied.")
            except Exception as e:
                logging.warning(
                    f"⚠️ playwright-stealth v1.x failed: {type(e).__name__}: {e}. "
                    f"Continuing without stealth."
                )
        else:
            logging.info(
                "ℹ️ playwright-stealth v2.x detected — skipping (unstable API). "
                "Using browser launch args for bot-detection evasion."
            )

        videos = _run_playwright_scrape_loop(page, profile_url, limit)

        if not videos:
            logging.warning(
                f"⚠️ Video grid not found after {PLAYWRIGHT_MAX_RELOADS} attempts."
            )
            ts = int(time.time())
            try:
                page.screenshot(
                    path=f"screenshot_no_grid_{PLAYWRIGHT_MAX_RELOADS}_{ts}.png"
                )
                logging.info(
                    f"📸 Screenshot saved: "
                    f"screenshot_no_grid_{PLAYWRIGHT_MAX_RELOADS}_{ts}.png"
                )
            except Exception:
                pass

        for obj in (page, context, browser):
            try:
                if obj:
                    obj.close()
            except Exception:
                pass

    return videos


# ─────────────────────────────────────────────────────────────────────────────
#  TikTok scraping — yt-dlp fallback
# ─────────────────────────────────────────────────────────────────────────────
def scrape_tiktok_profile_ytdlp(
    handle: str,
    netscape_cookies_path: str = None,
    limit: int = SCRAPE_VIDEO_LIMIT,
) -> list[dict]:
    """
    Fallback: use yt-dlp to extract the video list from a TikTok profile.
    Accepts a Netscape-format cookie file path (not JSON).

    Note: flat playlist extraction gives us basic metadata (title, timestamp)
    but not the full description — that is fetched per-video in process_videos().
    """
    import yt_dlp

    profile_url = f"https://www.tiktok.com/@{handle}"
    logging.info(f"📦 yt-dlp profile scrape fallback for @{handle}...")

    impersonate = get_best_impersonation_target()

    ydl_opts = {
        "extract_flat": True,
        "quiet":        True,
        "no_warnings":  True,
        "playlistend":  limit,
    }
    if netscape_cookies_path and os.path.exists(netscape_cookies_path):
        ydl_opts["cookiefile"] = netscape_cookies_path
    if impersonate is not None:
        ydl_opts["impersonate"] = impersonate

    try:
        logging.info(f"🌐 yt-dlp extracting: {profile_url}")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(profile_url, download=False)

        entries = info.get("entries", []) if info else []
        logging.info(
            f"✅ yt-dlp returned {len(entries)} entries "
            f"(playlist: {info.get('title', '?') if info else '?'})"
        )

        videos = []
        for entry in entries:
            if not entry:
                continue
            url = entry.get("url") or entry.get("webpage_url") or ""
            vid_match = re.search(r"/video/(\d+)", url)
            if not vid_match:
                vid_id = entry.get("id", "")
                if vid_id:
                    url = f"https://www.tiktok.com/@{handle}/video/{vid_id}"
                    vid_match = re.search(r"/video/(\d+)", url)
            if vid_match:
                # description from flat extraction is usually just the title —
                # the full caption is fetched per-video in process_videos()
                videos.append({
                    "video_id":    vid_match.group(1),
                    "url":         url,
                    "timestamp":   entry.get("timestamp"),
                    "description": (entry.get("description") or "").strip(),
                })

        logging.info(f"✅ yt-dlp fallback produced {len(videos)} usable videos.")
        return videos[:limit]

    except Exception as e:
        logging.error(f"❌ yt-dlp profile scrape failed: {type(e).__name__}: {e}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
#  Main processing loop
# ─────────────────────────────────────────────────────────────────────────────
def process_videos(
    videos: list[dict],
    state: dict,
    client: Client,
    tiktok_handle: str,
    netscape_cookies_path: str,
    langs: list[str],
    max_age_days: int,
    video_max_size_bytes: int,
) -> int:
    """
    For each new video:
      0. Fetch full metadata (description/caption) via yt-dlp
      1. Download the video file
      2. Compress to fit within the PDS size limit
      3. Upload blob to Bluesky
      4. Create the post with caption + URL
    """
    posted_count = 0
    now = arrow.utcnow()

    for video in videos:
        video_id  = video["video_id"]
        video_url = video["url"]

        if is_already_posted(video_id, state):
            logging.info(f"⏭️  Already posted: {video_id}")
            continue

        # Age filter (only when timestamp is available)
        ts = video.get("timestamp")
        if ts:
            try:
                video_time = arrow.get(ts)
                age_days   = (now - video_time).days
                if age_days > max_age_days:
                    logging.info(
                        f"⏭️  Video {video_id} too old "
                        f"({age_days}d > {max_age_days}d). Skipping."
                    )
                    continue
            except Exception:
                pass

        logging.info(f"🎬 Processing video {video_id}: {video_url}")

        # ── 0. Fetch full metadata if description not already populated ───
        if not video.get("description"):
            logging.info(f"🔍 Fetching metadata for {video_id}...")
            meta = fetch_video_metadata_ytdlp(
                video_url,
                netscape_cookies_path=netscape_cookies_path,
            )
            if meta:
                video["description"] = meta.get("description", "")
                # Backfill timestamp if we didn't have one from scraping
                if not video.get("timestamp") and meta.get("timestamp"):
                    video["timestamp"] = meta["timestamp"]

        with tempfile.TemporaryDirectory() as tmpdir:
            raw_path  = os.path.join(tmpdir, f"{video_id}_raw.mp4")
            comp_path = os.path.join(tmpdir, f"{video_id}.mp4")

            # 1. Download
            ok = download_video(
                video_url, raw_path,
                netscape_cookies_path=netscape_cookies_path,
            )
            if not ok:
                logging.error(f"❌ Download failed for {video_id}. Skipping.")
                continue

            # 2. Compress
            ok = compress_video(
                raw_path, comp_path, max_size_bytes=video_max_size_bytes
            )
            if not ok:
                logging.error(f"❌ Compression failed for {video_id}. Skipping.")
                continue

            # 3. Upload blob
            blob = upload_video_to_bluesky(client, comp_path, video_id)
            if blob is None:
                logging.error(f"❌ Blob upload failed for {video_id}.")
                continue

            # 4. Post
            caption = build_caption(video, tiktok_handle)
            logging.info(f"📝 Caption preview: {caption[:120]!r}")
            ok = post_video_to_bluesky(client, blob, caption, langs, video_id)
            if ok:
                mark_as_posted(video_id, state, meta={"url": video_url})
                posted_count += 1
                time.sleep(random.uniform(2.0, 5.0))

    return posted_count


# ─────────────────────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────────────────────
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Cross-post TikTok videos to Bluesky."
    )
    parser.add_argument("--tiktok-handle",     required=True)
    parser.add_argument("--bsky-handle",       required=True)
    parser.add_argument("--bsky-app-password", required=True)
    parser.add_argument(
        "--bsky-base-url", default=DEFAULT_BSKY_BASE_URL,
        help=f"Bluesky PDS base URL (default: {DEFAULT_BSKY_BASE_URL})",
    )
    parser.add_argument(
        "--bsky-langs", nargs="+", default=DEFAULT_BSKY_LANGS,
        help="BCP-47 language tags for posts (default: es)",
    )
    parser.add_argument(
        "--cookies-path", default=TIKTOK_COOKIES_PATH,
        help=f"Path to TikTok cookies JSON (default: {TIKTOK_COOKIES_PATH})",
    )
    parser.add_argument(
        "--max-age-days", type=int, default=VIDEO_MAX_AGE_DAYS,
        help=f"Skip videos older than N days (default: {VIDEO_MAX_AGE_DAYS})",
    )
    return parser.parse_args()


def main():
    load_dotenv()
    args = parse_args()

    video_max_size_bytes = get_video_size_limit(args.bsky_base_url)

    logging.info("=" * 60)
    logging.info("🤖 TikTok→Bluesky bot started")
    logging.info(f"   TikTok handle : @{args.tiktok_handle}")
    logging.info(f"   Bluesky handle: {args.bsky_handle}")
    logging.info(f"   Bluesky PDS   : {args.bsky_base_url}")
    logging.info(f"   Languages     : {args.bsky_langs}")
    logging.info(f"   Video size cap: {video_max_size_bytes // 1024 // 1024} MB")
    cookie_status = "✅ found" if os.path.exists(args.cookies_path) else "❌ NOT FOUND"
    logging.info(f"   Cookie file   : {args.cookies_path} ({cookie_status})")
    logging.info("=" * 60)

    state  = load_state()
    client = connect_bluesky(
        args.bsky_handle, args.bsky_app_password, args.bsky_base_url
    )

    # Convert JSON cookies → Netscape format once for all yt-dlp calls
    netscape_cookies_path = convert_json_cookies_to_netscape(args.cookies_path)
    if netscape_cookies_path:
        logging.info(f"🍪 Netscape cookie file ready: {netscape_cookies_path}")
    else:
        logging.warning(
            "⚠️ Could not create Netscape cookie file. "
            "yt-dlp will run without cookies."
        )

    try:
        logging.info(f"🔄 Scraping @{args.tiktok_handle}...")
        cookies = load_cookies_from_file(args.cookies_path)

        videos = scrape_tiktok_profile_playwright(
            args.tiktok_handle, cookies, limit=SCRAPE_VIDEO_LIMIT,
        )

        if not videos:
            logging.warning(
                "⚠️ Playwright grid scraping failed. Trying yt-dlp fallback..."
            )
            ts = int(time.time())
            logging.info(f"📸 Screenshot saved: screenshot_playwright_failed_{ts}.png")

            videos = scrape_tiktok_profile_ytdlp(
                args.tiktok_handle,
                netscape_cookies_path=netscape_cookies_path,
                limit=SCRAPE_VIDEO_LIMIT,
            )

        if not videos:
            logging.error("❌ No videos found. Exiting.")
            sys.exit(0)

        logging.info(f"📋 Found {len(videos)} video(s). Processing new ones...")

        posted = process_videos(
            videos=videos,
            state=state,
            client=client,
            tiktok_handle=args.tiktok_handle,
            netscape_cookies_path=netscape_cookies_path,
            langs=args.bsky_langs,
            max_age_days=args.max_age_days,
            video_max_size_bytes=video_max_size_bytes,
        )

        logging.info("=" * 60)
        logging.info(f"✅ Sync complete. Posted {posted} new video(s).")
        logging.info("🤖 Bot finished.")
        logging.info("=" * 60)

    finally:
        # Always clean up the temporary Netscape cookie file
        if netscape_cookies_path and os.path.exists(netscape_cookies_path):
            try:
                os.remove(netscape_cookies_path)
                logging.info(
                    f"🧹 Removed temporary Netscape cookie file: {netscape_cookies_path}"
                )
            except Exception as e:
                logging.warning(
                    f"⚠️ Could not remove Netscape cookie file: {e}"
                )


if __name__ == "__main__":
    main()