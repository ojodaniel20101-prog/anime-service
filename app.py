#!/usr/bin/env python3
"""
AnimeHeaven API Server
======================
A Flask-based REST API for extracting video sources from animeheaven.me.
Designed for cloud deployment (Railway, Heroku, VPS, etc.)

Features:
  - Search anime by title
  - List all available episodes  
  - Extract video source URLs (stream + download)
  - Proxy support for cloud environments
  - Cloudflare bypass via cloudscraper
  - Health check endpoint
  - CORS enabled for web clients

Environment Variables:
  - PORT: Server port (default: 5000)
  - PROXY_URL: Optional proxy (e.g., http://user:pass@host:port)
  - SELENIUM_ENABLED: Set to "true" to enable Selenium fallback (default: false)
  - CHROME_BINARY: Path to Chrome/Chromium binary (optional)
  - RAILWAY_ENVIRONMENT: Auto-detected for Railway deployments

Endpoints:
  GET  /                  - API info + status
  GET  /health            - Health check
  GET  /search?q=<title>  - Search for anime
  GET  /episodes?id=<id>  - List episodes for an anime
  GET  /video?id=<anime_id>&ep=<number>&ep_id=<id>&mode=<stream|download>
                          - Extract video URL for an episode
"""

import os
import re
import sys
import json
import time
import logging
from pathlib import Path
from urllib.parse import urljoin, urlparse
from typing import List, Dict, Optional, Tuple

from flask import Flask, jsonify, request
from flask_cors import CORS

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("AnimeHeavenAPI")

# ---------------------------------------------------------------------------
# Try cloudscraper first (handles Cloudflare automatically), fall back to requests
try:
    import cloudscraper
    CLOUDSCRAPER_OK = True
    logger.info("cloudscraper loaded - Cloudflare bypass enabled")
except ImportError:
    CLOUDSCRAPER_OK = False
    logger.warning("cloudscraper not installed. Install with: pip install cloudscraper")

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Optional: Selenium for JavaScript-heavy pages
# ---------------------------------------------------------------------------
SELENIUM_ENABLED = os.environ.get("SELENIUM_ENABLED", "false").lower() == "true"
SELENIUM_OK = False
if SELENIUM_ENABLED:
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options as ChromeOptions
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.common.exceptions import TimeoutException, WebDriverException
        SELENIUM_OK = True
        logger.info("Selenium loaded - browser fallback available")
    except ImportError:
        logger.warning("Selenium not installed. Browser fallback unavailable.")

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
BASE_URL = "https://animeheaven.me"
SEARCH_URL = f"{BASE_URL}/search.php"
PROXY_URL = os.environ.get("PROXY_URL", "")
REQUEST_TIMEOUT = 30
RETRY_ATTEMPTS = 3
RETRY_DELAY = 3

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

VIDEO_SUBDOMAINS = ["rk", "fi", "cc", "la", "ny", "py", "ct", "ck", "cw", "ci"]

app = Flask(__name__)
CORS(app)  # Enable CORS for all domains

# =============================================================================
# SESSION MANAGER (cloudscraper + proxy support)
# =============================================================================

