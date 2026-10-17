"""Read-only public odds clients."""

from __future__ import annotations

import json
import os
import random
import ssl
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


KALSHI = "https://external-api.kalshi.com/trade-api/v2"
PINNACLE = "https://guest.api.arcadia.pinnacle.com/0.1"
BOVADA = "https://www.bovada.lv/services/sports/event"


def _load_env(path: Path = Path(".env")) -> None:
    """Load KEY=value lines from .env without overriding the real environment."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


_load_env()
PINNACLE_GUEST_KEY = os.environ.get("PINNACLE_API_KEY", "")
PINNACLE_HEADERS = {
    "X-API-Key": PINNACLE_GUEST_KEY,
    "User-Agent": "Mozilla/5.0 AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.pinnacle.com/",
}
_pinnacle_cache: dict[str, object] = {}
SPORT_IDS = (12, 22, 6, 15, 29, 4)
BOVADA_PATHS = (
    "esports",
    "ufc-mma",
    "boxing",
    "football",
    "football/college-football",
    "soccer",
    "basketball",
)


def get_json(url: str, headers: dict[str, str] | None = None) -> object:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0", **(headers or {})})
    mac_cert = Path("/etc/ssl/cert.pem")
    context = ssl.create_default_context(cafile=str(mac_cert) if mac_cert.exists() else None)
    with urlopen(request, timeout=20, context=context) as response:
        return json.load(response)


KALSHI_SERIES = {
    "boxing": ("KXBOXING", "KXBOXINGFIGHT", "KXBOXINGMOV", "KXBOXINGKNOCKOUT", "KXBOXINGROUNDS"),
    "mma": ("KXUFC", "KXUFCFIGHT", "KXUFCMOV", "KXUFCDISTANCE", "KXUFCROUNDS"),
    "esports": ("KXCS2GAME", "KXVALORANTGAME", "KXLOLGAME"),
    "football": ("KXNCAAFGAME", "KXNFLGAME"),
    "soccer": ("KXEPLGAME", "KXLALIGAGAME", "KXLALIGA"),
    "basketball": ("KXNBAGAME", "KXWNBAGAME", "KXNCAABGAME"),
}


class KalshiRateLimiter:
    def __init__(self) -> None:
        self.next_request = 0.0

    def wait(self) -> None:
        delay = self.next_request - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self.next_request = time.monotonic() + 0.21


def _retry_after(error: HTTPError) -> float:
    value = error.headers.get("Retry-After") if error.headers else None
    if not value:
        return 0.0
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            return max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 0.0


def _kalshi_get(url: str, limiter: KalshiRateLimiter) -> object:
    for attempt in range(5):
        limiter.wait()
        try:
            return get_json(url)
        except HTTPError as error:
            if error.code != 429 and not 500 <= error.code <= 599:
                raise
            if attempt == 4:
                raise
            delay = max(_retry_after(error), 2 ** attempt + random.uniform(0, 0.25))
            time.sleep(delay)


def _kalshi_series_events(series_ticker: str, limiter: KalshiRateLimiter) -> list[dict]:
    events = []
    cursor = ""
    while True:
        params = {"limit": 200, "with_nested_markets": "true", "series_ticker": series_ticker,
                  "min_close_ts": int(time.time()) - 86400}
        if cursor:
            params["cursor"] = cursor
        page = _kalshi_get(f"{KALSHI}/events?{urlencode(params)}", limiter)
        events.extend(page["events"])
        cursor = page.get("cursor", "")
        if not cursor:
            return events


def kalshi_events(sports: set[str]) -> list[dict]:
    tickers = sorted({ticker for sport in sports for ticker in KALSHI_SERIES.get(sport, ())})
    limiter = KalshiRateLimiter()
    cache = {ticker: _kalshi_series_events(ticker, limiter) for ticker in tickers}
    return [event for ticker in tickers for event in cache[ticker]]


def pinnacle_sport(sport_id: int) -> tuple[list[dict], list[dict]]:
    matchups = pinnacle_get(f"{PINNACLE}/sports/{sport_id}/matchups")
    markets = pinnacle_get(f"{PINNACLE}/sports/{sport_id}/markets/straight")
    return matchups, markets


def pinnacle_get(url: str) -> object:
    if url in _pinnacle_cache:
        return _pinnacle_cache[url]
    for attempt in range(3):
        try:
            response = get_json(url, PINNACLE_HEADERS)
            _pinnacle_cache[url] = response
            return response
        except OSError:
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)


def bovada_sport(path: str) -> list[dict]:
    query = urlencode({"marketFilterId": "def", "preMatchOnly": "true", "lang": "en"})
    return get_json(f"{BOVADA}/v2/events/A/description/{path}?{query}")


def bovada_coupon(event_link: str) -> list[dict]:
    return get_json(f"{BOVADA}/coupon/events/A/description/{event_link.lstrip('/')}")
