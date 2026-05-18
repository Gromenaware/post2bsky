import argparse
import json
import os
import shutil
import time
from playwright.sync_api import sync_playwright

SESSION_FILE_PERMISSIONS = 0o600


def normalize_cookie_for_playwright(cookie: dict) -> dict:
    """
    Ensure cookie fields are compatible with Playwright's storage_state format.
    Playwright requires 'domain' to start with a dot for cross-subdomain cookies,
    and 'sameSite' must be one of 'Strict', 'Lax', or 'None'.
    """
    c = dict(cookie)

    # Normalize domain: x.com → .x.com
    domain = c.get("domain", "")
    if domain and not domain.startswith("."):
        c["domain"] = f".{domain}"

    # Normalize sameSite to valid Playwright values
    same_site = c.get("sameSite", "")
    valid_same_site = {"Strict", "Lax", "None"}
    if same_site not in valid_same_site:
        c["sameSite"] = "None"

    # Ensure required fields have defaults
    c.setdefault("path", "/")
    c.setdefault("httpOnly", False)
    c.setdefault("secure", True)
    c.setdefault("expires", -1)

    return c


def get_twitter_cookies(username: str, password: str) -> dict:
    """
    Automates Twitter login via Playwright and returns a Playwright-compatible
    storage_state dict (cookies + origins/localStorage).
    """
    with sync_playwright() as p:
        print("🚀 Launching headless browser...")
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/145.0.7632.6 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
        )
        page = context.new_page()

        try:
            print("🌐 Navigating to X login...")
            page.goto("https://x.com/i/flow/login", wait_until="domcontentloaded")

            # --- Username ---
            print(f"👤 Entering username: {username[:10]}...")
            page.wait_for_selector('input[autocomplete="username"]', timeout=25000)
            page.click('input[autocomplete="username"]')
            page.fill('input[autocomplete="username"]', username)

            print("➡️ Pressing Next...")
            page.locator('button:has-text("Next")').first.click()

            # --- Security challenge (email/phone) ---
            page.wait_for_selector(
                'input[name="password"], '
                'input[data-testid="ocfEnterTextTextInput"], '
                'input[name="text"]',
                timeout=15000,
            )
            time.sleep(1)

            if (
                page.locator('input[data-testid="ocfEnterTextTextInput"]').is_visible()
                or page.locator('input[name="text"]').is_visible()
            ):
                print("🛡️ Security challenge detected — this script needs --email arg.")
                raise RuntimeError(
                    "Security challenge appeared but no email/phone was provided. "
                    "Re-run with --email your_email_or_phone"
                )

            # --- Password ---
            print("🔑 Entering password...")
            page.wait_for_selector('input[name="password"]', timeout=15000)
            page.fill('input[name="password"]', password)

            print("🖱️ Clicking 'Log in'...")
            page.locator('span:has-text("Log in")').first.click()

            # --- Poll for auth_token + ct0 ---
            print("🍪 Waiting for auth_token + ct0 cookies...")
            auth_token = None
            ct0 = None
            for _ in range(40):
                cookies_list = context.cookies()
                auth_token = next((c["value"] for c in cookies_list if c["name"] == "auth_token"), None)
                ct0 = next((c["value"] for c in cookies_list if c["name"] == "ct0"), None)
                if auth_token and ct0:
                    print("✅ Both cookies found!")
                    break
                page.wait_for_timeout(1000)
            else:
                raise TimeoutError("auth_token/ct0 cookies never appeared after 40 seconds.")

            # --- Wait for page to fully settle before evaluate() ---
            print("⏳ Waiting for page to stabilize...")
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            try:
                page.wait_for_selector('[data-testid="primaryColumn"]', timeout=20000)
            except Exception:
                print("⚠️ primaryColumn not found — page may still be usable.")

            # --- Grab localStorage (non-critical) ---
            try:
                local_storage = page.evaluate(
                    "() => Object.entries(localStorage).map(([name, value]) => ({ name, value }))"
                )
            except Exception as ls_err:
                print(f"⚠️ Could not extract localStorage (non-critical): {ls_err}")
                local_storage = []

            # --- Re-fetch final cookie list and normalize for Playwright ---
            raw_cookies = context.cookies()
            normalized_cookies = [normalize_cookie_for_playwright(c) for c in raw_cookies]

            session_data = {
                "cookies": normalized_cookies,
                "origins": [
                    {
                        "origin": "https://x.com",
                        "localStorage": local_storage,
                    }
                ],
            }

            print(f"✅ auth_token: {auth_token[:10]}...")
            print(f"✅ ct0:        {ct0[:10]}...")

            browser.close()
            return session_data

        except Exception as e:
            print(f"❌ Error encountered. Taking a screenshot...")
            try:
                page.screenshot(path="error_screenshot.png")
                print("📸 Saved: error_screenshot.png")
            except Exception:
                pass
            browser.close()
            raise e


def save_session(session_data: dict, path: str):
    """Save Playwright storage_state JSON to disk with restricted permissions."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(session_data, f, indent=2)
    os.replace(temp_path, path)
    try:
        os.chmod(path, SESSION_FILE_PERMISSIONS)
    except Exception as e:
        print(f"⚠️ Could not set file permissions on {path}: {e}")
    print(f"💾 Session saved to: {path}")


def clear_session(path: str):
    """Delete a stale session file."""
    if os.path.exists(path):
        os.remove(path)
        print(f"🧹 Removed stale session file: {path}")
    else:
        print(f"ℹ️ No session file found at {path} — nothing to clear.")


def main():
    parser = argparse.ArgumentParser(
        description="Automate Twitter login and save Playwright session state."
    )
    parser.add_argument("username", help="Twitter username or handle")
    parser.add_argument("password", help="Twitter password")
    parser.add_argument(
        "--email",
        default="",
        help="Twitter email or phone (required if X shows a security challenge)",
    )
    parser.add_argument(
        "--output",
        default="twitter_browser_state.json",  # ← matches twitter2bsky.py exactly
        help="Output path for the Playwright storage state JSON",
    )
    parser.add_argument(
        "--clear-session",
        action="store_true",
        help="Delete any existing session file before starting",
    )

    args = parser.parse_args()

    if args.clear_session:
        clear_session(args.output)

    session_data = get_twitter_cookies(args.username, args.password)
    save_session(session_data, args.output)

    print(f"\n✅ Done! Session saved to '{args.output}'")
    print(f"   twitter2bsky.py will automatically pick it up on next run.")


if __name__ == "__main__":
    main()