class SessionManager:
    """Manages HTTP requests with cloudscraper, retry logic, and optional proxy."""

    def __init__(self):
        self.proxies = None
        if PROXY_URL:
            self.proxies = {"http": PROXY_URL, "https": PROXY_URL}
            logger.info(f"Using proxy: {PROXY_URL.split('@')[-1]}")  # Log host only

        self.session = self._create_session()
        self.driver = None

    def _create_session(self):
        """Create a session - cloudscraper if available, else requests."""
        if CLOUDSCRAPER_OK:
            scraper = cloudscraper.create_scraper(
                browser={
                    'browser': 'chrome',
                    'platform': 'windows',
                    'mobile': False
                }
            )
            if self.proxies:
                scraper.proxies.update(self.proxies)
            return scraper
        else:
            sess = requests.Session()
            sess.headers.update(HEADERS)
            if self.proxies:
                sess.proxies.update(self.proxies)
            return sess

    def get(self, url: str, timeout: int = REQUEST_TIMEOUT, **kwargs) -> Optional[requests.Response]:
        """GET with retry logic."""
        last_error = None
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                resp = self.session.get(url, timeout=timeout, **kwargs)
                # Detect Cloudflare challenge
                if resp.status_code in (403, 503) or (
                    resp.status_code == 200
                    and ("Just a moment" in resp.text
                         or "cf-browser-verification" in resp.text)
                ):
                    logger.warning(f"Attempt {attempt}: Cloudflare challenge detected")
                    if attempt < RETRY_ATTEMPTS:
                        time.sleep(RETRY_DELAY)
                        continue
                    return None
                if "You have triggered abuse protection" in resp.text:
                    logger.warning(f"Attempt {attempt}: Abuse protection triggered")
                    if attempt < RETRY_ATTEMPTS:
                        time.sleep(RETRY_DELAY * 2)
                        continue
                    return None
                return resp
            except requests.RequestException as e:
                last_error = e
                logger.warning(f"Attempt {attempt}/{RETRY_ATTEMPTS}: {e}")
                if attempt < RETRY_ATTEMPTS:
                    time.sleep(RETRY_DELAY)

        logger.error(f"Failed to fetch {url}: {last_error}")
        return None

    def head(self, url: str, **kwargs) -> requests.Response:
        """HEAD request wrapper."""
        return self.session.head(url, timeout=REQUEST_TIMEOUT, **kwargs)

    def init_selenium(self) -> bool:
        """Initialize headless Chrome."""
        if not SELENIUM_OK:
            return False
        if self.driver is not None:
            return True

        options = ChromeOptions()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
        options.add_argument(f"--user-agent={HEADERS['User-Agent']}")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)

        # Use custom Chrome binary if specified
        chrome_binary = os.environ.get("CHROME_BINARY", "")
        if chrome_binary:
            options.binary_location = chrome_binary

        try:
            self.driver = webdriver.Chrome(options=options)
            self.driver.execute_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            logger.info("Selenium: Chrome headless initialized")
            return True
        except Exception as e:
            logger.error(f"Selenium: Failed to start Chrome: {e}")
            return False

    def close(self):
        if self.driver:
            self.driver.quit()
            self.driver = None


# =============================================================================
# CORE FUNCTIONS
# =============================================================================

def search_anime(session: SessionManager, query: str) -> List[Dict[str, str]]:
    """Search for anime. Returns list of {title, url, id}."""
    logger.info(f"Searching for: '{query}'")

    resp = session.get(SEARCH_URL, params={"s": query})
    if resp is None:
        raise Exception("Search failed. Site may be blocking requests.")

    soup = BeautifulSoup(resp.content, "lxml")
    results = []

    for link in soup.find_all("a", href=re.compile(r"anime\.php\?")):
        title = link.get_text(strip=True)
        href = link.get("href", "")
        if not title or not href:
            continue
        full_url = urljoin(BASE_URL, href)
        anime_id = href.split("?")[-1] if "?" in href else ""
        results.append({"title": title, "url": full_url, "id": anime_id})

    # Deduplicate by ID
    seen = set()
    unique = []
    for r in results:
        if r["id"] and r["id"] not in seen:
            seen.add(r["id"])
            unique.append(r)

    if not unique:
        raise Exception(f"No results found for '{query}'")

    return unique


