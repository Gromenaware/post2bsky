#!/usr/bin/env python3
import sys
import argparse
import io
import json
import logging
import os
import random
import re
import subprocess
import tempfile
import time
from dotenv import load_dotenv
from PIL import Image
from atproto import Client, client_utils, models

# --- Configuration ---
LOG_PATH = "bsky_single_post.log"
DEFAULT_BSKY_BASE_URL = "https://bsky.social"
DEFAULT_BSKY_LANGS = ["ca"]

BSKY_TEXT_MAX_LENGTH = 300

MAX_VIDEO_UPLOAD_SIZE_MB = 45
VIDEO_MAX_DURATION_SECONDS = 179
SUBPROCESS_TIMEOUT_SECONDS = 240
FFPROBE_TIMEOUT_SECONDS = 20

BSKY_IMAGE_MAX_BYTES = 950 * 1024
BSKY_IMAGE_MAX_DIMENSION = 2000
BSKY_IMAGE_MIN_JPEG_QUALITY = 45

BSKY_BLOB_UPLOAD_MAX_RETRIES = 5
BSKY_BLOB_UPLOAD_BASE_DELAY = 10
BSKY_BLOB_UPLOAD_MAX_DELAY = 300
BSKY_BLOB_TRANSIENT_ERROR_RETRIES = 3
BSKY_BLOB_TRANSIENT_ERROR_DELAY = 15

BSKY_SEND_POST_MAX_RETRIES = 3
BSKY_SEND_POST_BASE_DELAY = 5
BSKY_SEND_POST_MAX_DELAY = 60

BSKY_LOGIN_MAX_RETRIES = 5
BSKY_LOGIN_BASE_DELAY = 10
BSKY_LOGIN_MAX_DELAY = 600
BSKY_LOGIN_JITTER_MAX = 1.5

# --- Logging ---
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler()],
    level=logging.INFO,
)


