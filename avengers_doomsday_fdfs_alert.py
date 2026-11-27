#!/usr/bin/env python3
"""
Avengers: Doomsday — Chennai First Day First Show (FDFS) Ticket Alert Monitor
=============================================================================
Monitors BookMyShow, District by Zomato, and PVR Cinemas for newly opened
December 17, 2026 (India opening day) early-morning / FDFS shows across any
theatre in Chennai (English, Tamil, IMAX, 3D, 4DX, 2D) and sends instant
push notifications to your phone via ntfy.sh (and optionally Telegram).

Quick Start
-----------
1. Install the free "ntfy" app on Android (Google Play / F-Droid) or iOS (App Store).
2. Open the app, tap "+", and subscribe to a unique private topic name, e.g.:
       chennai-doomsday-fdfs-9482
3. Install Python dependencies (Playwright is recommended because BookMyShow,
   District, and PVR render showtimes dynamically with JavaScript):
       pip install requests beautifulsoup4 playwright
       playwright install chromium
4. Set your ntfy topic (either edit NTFY_TOPIC below or set an env var):
       export NTFY_TOPIC="chennai-doomsday-fdfs-9482"
5. Test your phone notification:
       python3 avengers_doomsday_fdfs_alert.py --test-alert
6. Start continuous monitoring (checks every 90 seconds by default):
       python3 avengers_doomsday_fdfs_alert.py

Optional flags:
    --interval 60        Seconds between checks (default: 90; avoid <30s to prevent blocks)
    --cutoff 10:30       Latest start time (24h HH:MM) considered FDFS (default: 10:30 AM)
    --date 2026-12-17    Target opening date YYYY-MM-DD (default: 2026-12-17)
    --no-playwright      Use lightweight HTTP requests instead of headless Chromium
    --once               Run a single check and exit (useful for cron / GitHub Actions)
    --test-alert         Send a test push notification to your phone and exit
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin

try:
    import requests
except ImportError:
    requests = None  # type: ignore

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None  # type: ignore

# ---------------------------------------------------------------------------
# Configuration (edit here or override via environment variables / CLI flags)
# ---------------------------------------------------------------------------

NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "Avengers-doomsday-ticket")

# Optional Telegram backup (leave blank to use ntfy only)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

TARGET_DATE = "2026-12-17"  # India opening day for Avengers: Doomsday
FDFS_CUTOFF_24H = "10:30"   # Any show from 00:00 (midnight) through 10:30 AM
DEFAULT_INTERVAL_SEC = 90
STATE_FILE = Path(__file__).with_name("seen_fdfs_shows.json")

# Verified Chennai URLs for Avengers: Doomsday (2026)
BOOKMYSHOW_MOVIE_URL = "https://in.bookmyshow.com/movies/chennai/avengers-doomsday/ET00439706"
BOOKMYSHOW_EVENT_CODES = [
    "ET00439706",  # Main Chennai listing
    "ET00526287",  # Format variant (2D / MS Infinity Vision)
    "ET00526312",  # Format variant
    "ET00526306",  # Format variant (3D)
]

PVR_SESSIONS_URL = "https://www.pvrcinemas.com/moviesessions/Chennai/AVENGERS-DOOMSDAY/35312"

DISTRICT_CHENNAI_URL = "https://www.district.in/movies/chennai-movie-tickets"
# If you open Avengers: Doomsday on District in your browser, you can also paste
# its exact movie URL below (e.g. "https://www.district.in/movies/avengers-doomsday-...")
DISTRICT_DIRECT_URLS: list[str] = []

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

FORMAT_KEYWORDS = ("IMAX", "4DX", "3D", "2D", "DOLBY", "ATMOS", "LASER", "ICE", "SCREENX", "P[XL]")
LANGUAGE_KEYWORDS = ("TAMIL", "ENGLISH", "HINDI", "TELUGU")

TIME_REGEX = re.compile(r"\b(1[0-2]|0?[1-9]):([0-5][0-9])\s*(AM|PM)\b", re.IGNORECASE)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("doomsday-fdfs")


# ---------------------------------------------------------------------------
# Data Model & Parsing Helpers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ShowOpening:
    platform: str
    theatre: str
    showtime: str      # Normalized, e.g. "06:30 AM"
    date: str          # YYYY-MM-DD
    formats: str       # e.g. "IMAX 3D" or "Standard"
    language: str      # e.g. "Tamil / English" or "Unknown"
    booking_url: str

    def dedup_key(self) -> str:
        return f"{self.platform}|{self.date}|{self.theatre.lower()}|{self.showtime}"


def parse_12h_to_minutes(time_str: str) -> int | None:
    """Convert '06:30 AM' or '12:05 AM' into minutes since midnight (0..1439)."""
    m = TIME_REGEX.search(time_str)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2))
    ampm = m.group(3).upper()
    if ampm == "AM":
        hour = 0 if hour == 12 else hour
    else:
        hour = 12 if hour == 12 else hour + 12
    return hour * 60 + minute


def parse_24h_to_minutes(hhmm: str) -> int:
    parts = hhmm.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid 24h time '{hhmm}'. Expected HH:MM (e.g. 10:30).")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Time out of range: '{hhmm}'.")
    return hour * 60 + minute


def normalize_time(time_str: str) -> str:
    m = TIME_REGEX.search(time_str)
    if not m:
        return time_str.strip().upper()
    return f"{int(m.group(1)):02d}:{m.group(2)} {m.group(3).upper()}"


def is_fdfs_time(time_str: str, cutoff_minutes: int) -> bool:
    """Return True if showtime is between 12:00 AM and cutoff_minutes (inclusive)."""
    mins = parse_12h_to_minutes(time_str)
    if mins is None:
        return False
    return 0 <= mins <= cutoff_minutes


def detect_tags(text: str, keywords: Iterable[str], default: str) -> str:
    upper = text.upper()
    found = [kw for kw in keywords if re.search(rf"\b{re.escape(kw)}\b", upper)]
    return ", ".join(dict.fromkeys(found)) if found else default


def date_markers(target_date: str) -> list[str]:
    """Generate common date strings found on Indian ticketing pages."""
    dt = datetime.strptime(target_date, "%Y-%m-%d")
    day = dt.day
    mon = dt.strftime("%b").upper()
    month_full = dt.strftime("%B").upper()
    return [
        dt.strftime("%Y%m%d"),
        target_date,
        f"{day:02d} {mon}",
        f"{day} {mon}",
        f"{mon} {day:02d}",
        f"{mon} {day}",
        f"{day} {month_full}",
        f"{month_full} {day}",
    ]


# ---------------------------------------------------------------------------
# Page Fetching (Playwright preferred, requests/urllib fallback)
# ---------------------------------------------------------------------------

class PageFetcher:
    def __init__(self, use_playwright: bool = True, timeout_ms: int = 25000):
        self.use_playwright = use_playwright
        self.timeout_ms = timeout_ms
        self._pw = None
        self._browser = None
        self._context = None

    def __enter__(self) -> "PageFetcher":
        if self.use_playwright:
            try:
                from playwright.sync_api import sync_playwright
                self._pw = sync_playwright().start()
                self._browser = self._pw.chromium.launch(headless=True)
                self._context = self._browser.new_context(
                    user_agent=USER_AGENT,
                    viewport={"width": 1366, "height": 900},
                    locale="en-IN",
                )
            except Exception as exc:
                log.warning(
                    "Playwright unavailable (%s). Falling back to lightweight HTTP requests.",
                    exc,
                )
                self.use_playwright = False
        return self

    def __exit__(self, *args) -> None:
        if self._context:
            self._context.close()
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()

    def fetch(self, url: str) -> tuple[str, str]:
        """Return (final_url, html_content)."""
        if self.use_playwright and self._context is not None:
            page = self._context.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                page.wait_for_timeout(3500)
                return page.url, page.content()
            finally:
                page.close()

        headers = {
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-IN,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        if requests is not None:
            resp = requests.get(url, headers=headers, timeout=self.timeout_ms / 1000)
            resp.raise_for_status()
            return resp.url, resp.text

        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=self.timeout_ms / 1000) as resp:
            return resp.geturl(), resp.read().decode("utf-8", errors="ignore")


# ---------------------------------------------------------------------------
# Generic & Platform-Specific Showtime Extractors
# ---------------------------------------------------------------------------

def extract_shows_from_html(
    html: str,
    platform: str,
    booking_url: str,
    target_date: str,
    cutoff_minutes: int,
    require_date_match: bool = True,
) -> list[ShowOpening]:
    """
    Extract Chennai theatre names + FDFS showtimes from a rendered ticketing page.
    Works across BookMyShow, PVR Cinemas, and District by Zomato DOM layouts.
    """
    if not html:
        return []

    upper_html = html.upper()
    if require_date_match:
        markers = date_markers(target_date)
        if not any(marker in upper_html for marker in markers):
            return []

    # Reject pages where the site silently redirected to a different date
    compact_target = target_date.replace("-", "")
    if platform == "BookMyShow" and re.search(r"/2026\d{4}\b", booking_url):
        if compact_target not in booking_url:
            return []

    found: list[ShowOpening] = []

    if BeautifulSoup is not None:
        soup = BeautifulSoup(html, "html.parser")
        page_text = soup.get_text(" ", strip=True)
        page_formats = detect_tags(page_text[:2500], FORMAT_KEYWORDS, "Standard")
        page_langs = detect_tags(page_text[:2500], LANGUAGE_KEYWORDS, "English/Tamil")

        # Look for venue/cinema containers that hold both a theatre name and showtime pills
        candidate_blocks = soup.find_all(["li", "div", "section", "article"])
        seen_local: set[tuple[str, str]] = set()

        for block in candidate_blocks:
            text = block.get_text(" ", strip=True)
            if not text or len(text) > 900:
                continue
            times = TIME_REGEX.findall(text)
            if not times:
                continue

            # Identify a likely theatre heading inside this block
            heading = block.find(["h2", "h3", "h4", "a", "strong"])
            if not heading:
                continue
            theatre_name = heading.get_text(" ", strip=True)
            if (
                not theatre_name
                or len(theatre_name) < 4
                or len(theatre_name) > 120
                or TIME_REGEX.search(theatre_name)
            ):
                continue

            block_formats = detect_tags(text, FORMAT_KEYWORDS, page_formats)
            block_langs = detect_tags(text, LANGUAGE_KEYWORDS, page_langs)

            for h, m, ap in times:
                norm_time = f"{int(h):02d}:{m} {ap.upper()}"
                if not is_fdfs_time(norm_time, cutoff_minutes):
                    continue
                pair = (theatre_name.lower(), norm_time)
                if pair in seen_local:
                    continue
                seen_local.add(pair)
                found.append(
                    ShowOpening(
                        platform=platform,
                        theatre=theatre_name,
                        showtime=norm_time,
                        date=target_date,
                        formats=block_formats,
                        language=block_langs,
                        booking_url=booking_url,
                    )
                )

    # Fallback regex scan if BeautifulSoup is missing or DOM structure changed
    if not found:
        raw_times = {
            f"{int(h):02d}:{m} {ap.upper()}"
            for h, m, ap in TIME_REGEX.findall(html)
            if is_fdfs_time(f"{int(h):02d}:{m} {ap.upper()}", cutoff_minutes)
        }
        if raw_times:
            formats = detect_tags(html[:4000], FORMAT_KEYWORDS, "Standard")
            langs = detect_tags(html[:4000], LANGUAGE_KEYWORDS, "English/Tamil")
            for t in sorted(raw_times, key=lambda x: parse_12h_to_minutes(x) or 0):
                found.append(
                    ShowOpening(
                        platform=platform,
                        theatre="Chennai Theatre (open link to view list)",
                        showtime=t,
                        date=target_date,
                        formats=formats,
                        language=langs,
                        booking_url=booking_url,
                    )
                )

    return found


def check_bookmyshow(
    fetcher: PageFetcher, target_date: str, cutoff_minutes: int
) -> list[ShowOpening]:
    compact_date = target_date.replace("-", "")
    urls = [
        f"https://in.bookmyshow.com/movies/chennai/avengers-doomsday/buytickets/{code}/{compact_date}"
        for code in BOOKMYSHOW_EVENT_CODES
    ]

    # Also inspect the main movie page to auto-discover any newly added event codes (e.g. IMAX / Tamil)
    try:
        _, movie_html = fetcher.fetch(BOOKMYSHOW_MOVIE_URL)
        discovered_codes = set(re.findall(r"\bET\d{8}\b", movie_html))
        for code in discovered_codes:
            candidate = (
                f"https://in.bookmyshow.com/movies/chennai/avengers-doomsday/"
                f"buytickets/{code}/{compact_date}"
            )
            if candidate not in urls:
                urls.append(candidate)
    except Exception as exc:
        log.debug("BookMyShow discovery page skipped: %s", exc)

    results: list[ShowOpening] = []
    for url in urls:
        try:
            final_url, html = fetcher.fetch(url)
            shows = extract_shows_from_html(
                html=html,
                platform="BookMyShow",
                booking_url=final_url,
                target_date=target_date,
                cutoff_minutes=cutoff_minutes,
                require_date_match=True,
            )
            results.extend(shows)
        except Exception as exc:
            log.warning("BookMyShow check failed for %s: %s", url, exc)
    return results


def check_pvr(
    fetcher: PageFetcher, target_date: str, cutoff_minutes: int
) -> list[ShowOpening]:
    try:
        final_url, html = fetcher.fetch(PVR_SESSIONS_URL)
        return extract_shows_from_html(
            html=html,
            platform="PVR Cinemas",
            booking_url=final_url,
            target_date=target_date,
            cutoff_minutes=cutoff_minutes,
            require_date_match=True,
        )
    except Exception as exc:
        log.warning("PVR Cinemas check failed: %s", exc)
        return []


def check_district(
    fetcher: PageFetcher, target_date: str, cutoff_minutes: int
) -> list[ShowOpening]:
    urls = list(DISTRICT_DIRECT_URLS)

    # Auto-discover Avengers: Doomsday link from District's Chennai movies page
    if not urls:
        try:
            _, listing_html = fetcher.fetch(DISTRICT_CHENNAI_URL)
            matches = re.findall(
                r'href="([^"]*avengers[^"]*doomsday[^"]*)"',
                listing_html,
                flags=re.IGNORECASE,
            )
            for href in dict.fromkeys(matches):
                urls.append(urljoin("https://www.district.in", href))
        except Exception as exc:
            log.warning("District Chennai discovery failed: %s", exc)

    results: list[ShowOpening] = []
    for url in urls:
        try:
            final_url, html = fetcher.fetch(url)
            shows = extract_shows_from_html(
                html=html,
                platform="District",
                booking_url=final_url,
                target_date=target_date,
                cutoff_minutes=cutoff_minutes,
                require_date_match=True,
            )
            results.extend(shows)
        except Exception as exc:
            log.warning("District check failed for %s: %s", url, exc)
    return results


# ---------------------------------------------------------------------------
# Phone Push Notifications (ntfy.sh + optional Telegram)
# ---------------------------------------------------------------------------

def send_ntfy(title: str, message: str, click_url: str) -> bool:
    if not NTFY_TOPIC or NTFY_TOPIC == "REPLACE_WITH_YOUR_UNIQUE_TOPIC":
        log.error(
            "NTFY_TOPIC is not configured! Set NTFY_TOPIC env var or edit the script."
        )
        return False

    endpoint = f"{NTFY_SERVER}/{NTFY_TOPIC}"
    headers = {
        "Title": title,
        "Priority": "urgent",
        "Tags": "rotating_light,ticket",
        "Click": click_url,
        "Content-Type": "text/plain; charset=utf-8",
    }
    req = urllib.request.Request(
        endpoint,
        data=message.encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if 200 <= resp.status < 300:
                log.info("ntfy alert delivered to topic '%s'.", NTFY_TOPIC)
                return True
            log.warning("ntfy returned HTTP %s", resp.status)
            return False
    except urllib.error.URLError as exc:
        log.error("Failed to send ntfy alert: %s", exc)
        return False


def send_telegram(title: str, message: str, click_url: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    endpoint = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps(
        {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": f"🚨 {title}\n\n{message}\n\nBook now: {click_url}",
            "disable_web_page_preview": False,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except urllib.error.URLError as exc:
        log.error("Failed to send Telegram alert: %s", exc)
        return False


def notify_phone(shows: list[ShowOpening]) -> None:
    """Group newly detected FDFS shows by platform and send urgent phone alerts."""
    by_platform: dict[str, list[ShowOpening]] = {}
    for show in shows:
        by_platform.setdefault(show.platform, []).append(show)

    for platform, group in by_platform.items():
        group.sort(key=lambda s: (parse_12h_to_minutes(s.showtime) or 0, s.theatre))
        lines = [
            f"• {s.showtime} — {s.theatre} ({s.formats} | {s.language})"
            for s in group[:12]
        ]
        if len(group) > 12:
            lines.append(f"...and {len(group) - 12} more FDFS shows!")

        title = f"FDFS OPEN on {platform}: Avengers Doomsday ({group[0].date})"
        body = "\n".join(lines)
        click_url = group[0].booking_url

        send_ntfy(title, body, click_url)
        send_telegram(title, body, click_url)


# ---------------------------------------------------------------------------
# State Persistence & Main Loop
# ---------------------------------------------------------------------------

def load_seen(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return set(data.get("seen_keys", []))
    except Exception:
        return set()


def save_seen(path: Path, seen_keys: set[str], latest_shows: list[ShowOpening]) -> None:
    payload = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "seen_keys": sorted(seen_keys),
        "last_detected": [asdict(s) for s in latest_shows[-25:]],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def run_check(
    fetcher: PageFetcher, target_date: str, cutoff_minutes: int, seen: set[str]
) -> list[ShowOpening]:
    all_shows: list[ShowOpening] = []
    all_shows.extend(check_bookmyshow(fetcher, target_date, cutoff_minutes))
    all_shows.extend(check_district(fetcher, target_date, cutoff_minutes))
    all_shows.extend(check_pvr(fetcher, target_date, cutoff_minutes))

    new_shows: list[ShowOpening] = []
    for show in all_shows:
        key = show.dedup_key()
        if key not in seen:
            seen.add(key)
            new_shows.append(show)
    return new_shows


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Chennai FDFS ticket monitor for Avengers: Doomsday (BookMyShow, District, PVR)."
    )
    parser.add_argument(
        "--date",
        default=TARGET_DATE,
        help=f"Target opening date in YYYY-MM-DD (default: {TARGET_DATE})",
    )
    parser.add_argument(
        "--cutoff",
        default=FDFS_CUTOFF_24H,
        help=f"Latest FDFS start time in 24h HH:MM (default: {FDFS_CUTOFF_24H})",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL_SEC,
        help=f"Seconds between checks (default: {DEFAULT_INTERVAL_SEC})",
    )
    parser.add_argument(
        "--no-playwright",
        action="store_true",
        help="Disable headless Chromium and use plain HTTP requests only",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single scan and exit",
    )
    parser.add_argument(
        "--test-alert",
        action="store_true",
        help="Send a test push notification to your phone and exit",
    )
    args = parser.parse_args()

    if args.test_alert:
        ok = send_ntfy(
            title="Test Alert: Avengers Doomsday FDFS Monitor",
            message="Phone push alerts are working! You will be alerted as soon as Dec 17 FDFS tickets open in Chennai.",
            click_url=BOOKMYSHOW_MOVIE_URL,
        )
        send_telegram(
            title="Test Alert: Avengers Doomsday FDFS Monitor",
            message="Phone push alerts are working!",
            click_url=BOOKMYSHOW_MOVIE_URL,
        )
        return 0 if ok else 1

    cutoff_minutes = parse_24h_to_minutes(args.cutoff)
    seen = load_seen(STATE_FILE)

    log.info(
        "Starting Avengers: Doomsday Chennai FDFS monitor | Date: %s | FDFS cutoff: <= %s | Interval: %ss",
        args.date,
        args.cutoff,
        args.interval,
    )

    with PageFetcher(use_playwright=not args.no_playwright) as fetcher:
        while True:
            try:
                new_shows = run_check(fetcher, args.date, cutoff_minutes, seen)
                if new_shows:
                    log.info("FOUND %d NEW FDFS SHOW(S)! Sending phone alert...", len(new_shows))
                    notify_phone(new_shows)
                    save_seen(STATE_FILE, seen, new_shows)
                else:
                    log.info("No new Dec 17 FDFS shows yet across BookMyShow, District, and PVR.")
            except KeyboardInterrupt:
                log.info("Monitor stopped by user.")
                return 0
            except Exception as exc:
                log.error("Unexpected error during check: %s", exc)

            if args.once:
                return 0
            time.sleep(max(30, args.interval))


if __name__ == "__main__":
    sys.exit(main())
