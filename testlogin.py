import argparse
import logging
import os
import random
import sys
import time

import httpx
from atproto import Client

# --- Logging ---
LOG_PATH = "bsky_login_test.log"
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(),
    ],
    level=logging.INFO,
)

EXIT_OK = 0
EXIT_BAD_CREDS = 2
EXIT_RATE_LIMIT = 3
EXIT_NETWORK = 4
EXIT_OTHER = 5


def parse_wait_seconds_from_exception(exc, default_delay=15, max_delay=900):
    """
    Parse common rate-limit headers from atproto exceptions:
      - retry-after (seconds)
      - x-ratelimit-after (seconds)
      - ratelimit-reset (unix timestamp)
    """
    try:
        headers = getattr(exc, "headers", None) or {}

        retry_after = headers.get("retry-after") or headers.get("Retry-After")
        if retry_after:
            return min(max(int(retry_after), 1), max_delay)

        x_after = headers.get("x-ratelimit-after") or headers.get("X-RateLimit-After")
        if x_after:
            return min(max(int(x_after), 1), max_delay)

        reset = headers.get("ratelimit-reset") or headers.get("RateLimit-Reset")
        if reset:
            wait_s = max(int(reset) - int(time.time()) + 1, 1)
            return min(wait_s, max_delay)

    except Exception:
        pass

    return default_delay


def classify_error(exc):
    """
    Classify exception into:
      - rate_limit
      - bad_creds
      - network
      - other
    """
    text = repr(exc).lower()
    status_code = getattr(exc, "status_code", None)

    if status_code == 429 or "429" in text or "too many requests" in text or "ratelimit" in text:
        return "rate_limit"

    if status_code in (401, 403) or "invalid identifier or password" in text or "authentication" in text:
        return "bad_creds"

    transient_signals = [
        "timeout",
        "connecterror",
        "remoteprotocolerror",
        "readtimeout",
        "writetimeout",
        "503",
        "502",
        "504",
        "connection",
    ]
    if any(sig in text for sig in transient_signals):
        return "network"

    return "other"


def preflight_health(service_url, timeout=8):
    url = f"{service_url.rstrip('/')}/xrpc/_health"
    try:
        r = httpx.get(url, timeout=timeout)
        logging.info(f"🩺 Health check {url} -> HTTP {r.status_code}")
        return True
    except Exception as e:
        logging.warning(f"🩺 Health check failed: {e}")
        return False


def build_client(service_url):
    normalized = service_url.strip().rstrip("/")

    try:
        return Client(base_url=normalized)
    except TypeError:
        logging.warning("⚠️ Client(base_url=...) unsupported in this atproto version. Falling back.")
        c = Client()
        try:
            if hasattr(c, "base_url"):
                c.base_url = normalized
            elif hasattr(c, "_base_url"):
                c._base_url = normalized
        except Exception as e:
            logging.warning(f"⚠️ Could not apply custom base URL: {e}")
        return c


def main():
    parser = argparse.ArgumentParser(description="Bluesky login test only.")
    parser.add_argument("--bsky-handle", required=True, help="Bluesky handle (e.g. user.example.social)")
    parser.add_argument(
        "--bsky-app-password",
        default=None,
        help="Bluesky app password (prefer env BSKY_APP_PASSWORD)",
    )
    parser.add_argument(
        "--service",
        default="https://bsky.social",
        help="PDS base URL (default: https://bsky.social)",
    )
    parser.add_argument("--max-attempts", type=int, default=3, help="Retry attempts (default: 3)")
    parser.add_argument("--base-delay", type=int, default=10, help="Base retry delay in seconds (default: 10)")
    parser.add_argument("--jitter-max", type=float, default=2.0, help="Random jitter max seconds (default: 2.0)")
    args = parser.parse_args()

    handle = args.bsky_handle.strip()
    service_url = args.service.strip().rstrip("/")
    app_password = (args.bsky_app_password or os.getenv("BSKY_APP_PASSWORD", "")).strip()

    if not app_password:
        logging.error("❌ Missing app password. Use --bsky-app-password or env BSKY_APP_PASSWORD.")
        print("LOGIN_FAILED_BAD_CREDS")
        sys.exit(EXIT_BAD_CREDS)

    logging.info(f"🔐 Testing login against: {service_url}")
    logging.info(f"👤 Handle: {handle}")

    # Optional but useful diagnostics
    preflight_health(service_url)

    client = build_client(service_url)

    last_kind = "other"

    for attempt in range(1, args.max_attempts + 1):
        try:
            logging.info(f"➡️ Login attempt {attempt}/{args.max_attempts}")
            client.login(handle, app_password)
            logging.info("✅ Login successful.")
            print("LOGIN_OK")
            sys.exit(EXIT_OK)

        except Exception as e:
            last_kind = classify_error(e)
            logging.exception(f"❌ Login failed [{last_kind}]")

            if last_kind == "bad_creds":
                print("LOGIN_FAILED_BAD_CREDS")
                sys.exit(EXIT_BAD_CREDS)

            if attempt >= args.max_attempts:
                break

            if last_kind == "rate_limit":
                wait_s = parse_wait_seconds_from_exception(e, default_delay=args.base_delay)
            elif last_kind == "network":
                wait_s = min(args.base_delay * attempt, 60)
            else:
                wait_s = min(args.base_delay * attempt, 45)

            wait_s = wait_s + random.uniform(0, max(args.jitter_max, 0.0))
            logging.warning(f"⏳ Waiting {wait_s:.1f}s before retry...")
            time.sleep(wait_s)

    if last_kind == "rate_limit":
        print("LOGIN_FAILED_RATE_LIMIT")
        sys.exit(EXIT_RATE_LIMIT)
    if last_kind == "network":
        print("LOGIN_FAILED_NETWORK")
        sys.exit(EXIT_NETWORK)

    print("LOGIN_FAILED")
    sys.exit(EXIT_OTHER)


if __name__ == "__main__":
    main()