def get_episode_list(session: SessionManager, anime_id: str) -> List[Dict]:
    """Extract episode list. Returns list of {number, title, ep_id, watch_url}."""
    logger.info(f"Fetching episodes for anime ID: {anime_id}")

    resp = session.get(f"{BASE_URL}/anime.php?{anime_id}")
    if resp is None:
        raise Exception("Failed to load anime page.")

    html = resp.text
    soup = BeautifulSoup(resp.content, "lxml")
    episodes = []

    # Method 1: Extract from <a> tags with gate.php and id attributes
    for link in soup.find_all("a", href="gate.php"):
        ep_id = link.get("id", "")
        if not ep_id:
            continue

        divs = link.find_all("div")
        for div in divs:
            text = div.get_text(strip=True)
            if re.match(r"^[0-9]+(?:\.[0-9]+)?$", text):
                ep_num = text
                episodes.append({
                    "number": ep_num,
                    "title": f"Episode {ep_num}",
                    "ep_id": ep_id,
                    "watch_url": f"{BASE_URL}/watch.php?{anime_id}&e={ep_num}"
                })
                break

    if not episodes:
        # Method 2: Use maxep variable
        maxep_match = re.search(r"var\s+maxep\s*=\s*([0-9]+)", html)
        if maxep_match:
            max_ep = int(maxep_match.group(1))
            logger.info(f"Found maxep={max_ep}, constructing range")
            for i in range(1, max_ep + 1):
                ep_num = str(i)
                episodes.append({
                    "number": ep_num,
                    "title": f"Episode {ep_num}",
                    "ep_id": "",
                    "watch_url": f"{BASE_URL}/watch.php?{anime_id}&e={ep_num}"
                })

    # Deduplicate and sort
    seen = set()
    unique_eps = []
    for ep in episodes:
        if ep["number"] not in seen:
            seen.add(ep["number"])
            unique_eps.append(ep)

    unique_eps.sort(key=lambda e: float(e["number"]) if e["number"].replace(".", "").isdigit() else 0)
    return unique_eps


def extract_urls_from_gate_html(html: str) -> Tuple[Optional[str], Optional[str]]:
    """Parse gate.php HTML to extract stream and download URLs."""
    stream_url = None
    download_url = None

    # Stream URL: first <source> tag with type='video/mp4' and onerror="xhr()"
    source_patterns = [
        r"<source\s+src='(https?://[^'\"]+\.animeheaven\.me/video\.mp4\?[^'\"&]+&[^'\"]+)'\s+type='video/mp4'\s+onerror=\"xhr\(\)\">",
        r"<source\s+src='(https?://[^'\"]+\.animeheaven\.me/video\.mp4\?[^'\"]+)'\s+type='video/mp4'",
    ]
    for pat in source_patterns:
        m = re.search(pat, html)
        if m:
            stream_url = m.group(1)
            break

    # Download URL: link with &d suffix
    download_patterns = [
        r"<a\s+href='(https?://[^'\"]+\.animeheaven\.me/video\.mp4\?[^'\"]+&d)'",
        r'href=["\'](https?://[^"\']+\.animeheaven\.me/video\.mp4\?[^"\']+&d)["\']',
    ]
    for pat in download_patterns:
        m = re.search(pat, html)
        if m:
            download_url = m.group(1)
            break

    # Fallback: construct download from stream
    if stream_url and not download_url:
        base = stream_url.split("&")[0]
        download_url = base + "&d"

    return stream_url, download_url