# --- Text helpers ---
def clean_post_text(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def truncate_text_safely(text: str, max_length: int = BSKY_TEXT_MAX_LENGTH) -> str:
    if len(text) <= max_length:
        return text
    truncated = text[:max_length]
    last_space = truncated.rfind(" ")
    if last_space > 20:
        return truncated[:last_space]
    return truncated


def make_rich(content: str):
    text_builder = client_utils.TextBuilder()
    content = clean_post_text(content)
    lines = content.splitlines()

    for li, line in enumerate(lines):
        words = line.split(" ")
        for wi, word in enumerate(words):
            text_builder.text(word)
            if wi < len(words) - 1:
                text_builder.text(" ")
        if li < len(lines) - 1:
            text_builder.text("\n")

    return text_builder


# --- Error helpers ---
def is_rate_limited_error(error_obj):
    t = repr(error_obj).lower()
    return "429" in t or "ratelimitexceeded" in t or "too many requests" in t or "rate limit" in t


def is_auth_error(error_obj):
    t = repr(error_obj).lower()
    return "401" in t or "403" in t or "invalid identifier or password" in t


def is_network_error(error_obj):
    t = repr(error_obj)
    signals = ["ConnectError", "RemoteProtocolError", "ReadTimeout", "WriteTimeout", "TimeoutException", "503", "502", "504", "ConnectionResetError"]
    return any(sig in t for sig in signals)


def is_transient_error(error_obj):
    t = repr(error_obj)
    transient = ["InvokeTimeoutError", "ReadTimeout", "WriteTimeout", "TimeoutException", "RemoteProtocolError", "ConnectError", "503", "502", "504"]
    return any(s in t for s in transient)


def get_rate_limit_wait_seconds(error_obj, default_delay):
    try:
        now_ts = int(time.time())
        headers = getattr(error_obj, "headers", None) or {}
        retry_after = headers.get("retry-after") or headers.get("Retry-After")
        if retry_after:
            return min(max(int(retry_after), 1), BSKY_LOGIN_MAX_DELAY)
        reset_value = headers.get("ratelimit-reset") or headers.get("RateLimit-Reset")
        if reset_value:
            wait_seconds = max(int(reset_value) - now_ts + 1, default_delay)
            return min(wait_seconds, BSKY_LOGIN_MAX_DELAY)
    except Exception:
        pass

    text = repr(error_obj)
    m = re.search(r"'retry-after': '(\d+)'", text, re.IGNORECASE)
    if m:
        return min(max(int(m.group(1)), 1), BSKY_LOGIN_MAX_DELAY)

    return default_delay


# --- Bluesky client ---
def create_bsky_client(base_url, handle, password):
    base = (base_url or DEFAULT_BSKY_BASE_URL).strip().rstrip("/")
    logging.info(f"🔐 Connecting Bluesky client via base URL: {base}")
    client = Client(base_url=base)

    for attempt in range(1, BSKY_LOGIN_MAX_RETRIES + 1):
        try:
            logging.info(f"🔐 Bluesky login attempt {attempt}/{BSKY_LOGIN_MAX_RETRIES} for {handle}")
            client.login(handle, password)
            logging.info("✅ Bluesky login successful.")
            return client

        except Exception as e:
            logging.exception("❌ Bluesky login exception")

            if is_auth_error(e):
                logging.error("❌ Invalid Bluesky credentials.")
                raise

            if is_rate_limited_error(e):
                if attempt < BSKY_LOGIN_MAX_RETRIES:
                    wait = get_rate_limit_wait_seconds(e, BSKY_LOGIN_BASE_DELAY) + random.uniform(0, BSKY_LOGIN_JITTER_MAX)
                    logging.warning(f"⏳ Login rate-limited. Retrying in {wait:.1f}s...")
                    time.sleep(wait)
                    continue
                raise

            if is_network_error(e) or is_transient_error(e):
                if attempt < BSKY_LOGIN_MAX_RETRIES:
                    wait = min(BSKY_LOGIN_BASE_DELAY * attempt, BSKY_LOGIN_MAX_DELAY) + random.uniform(0, BSKY_LOGIN_JITTER_MAX)
                    logging.warning(f"⏳ Login transient failure. Retrying in {wait:.1f}s...")
                    time.sleep(wait)
                    continue
                raise

            raise

    raise RuntimeError("Bluesky login failed after retries.")


# --- Blob upload retries ---
def upload_blob_with_retry(client, binary_data, media_label="media"):
    last_exception = None
    transient_attempts = 0

    for attempt in range(1, BSKY_BLOB_UPLOAD_MAX_RETRIES + 1):
        try:
            result = client.upload_blob(binary_data)
            return result.blob
        except Exception as e:
            last_exception = e
            text = str(e)
            is_rate = "429" in text or "RateLimitExceeded" in text

            if is_rate:
                backoff = min(BSKY_BLOB_UPLOAD_BASE_DELAY * (2 ** (attempt - 1)), BSKY_BLOB_UPLOAD_MAX_DELAY)
                wait = get_rate_limit_wait_seconds(e, backoff)
                if attempt < BSKY_BLOB_UPLOAD_MAX_RETRIES:
                    logging.warning(f"⏳ Blob upload rate-limited for {media_label}. Retry in {wait}s.")
                    time.sleep(wait)
                    continue
                break

            if is_transient_error(e) and transient_attempts < BSKY_BLOB_TRANSIENT_ERROR_RETRIES:
                transient_attempts += 1
                wait = BSKY_BLOB_TRANSIENT_ERROR_DELAY * transient_attempts
                logging.warning(f"⏳ Blob transient error for {media_label}. Retry in {wait}s.")
                time.sleep(wait)
                continue

            logging.warning(f"⚠️ Blob upload failed for {media_label}: {repr(e)}")
            return None

    logging.warning(f"⚠️ Blob upload exhausted retries for {media_label}: {repr(last_exception)}")
    return None


def send_post_with_retry(client, **kwargs):
    last_exception = None

    for attempt in range(1, BSKY_SEND_POST_MAX_RETRIES + 1):
        try:
            return client.send_post(**kwargs)
        except Exception as e:
            last_exception = e
            text = str(e)
            is_rate = "429" in text or "RateLimitExceeded" in text

            if is_rate and attempt < BSKY_SEND_POST_MAX_RETRIES:
                backoff = min(BSKY_SEND_POST_BASE_DELAY * (2 ** (attempt - 1)), BSKY_SEND_POST_MAX_DELAY)
                wait = get_rate_limit_wait_seconds(e, backoff)
                logging.warning(f"⏳ send_post rate-limited. Retry in {wait}s.")
                time.sleep(wait)
                continue

            if is_transient_error(e) and attempt < BSKY_SEND_POST_MAX_RETRIES:
                wait = BSKY_SEND_POST_BASE_DELAY * attempt
                logging.warning(f"⏳ send_post transient error. Retry in {wait}s.")
                time.sleep(wait)
                continue

            raise

    raise last_exception


# --- Image helpers ---
def compress_post_image_to_limit(image_bytes, max_bytes=BSKY_IMAGE_MAX_BYTES):
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            img = img.convert("RGB")
            w, h = img.size
            max_dim = max(w, h)

            if max_dim > BSKY_IMAGE_MAX_DIMENSION:
                scale = BSKY_IMAGE_MAX_DIMENSION / max_dim
                img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)

            for q in [90, 82, 75, 68, 60, 52, BSKY_IMAGE_MIN_JPEG_QUALITY]:
                out = io.BytesIO()
                img.save(out, format="JPEG", quality=q, optimize=True, progressive=True)
                data = out.getvalue()
                if len(data) <= max_bytes:
                    return data
    except Exception as e:
        logging.warning(f"⚠️ Could not compress image: {repr(e)}")
    return None


