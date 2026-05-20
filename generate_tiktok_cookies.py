#!/usr/bin/env python3
"""
generate_tiktok_cookies.py
──────────────────────────
Opens a real (headed) Chromium browser, navigates to TikTok,
and waits for you to:
  1. Log in manually
  2. Solve any CAPTCHA
  3. Reach the TikTok home feed

Then it saves the session cookies to tiktok_cookies.json
so tiktok2bsky.py can reuse them without a browser.

Usage:
    python generate_tiktok_cookies.py
    python generate_tiktok_cookies.py --output my_cookies.json
    python generate_tiktok_cookies.py --handle jijantesfc
"""

import argparse
import json
import logging
import os
import sys
import time

# ─────────────────────────────────────────────
#  Config
# ─────────────────────────────────────────────
DEFAULT_OUTPUT_PATH  = "tiktok_cookies.json"
TIKTOK_LOGIN_URL     = "https://www.tiktok.com/login"
TIKTOK_HOME_URL      = "https://www.tiktok.com"
POLL_INTERVAL_S      = 2.0      # how often to check if login is complete
LOGIN_TIMEOUT_S      = 300      # max seconds to wait for manual login (5 min)

# Selectors that indicate a successful login
LOGGED_IN_SELECTORS = [
    '[data-e2e="profile-icon"]',
    '[data-e2e="nav-profile"]',
    'a[href*="/profile"]',
    '[class*="DivAvatarContainer"]',
    '[class*="avatar-wrapper"]',
    'button:has-text("Upload")',
    'button:has-text("Cargar")',
    '[data-e2e="upload-icon"]',
]

# ─────────────────────────────────────────────
#  Logging
# ─────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    level=logging.INFO,
)


# ─────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────
def is_logged_in(page) -> bool:
    """Check if any known post-login selector is visible."""
    for sel in LOGGED_IN_SELECTORS:
        try:
            if page.locator(sel).first.is_visible(timeout=1500):
                logging.info(f"✅ Login detected via: {sel}")
                return True
        except Exception:
            pass
    return False


def wait_for_login(page, timeout_s: int = LOGIN_TIMEOUT_S) -> bool:
    """
    Poll the page every POLL_INTERVAL_S seconds until a logged-in
    selector appears or the timeout is reached.
    """
    elapsed = 0
    logging.info(
        f"⏳ Waiting up to {timeout_s}s for you to log in "
        "and solve any CAPTCHA..."
    )
    while elapsed < timeout_s:
        if is_logged_in(page):
            return True
        time.sleep(POLL_INTERVAL_S)
        elapsed += POLL_INTERVAL_S
        remaining = timeout_s - elapsed
        if elapsed % 30 < POLL_INTERVAL_S:   # log reminder every ~30s
            logging.info(
                f"   Still waiting... {remaining:.0f}s remaining. "
                "Complete the login in the browser window."
            )
    return False


def normalise_cookies(raw_cookies: list) -> list:
    """
    Normalise Playwright cookies to a clean JSON format
    compatible with both tiktok2bsky.py and yt-dlp (Netscape-like).
    Removes internal Playwright fields that cause issues.
    """
    cleaned = []
    for c in raw_cookies:
        entry = {
            "name":     c.get("name", ""),
            "value":    c.get("value", ""),
            "domain":   c.get("domain", ".tiktok.com"),
            "path":     c.get("path", "/"),
            "sameSite": c.get("sameSite", "None"),
            "secure":   c.get("secure", False),
            "httpOnly": c.get("httpOnly", False),
        }
        if c.get("expires") and c["expires"] > 0:
            entry["expirationDate"] = int(c["expires"])
        cleaned.append(entry)
    return cleaned


def save_cookies(cookies: list, output_path: str):
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(cookies, f, indent=2, ensure_ascii=False)
    logging.info(f"💾 Saved {len(cookies)} cookies → {output_path}")