def extract_video_source(
    session: SessionManager,
    anime_id: str,
    ep_number: str,
    ep_id: str = "",
    mode: str = "stream"
) -> Dict:
    """
    Extract video source for an episode.

    Returns dict with:
      - video_url: The direct video URL
      - video_type: "stream" or "download"
      - headers_needed: dict of headers required to access the URL
      - supports_range: bool (HTTP Range seek support)
      - size_bytes: int (content length if available)
    """
    logger.info(f"Extracting video for Episode {ep_number} (mode: {mode})")

    stream_url = None
    download_url = None
    html_for_debug = ""

    # Method 1: gate.php with cookie (most reliable)
    if ep_id:
        logger.info("Trying gate.php with cookie...")
        gate_session = session.session
        try:
            # Create a fresh request with the cookie
            cookies = {"key": ep_id}
            resp = gate_session.get(
                f"{BASE_URL}/gate.php",
                cookies=cookies,
                timeout=20,
                allow_redirects=True
            )
            if resp.status_code == 200:
                stream_url, download_url = extract_urls_from_gate_html(resp.text)
                html_for_debug = resp.text[:2000]
                if stream_url:
                    logger.info(f"Stream URL extracted from gate.php")
                if download_url:
                    logger.info(f"Download URL extracted from gate.php")
        except Exception as e:
            logger.warning(f"gate.php error: {e}")

    # Method 2: Selenium fallback
    if not stream_url and SELENIUM_OK:
        logger.info("Trying Selenium extraction...")
        try:
            result = _extract_via_selenium(session, anime_id, ep_number)
            if result:
                stream_url, download_url = result
        except Exception as e:
            logger.warning(f"Selenium extraction failed: {e}")

    # Method 3: Direct construction fallback
    if not download_url and ep_id:
        logger.info("Trying direct URL construction...")
        direct_url = _try_direct_urls(session, ep_id)
        if direct_url:
            download_url = direct_url
            if not stream_url:
                stream_url = direct_url.replace("&d", "")
            logger.info("Direct construction succeeded")

    # Validate URLs
    selected_url = None
    video_type = mode

    if mode == "stream":
        selected_url = stream_url or download_url
    else:
        selected_url = download_url or stream_url
        video_type = "download" if download_url else "stream"

    if not selected_url:
        raise Exception(
            "Could not extract video source. "
            "The site may have changed or the episode may not be available."
        )

    # Verify URL is accessible
    headers_needed = {"Referer": f"{BASE_URL}/", "User-Agent": HEADERS["User-Agent"]}
    supports_range = False
    size_bytes = None

    try:
        check_resp = session.session.head(
            selected_url, timeout=15, allow_redirects=True,
            headers=headers_needed
        )
        if check_resp.status_code in (200, 206):
            supports_range = check_resp.headers.get("Accept-Ranges") == "bytes"
            size_bytes = int(check_resp.headers.get("Content-Length", 0)) or None
            logger.info(f"URL verified - Range: {supports_range}, Size: {size_bytes}")
        else:
            logger.warning(f"URL returned status {check_resp.status_code}")
    except Exception as e:
        logger.warning(f"Could not verify URL: {e}")

    return {
        "video_url": selected_url,
        "video_type": video_type,
        "headers_needed": headers_needed,
        "supports_range": supports_range,
        "size_bytes": size_bytes,
        "episode": ep_number,
        "anime_id": anime_id,
    }


def _try_direct_urls(session: SessionManager, ep_id: str) -> Optional[str]:
    """Try to construct and verify direct video URL from episode ID."""
    test_headers = {
        "User-Agent": HEADERS["User-Agent"],
        "Referer": f"{BASE_URL}/",
    }

    for sub in VIDEO_SUBDOMAINS:
        url = f"https://{sub}.animeheaven.me/video.mp4?{ep_id}&d"
        try:
            resp = session.session.head(url, timeout=10, allow_redirects=True, headers=test_headers)
            if resp.status_code == 200:
                ct = resp.headers.get("Content-Type", "")
                cl = resp.headers.get("Content-Length", "0")
                if "video" in ct or (cl and int(cl) > 100000):
                    logger.info(f"Direct URL verified on CDN: {sub}")
                    return url
        except Exception:
            continue

        # Also try without &d
        url2 = f"https://{sub}.animeheaven.me/video.mp4?{ep_id}"
        try:
            resp = session.session.head(url2, timeout=10, allow_redirects=True, headers=test_headers)
            if resp.status_code == 200 and "video" in resp.headers.get("Content-Type", ""):
                return url2
        except Exception:
            continue

    return None


def _extract_via_selenium(session: SessionManager, anime_id: str, ep_number: str) -> Optional[Tuple[str, str]]:
    """Extract video URLs using Selenium browser."""
    if not session.init_selenium():
        return None

    anime_url = f"{BASE_URL}/anime.php?{anime_id}"
    session.driver.get(anime_url)
    time.sleep(5)

    # Find and click episode
    ep_xpath = f"//a[.//div[contains(text(), 'Episode {ep_number}')]]"
    try:
        ep_link = WebDriverWait(session.driver, 15).until(
            EC.presence_of_element_located((By.XPATH, ep_xpath))
        )
        session.driver.execute_script("arguments[0].scrollIntoView();", ep_link)
        time.sleep(1)
        ep_link.click()
        time.sleep(6)
    except TimeoutException:
        logger.warning(f"Could not find Episode {ep_number}")
        return None

    html = session.driver.page_source
    return extract_urls_from_gate_html(html)