def get_blob_from_file_image(client, path):
    if not os.path.exists(path):
        logging.error(f"❌ Image not found: {path}")
        return None

    with open(path, "rb") as f:
        content = f.read()

    upload_bytes = content
    if len(content) > BSKY_IMAGE_MAX_BYTES:
        logging.info("🖼️ Image exceeds safe limit, compressing...")
        compressed = compress_post_image_to_limit(content, BSKY_IMAGE_MAX_BYTES)
        if not compressed:
            logging.error("❌ Could not compress image enough.")
            return None
        upload_bytes = compressed

    return upload_blob_with_retry(client, upload_bytes, media_label=path)


# --- Video helpers ---
def remove_file_quietly(path):
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except Exception:
            pass


def probe_video_duration(file_path):
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        file_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=FFPROBE_TIMEOUT_SECONDS)
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError("ffprobe failed to read duration")
    return float(result.stdout.strip())


def transcode_video_for_bsky(input_path, output_path):
    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-vf", "scale='min(1280,iw)':-2",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "28",
        "-maxrate", "1800k",
        "-bufsize", "3600k",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        output_path,
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT_SECONDS)


def prepare_video_file(video_path):
    if not os.path.exists(video_path):
        logging.error(f"❌ Video not found: {video_path}")
        return None, None

    size_mb = os.path.getsize(video_path) / (1024 * 1024)
    logging.info(f"🎬 Source video size: {size_mb:.2f} MB")

    # Always transcode to maximize compatibility
    temp_out = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False).name
    res = transcode_video_for_bsky(video_path, temp_out)
    if res.returncode != 0:
        logging.error(f"❌ ffmpeg transcode failed:\n{res.stderr[-1200:]}")
        remove_file_quietly(temp_out)
        return None, None

    out_size_mb = os.path.getsize(temp_out) / (1024 * 1024)
    logging.info(f"✅ Prepared video size: {out_size_mb:.2f} MB")

    if out_size_mb > MAX_VIDEO_UPLOAD_SIZE_MB:
        logging.error(f"❌ Video still too large after transcode ({out_size_mb:.2f} MB > {MAX_VIDEO_UPLOAD_SIZE_MB} MB).")
        remove_file_quietly(temp_out)
        return None, None

    # Optional duration check
    try:
        duration = probe_video_duration(temp_out)
        if duration > VIDEO_MAX_DURATION_SECONDS:
            logging.warning(f"⚠️ Video duration {duration:.1f}s exceeds recommended {VIDEO_MAX_DURATION_SECONDS}s.")
    except Exception:
        pass

    return temp_out, True