def navigate_to_profile(page, handle: str):
    """After login, optionally navigate to the target profile to warm up cookies."""
    profile_url = f"https://www.tiktok.com/@{handle.lstrip('@')}"
    logging.info(f"🌐 Navigating to target profile: {profile_url}")
    try:
        page.goto(profile_url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(3.0)
        logging.info("✅ Profile page loaded — cookies are now profile-warmed.")
    except Exception as e:
        logging.warning(f"⚠️ Could not navigate to profile: {e}")


# ─────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Generate tiktok_cookies.json by logging in manually."
    )
    parser.add_argument(
        "--output", "-o",
        default=DEFAULT_OUTPUT_PATH,
        help=f"Output path for cookies JSON (default: {DEFAULT_OUTPUT_PATH})",
    )
    parser.add_argument(
        "--handle",
        default=None,
        help=(
            "TikTok handle to visit after login (e.g. jijantesfc). "
            "Warms up the session cookies for that profile."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=LOGIN_TIMEOUT_S,
        help=f"Seconds to wait for manual login (default: {LOGIN_TIMEOUT_S})",
    )
    args = parser.parse_args()

    # ── Safety check: warn if output already exists ───────────────────
    if os.path.exists(args.output):
        logging.warning(
            f"⚠️ '{args.output}' already exists and will be overwritten."
        )

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logging.error(
            "❌ playwright is not installed. Run: pip install playwright"
        )
        sys.exit(1)

    logging.info("🚀 Launching headed Chromium browser...")
    logging.info("=" * 60)
    logging.info("  👉 A browser window will open.")
    logging.info("  👉 Log in to TikTok manually.")
    logging.info("  👉 Solve any CAPTCHA or verification that appears.")
    logging.info("  👉 Once you reach the home feed, this script")
    logging.info("     will detect it and save your cookies automatically.")
    logging.info("  👉 Do NOT close the browser window yourself.")
    logging.info("=" * 60)

    with sync_playwright() as p:
        # ── Launch HEADED browser (visible window) ─────────────────────
        # [[2]](#__2): Playwright headless=False for interactive login sessions
        browser = p.chromium.launch(
            headless=False,           # ← must be False so you can interact
            slow_mo=50,               # slight slowdown for stability
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--window-size=1280,900",
            ],
        )

        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="es-ES",
            timezone_id="Europe/Madrid",
        )

        page = context.new_page()

        # ── Navigate to TikTok login ───────────────────────────────────
        logging.info(f"🌐 Opening TikTok login page: {TIKTOK_LOGIN_URL}")
        try:
            page.goto(TIKTOK_LOGIN_URL, wait_until="domcontentloaded",
                      timeout=30000)
        except Exception as e:
            logging.error(f"❌ Failed to open TikTok login page: {e}")
            browser.close()
            sys.exit(1)

        # ── Wait for manual login ──────────────────────────────────────
        # [[3]](#__3): Playwright storage_state / context.cookies() for session persistence
        logged_in = wait_for_login(page, timeout_s=args.timeout)

        if not logged_in:
            logging.error(
                f"❌ Login not detected within {args.timeout}s. "
                "Cookies NOT saved. Please try again."
            )
            browser.close()
            sys.exit(1)

        logging.info("🎉 Login confirmed!")

        # ── Optional: warm up cookies on target profile ────────────────
        if args.handle:
            navigate_to_profile(page, args.handle)

        # ── Give TikTok a moment to set all session cookies ───────────
        logging.info("⏳ Waiting 3s for all session cookies to settle...")
        time.sleep(3.0)

        # ── Extract and save cookies ───────────────────────────────────
        raw_cookies = context.cookies()
        if not raw_cookies:
            logging.error("❌ No cookies found in context. Something went wrong.")
            browser.close()
            sys.exit(1)

        tiktok_cookies = [
            c for c in raw_cookies
            if "tiktok.com" in c.get("domain", "")
        ]

        logging.info(
            f"🍪 Extracted {len(tiktok_cookies)} TikTok cookies "
            f"(out of {len(raw_cookies)} total)."
        )

        normalised = normalise_cookies(tiktok_cookies)
        save_cookies(normalised, args.output)

        # ── Also save Playwright storage_state as backup ───────────────
        storage_path = args.output.replace(".json", "_storage_state.json")
        context.storage_state(path=storage_path)
        logging.info(f"💾 Full storage state (backup) → {storage_path}")

        browser.close()

    logging.info("")
    logging.info("=" * 60)
    logging.info(f"✅ Done! Cookies saved to: {args.output}")
    logging.info(f"   Storage state saved to:  {storage_path}")
    logging.info("")
    logging.info("Next steps:")
    logging.info(f"  1. Copy '{args.output}' to your Jenkins workspace")
    logging.info("     or add it as a Jenkins Secret File credential.")
    logging.info(
        "  2. Run tiktok2bsky.py — it will load cookies automatically."
    )
    logging.info(
        "  3. Cookies typically last 30–90 days. Re-run this script"
    )
    logging.info("     when the bot starts hitting CAPTCHAs again.")
    logging.info("=" * 60)


if __name__ == "__main__":
    main()