# =============================================================================
# FLASK ROUTES
# =============================================================================

@app.route("/")
def index():
    """API info and status."""
    return jsonify({
        "name": "AnimeHeaven API",
        "version": "1.0.0",
        "description": "Extract video sources from animeheaven.me",
        "cloudscraper": CLOUDSCRAPER_OK,
        "selenium": SELENIUM_OK,
        "proxy": bool(PROXY_URL),
        "endpoints": {
            "GET /health": "Health check",
            "GET /search?q=<title>": "Search for anime",
            "GET /episodes?id=<anime_id>": "List episodes",
            "GET /video?id=<anime_id>&ep=<ep_number>&ep_id=<ep_id>&mode=<stream|download>": "Extract video URL",
        }
    })


@app.route("/health")
def health():
    """Health check - also verifies AnimeHeaven is reachable."""
    try:
        session = SessionManager()
        resp = session.get(BASE_URL, timeout=15)
        site_ok = resp is not None and resp.status_code == 200
        session.close()

        return jsonify({
            "status": "healthy" if site_ok else "degraded",
            "animeheaven_reachable": site_ok,
            "cloudscraper": CLOUDSCRAPER_OK,
            "selenium": SELENIUM_OK,
            "proxy_configured": bool(PROXY_URL),
        })
    except Exception as e:
        return jsonify({
            "status": "unhealthy",
            "error": str(e),
            "cloudscraper": CLOUDSCRAPER_OK,
            "selenium": SELENIUM_OK,
        }), 503


@app.route("/search")
def search():
    """Search for anime by title. Query param: q"""
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "Missing 'q' query parameter"}), 400

    session = SessionManager()
    try:
        results = search_anime(session, query)
        return jsonify({
            "query": query,
            "count": len(results),
            "results": results
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        session.close()


@app.route("/episodes")
def episodes():
    """List episodes for an anime. Query param: id (anime ID)"""
    anime_id = request.args.get("id", "").strip()
    if not anime_id:
        return jsonify({"error": "Missing 'id' query parameter (anime ID)"}), 400

    session = SessionManager()
    try:
        eps = get_episode_list(session, anime_id)
        return jsonify({
            "anime_id": anime_id,
            "episode_count": len(eps),
            "episodes": eps
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        session.close()


@app.route("/video")
def video():
    """
    Extract video URL for an episode.
    Query params:
      - id: Anime ID (required)
      - ep: Episode number (required)
      - ep_id: Episode hash ID (optional but recommended)
      - mode: 'stream' or 'download' (default: stream)
    """
    anime_id = request.args.get("id", "").strip()
    ep_number = request.args.get("ep", "").strip()
    ep_id = request.args.get("ep_id", "").strip()
    mode = request.args.get("mode", "stream").strip().lower()

    if not anime_id:
        return jsonify({"error": "Missing 'id' parameter (anime ID)"}), 400
    if not ep_number:
        return jsonify({"error": "Missing 'ep' parameter (episode number)"}), 400
    if mode not in ("stream", "download"):
        return jsonify({"error": "Mode must be 'stream' or 'download'"}), 400

    session = SessionManager()
    try:
        result = extract_video_source(session, anime_id, ep_number, ep_id, mode)
        return jsonify({
            "success": True,
            **result
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e),
            "note": (
                "If this fails repeatedly, the site may be blocking cloud IPs. "
                "Try using a proxy by setting the PROXY_URL environment variable."
            )
        }), 500
    finally:
        session.close()


# =============================================================================
# MAIN
# =============================================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"

    logger.info(f"Starting AnimeHeaven API on port {port}")
    logger.info(f"Cloudscraper: {'enabled' if CLOUDSCRAPER_OK else 'DISABLED'}")
    logger.info(f"Selenium: {'enabled' if SELENIUM_OK else 'disabled'}")
    logger.info(f"Proxy: {'configured' if PROXY_URL else 'none'}")

    app.run(host="0.0.0.0", port=port, debug=debug)