def build_video_embed_raw(video_blob, alt_text):
    # RAW embed dict avoids SDK BlobRef issues
    embed = {
        "$type": "app.bsky.embed.video",
        "video": video_blob,
    }
    if alt_text:
        embed["alt"] = alt_text
    return embed


# --- Main post function ---
def post_single(client, text, langs, image_path=None, video_path=None, alt_text=""):
    clean = truncate_text_safely(clean_post_text(text), BSKY_TEXT_MAX_LENGTH)
    rich_text = make_rich(clean)

    if image_path and video_path:
        raise ValueError("Use either image or video, not both.")

    # Text + image
    if image_path:
        blob = get_blob_from_file_image(client, image_path)
        if not blob:
            logging.error("❌ Image upload failed.")
            return False

        embed = models.AppBskyEmbedImages.Main(
            images=[models.AppBskyEmbedImages.Image(alt=alt_text or "Image", image=blob)]
        )
        resp = send_post_with_retry(client, text=rich_text, embed=embed, langs=langs)
        logging.info(f"✅ Posted text+image: {getattr(resp, 'uri', None)}")
        return True

    # Text + video
    if video_path:
        prepared_path, is_temp = prepare_video_file(video_path)
        if not prepared_path:
            return False

        try:
            with open(prepared_path, "rb") as f:
                b = f.read()

            video_blob = upload_blob_with_retry(client, b, media_label=prepared_path)
            if not video_blob:
                logging.error("❌ Video blob upload failed.")
                return False

            raw_video_embed = build_video_embed_raw(video_blob, alt_text or "Video")
            resp = send_post_with_retry(client, text=rich_text, embed=raw_video_embed, langs=langs)
            logging.info(f"✅ Posted text+video: {getattr(resp, 'uri', None)}")
            return True
        finally:
            if is_temp:
                remove_file_quietly(prepared_path)

    # Text only fallback
    resp = send_post_with_retry(client, text=rich_text, langs=langs)
    logging.info(f"✅ Posted text only: {getattr(resp, 'uri', None)}")
    return True


def main():
    load_dotenv()

    parser = argparse.ArgumentParser(description="Single Bluesky post: text + image OR video")
    parser.add_argument("--text", required=True, help="Post text")
    parser.add_argument("--image", default=None, help="Local image path")
    parser.add_argument("--video", default=None, help="Local video path")
    parser.add_argument("--alt", default="", help="Alt text for media")
    parser.add_argument("--bsky-handle", default=None, help="Bluesky handle")
    parser.add_argument("--bsky-password", default=None, help="Bluesky app password")
    parser.add_argument("--bsky-base-url", default=None, help="PDS URL (e.g. https://eurosky.social)")
    parser.add_argument("--bsky-langs", default=None, help="Comma-separated langs, e.g. ca,es")
    args = parser.parse_args()

    handle = args.bsky_handle or os.getenv("BSKY_HANDLE")
    password = args.bsky_password or os.getenv("BSKY_APP_PASSWORD")
    base_url = (args.bsky_base_url or os.getenv("BSKY_BASE_URL") or DEFAULT_BSKY_BASE_URL).strip()

    raw_langs = args.bsky_langs or os.getenv("BSKY_LANGS")
    langs = [x.strip() for x in raw_langs.split(",") if x.strip()] if raw_langs else DEFAULT_BSKY_LANGS

    if not handle or not password:
        logging.error("❌ Missing credentials: --bsky-handle/BSKY_HANDLE and --bsky-password/BSKY_APP_PASSWORD are required.")
        sys.exit(1)

    if args.image and args.video:
        logging.error("❌ Use either --image or --video, not both.")
        sys.exit(1)

    client = create_bsky_client(base_url, handle, password)

    ok = post_single(
        client=client,
        text=args.text,
        langs=langs,
        image_path=args.image,
        video_path=args.video,
        alt_text=args.alt,
    )

    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
